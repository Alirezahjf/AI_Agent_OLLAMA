"""Telethon wrapper for the local assistant.

The wrapper is intentionally small: it owns a single ``TelegramClient``
singleton and exposes a handful of methods that the agent loop calls.
A separate ``connect()`` step is required before any other method.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("telegram")


class TelegramError(AssistantError):
    """A user-facing failure from the personal Telegram client."""


@dataclass
class Chat:
    """A dialog summary returned by :meth:`PersonalTelegram.list_chats`."""

    id: int
    title: str
    username: str | None
    is_group: bool
    last_message: str | None = None
    unread_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "username": self.username,
            "is_group": self.is_group,
            "last_message": self.last_message,
            "unread_count": self.unread_count,
        }


@dataclass
class Message:
    """A single message returned by the personal client."""

    id: int
    chat_id: int
    sender: str
    text: str
    date: datetime
    is_outgoing: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "sender": self.sender,
            "text": self.text,
            "date": self.date.isoformat(),
            "is_outgoing": self.is_outgoing,
        }


class PersonalTelegram:
    """Async client for the user's personal Telegram account.

    Public methods are synchronous wrappers that schedule their work on
    a private event loop running in a background thread, so the CLI can
    call them from normal code without ``await``.
    """

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: Path,
    ) -> None:
        if not api_id or not api_hash or not phone:
            raise TelegramError(
                "telegram credentials missing: set api_id, api_hash, and phone in config"
            )
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)
        self._phone = str(phone)
        self._session_path = Path(session_path)
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._connected = False
        # Stepwise login state machine:
        #   disconnected -> await_code -> await_2fa -> connected
        self._login_state = "disconnected"
        self._login_ctx: dict[str, Any] = {}

    # ---------------------------------------------------------------- I/O

    @property
    def session_path(self) -> Path:
        return self._session_path

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def login_state(self) -> str:
        """One of ``disconnected`` | ``await_code`` | ``await_2fa`` | ``connected``."""
        return "connected" if self._connected else self._login_state

    # ------------------------------------------------------ stepwise login

    def start_login(self) -> dict[str, Any]:
        """Begin the interactive login: connect the session and ask for an SMS code.

        Returns ``{"state": "await_code", "message": ...}``.  The caller
        then submits the code with :meth:`submit_code` and, if Telegram
        asks for 2FA, :meth:`submit_password`.  A valid session file on
        disk skips the code step entirely and returns ``connected``.
        """
        with self._lock:
            if self._connected:
                return {"state": "connected", "message": "already connected"}
            if self._login_state != "disconnected":
                return {"state": self._login_state, "message": "login already in progress"}
            self._start_loop()
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(self._begin_login(), self._loop)
            return future.result(timeout=180)

    def submit_code(self, code: str) -> dict[str, Any]:
        """Submit the SMS code received after :meth:`start_login`."""
        with self._lock:
            if self._connected:
                return {"state": "connected", "message": "already connected"}
            if self._login_state != "await_code":
                raise TelegramError("هیچ درخواست کدی در جریان نیست؛ ابتدا اتصال را شروع کنید")
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(
                self._submit_code(str(code or "").strip()), self._loop
            )
            return future.result(timeout=120)

    def submit_password(self, password: str) -> dict[str, Any]:
        """Submit the 2FA password when the code was accepted but 2FA is on."""
        with self._lock:
            if self._connected:
                return {"state": "connected", "message": "already connected"}
            if self._login_state != "await_2fa":
                raise TelegramError("تلگرام رمز دوم‌مرحله‌ای نخواسته است")
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(
                self._submit_password(str(password)), self._loop
            )
            return future.result(timeout=120)

    def cancel_login(self) -> None:
        """Abort an in-progress login and close the temporary session."""
        with self._lock:
            if self._login_state in {"await_code", "await_2fa"}:
                assert self._loop is not None
                future = asyncio.run_coroutine_threadsafe(self._abort_login(), self._loop)
                try:
                    future.result(timeout=15)
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    logger.debug("login abort failed: %s", exc)
            self._login_state = "disconnected"
            self._login_ctx = {}

    def connect(self, *, code_callback=None, password_callback=None) -> str:
        """Connect and (if needed) complete the interactive login.

        ``code_callback`` is a zero-arg callable that should return the
        SMS code the user received. ``password_callback`` is invoked if
        Telegram asks for 2FA. Either may be None, in which case the
        helper falls back to ``input()``.
        """
        with self._lock:
            if self._connected:
                return "already connected"
            result = self.start_login()
            while result.get("state") == "await_code":
                code = (code_callback or (lambda: input("Telegram code: ")))()
                result = self.submit_code(code)
            if result.get("state") == "await_2fa":
                password = (password_callback or (lambda: input("2FA password: ")))()
                result = self.submit_password(password)
            if result.get("state") != "connected":
                raise TelegramError(str(result.get("error") or result.get("message") or "login failed"))
            return str(result.get("message") or "connected")

    def disconnect(self) -> None:
        with self._lock:
            if not self._connected:
                return
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                future.result(timeout=15)
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("disconnect failed: %s", exc)
            self._login_state = "disconnected"
            self._login_ctx = {}

    # ----------------------------------------------------------- Actions

    def list_chats(self, limit: int = 30) -> list[Chat]:
        return self._run(self._list_chats(limit))

    def send_message(self, chat: str | int, text: str) -> Message:
        if not isinstance(text, str) or not text:
            raise TelegramError("text must be a non-empty string")
        return self._run(self._send_message(chat, text))

    def send_photo(self, chat: str | int, path: str | os.PathLike, caption: str = "") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"photo does not exist: {target}")
        return self._run(self._send_file(chat, target, caption=caption, is_photo=True))

    def send_file(self, chat: str | int, path: str | os.PathLike, caption: str = "") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"file does not exist: {target}")
        return self._run(self._send_file(chat, target, caption=caption, is_photo=False))

    def search_messages(self, chat: str | int, query: str, limit: int = 30) -> list[Message]:
        return self._run(self._search_messages(chat, query, limit))

    def get_me(self) -> dict[str, Any]:
        return self._run(self._get_me())

    # -------------------------------------------------------- Internals

    def _start_loop(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return
        ready_evt = threading.Event()
        loop_holder: dict[str, Any] = {}

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            ready_evt.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=runner, name="telethon-loop", daemon=True)
        self._thread.start()
        ready_evt.wait(timeout=10)
        self._loop = loop_holder["loop"]

    def _run(self, coro):
        if not self._connected:
            coro.close()  # avoid 'coroutine was never awaited' warnings
            raise TelegramError("telegram client is not connected; call connect() first")
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    # -------------------------------------------------------- Async core

    async def _begin_login(self) -> dict[str, Any]:
        try:
            from telethon import TelegramClient  # type: ignore
        except ImportError as exc:
            raise TelegramError(
                "telethon is not installed; run: pip install telethon"
            ) from exc

        client = TelegramClient(
            str(self._session_path.with_suffix("")),
            self._api_id,
            self._api_hash,
        )
        await client.connect()
        self._client = client
        if await client.is_user_authorized():
            # Session file is already valid — no re-login needed.
            self._login_state = "connected"
            self._connected = True
            me = await client.get_me()
            return {
                "state": "connected",
                "message": f"connected as {getattr(me, 'username', None) or me.first_name}",
            }
        sent = await client.send_code_request(self._phone)
        self._login_state = "await_code"
        self._login_ctx = {"phone_code_hash": sent.phone_code_hash}
        return {
            "state": "await_code",
            "message": "کد تأیید به تلگرام شما ارسال شد؛ آن را وارد کنید.",
        }

    async def _submit_code(self, code: str) -> dict[str, Any]:
        if not code:
            raise TelegramError("کد وارد نشده است")
        client = self._client
        if client is None:
            raise TelegramError("ابتدا اتصال را شروع کنید")
        phone_code_hash = self._login_ctx.get("phone_code_hash")
        try:
            await client.sign_in(self._phone, code, phone_code_hash=phone_code_hash)
        except Exception as sign_in_exc:
            # SessionPasswordNeededError means 2FA is on.
            if "SessionPasswordNeededError" in type(sign_in_exc).__name__:
                self._login_state = "await_2fa"
                return {
                    "state": "await_2fa",
                    "message": "حساب شما رمز دوم‌مرحله‌ای (2FA) دارد؛ رمز را وارد کنید.",
                }
            if "PhoneCodeInvalidError" in type(sign_in_exc).__name__:
                raise TelegramError("کد واردشده صحیح نیست؛ دوباره تلاش کنید")
            raise TelegramError(f"ورود ناموفق بود: {sign_in_exc}") from sign_in_exc
        return await self._finish_login()

    async def _submit_password(self, password: str) -> dict[str, Any]:
        if not password:
            raise TelegramError("رمز 2FA وارد نشده است")
        client = self._client
        if client is None:
            raise TelegramError("ابتدا اتصال را شروع کنید")
        try:
            await client.sign_in(password=password)
        except Exception as sign_in_exc:
            if "PasswordHashInvalidError" in type(sign_in_exc).__name__:
                raise TelegramError("رمز 2FA صحیح نیست؛ دوباره تلاش کنید")
            raise TelegramError(f"ورود با رمز 2FA ناموفق بود: {sign_in_exc}") from sign_in_exc
        return await self._finish_login()

    async def _finish_login(self) -> dict[str, Any]:
        self._login_state = "connected"
        self._connected = True
        me = await self._get_me()
        return {
            "state": "connected",
            "message": f"connected as {me.get('username') or me.get('first_name') or '?'}",
            "user": me,
        }

    async def _abort_login(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("abort disconnect failed: %s", exc)
            self._client = None
            self._connected = False

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
                self._connected = False

    async def _resolve_entity(self, target):
        if isinstance(target, int):
            return await self._client.get_entity(target)
        cleaned = str(target).strip()
        if not cleaned:
            raise TelegramError("chat target is empty")
        try:
            return await self._client.get_entity(cleaned)
        except Exception as exc:
            raise TelegramError(f"could not resolve chat {target!r}: {exc}") from exc

    async def _list_chats(self, limit: int) -> list[Chat]:
        chats: list[Chat] = []
        async for dialog in self._client.iter_dialogs(limit=max(1, limit)):
            entity = dialog.entity
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "?"
            username = getattr(entity, "username", None)
            is_group = bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) or getattr(entity, "is_group", False))
            last_message = None
            if dialog.message is not None:
                last_message = (dialog.message.message or "")[:140]
            chats.append(
                Chat(
                    id=dialog.id,
                    title=str(title),
                    username=username,
                    is_group=is_group,
                    last_message=last_message,
                    unread_count=int(dialog.unread_count or 0),
                )
            )
        return chats

    async def _send_message(self, chat, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.send_message(entity, text)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _send_file(self, chat, path: Path, *, caption: str, is_photo: bool) -> Message:
        entity = await self._resolve_entity(chat)
        file = await self._client.upload_file(str(path))
        if is_photo:
            result = await self._client.send_file(
                entity, file, caption=caption or "", force_document=False
            )
        else:
            result = await self._client.send_file(
                entity, file, caption=caption or "", force_document=True
            )
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _search_messages(self, chat, query: str, limit: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = getattr(entity, "id", 0)
        results: list[Message] = []
        async for msg in self._client.iter_messages(entity, search=query, limit=max(1, limit)):
            results.append(_message_from_telethon(msg, chat_id=chat_id))
        return results

    async def _get_me(self) -> dict[str, Any]:
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": getattr(me, "last_name", ""),
            "username": getattr(me, "username", ""),
            "phone": getattr(me, "phone", ""),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_from_telethon(msg, *, chat_id: int) -> Message:
    sender = "?"
    if getattr(msg, "sender", None) is not None:
        sender_obj = msg.sender
        sender = getattr(sender_obj, "username", None) or getattr(sender_obj, "first_name", None) or "?"
    return Message(
        id=int(msg.id),
        chat_id=chat_id,
        sender=str(sender),
        text=str(msg.message or ""),
        date=msg.date,
        is_outgoing=bool(getattr(msg, "out", False)),
    )
