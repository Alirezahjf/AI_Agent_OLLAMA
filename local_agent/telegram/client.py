"""Telethon wrapper for the local assistant.

The wrapper is intentionally small: it owns a single ``TelegramClient``
singleton and exposes a handful of methods that the agent loop calls.
A separate ``connect()`` step is required before any other method.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("telegram")


class TelegramError(AssistantError):
    """Structured, safe and user-facing Telegram failure."""

    def __init__(
        self, message: str, *, code: str = "telegram_error", retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "retry_after": self.retry_after,
        }


@dataclass
class Chat:
    """A live dialog summary returned by :meth:`PersonalTelegram.list_chats`."""

    id: int
    title: str
    username: str | None
    is_group: bool
    last_message: str | None = None
    unread_count: int = 0
    is_channel: bool = False
    is_bot: bool = False
    is_private: bool = False
    is_supergroup: bool = False
    is_forum: bool = False
    verified: bool = False
    pinned: bool = False
    muted: bool = False
    archived: bool = False
    folder_id: int | None = None
    members_count: int | None = None
    last_message_date: datetime | None = None
    phone: str | None = None
    deleted: bool = False

    @property
    def kind(self) -> str:
        if self.is_bot:
            return "bot"
        if self.is_channel:
            return "channel"
        if self.is_supergroup:
            return "supergroup"
        if self.is_group:
            return "group"
        return "private"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "username": self.username,
            "kind": self.kind,
            "is_group": self.is_group,
            "is_channel": self.is_channel,
            "is_bot": self.is_bot,
            "is_private": self.is_private,
            "is_supergroup": self.is_supergroup,
            "is_forum": self.is_forum,
            "verified": self.verified,
            "pinned": self.pinned,
            "muted": self.muted,
            "archived": self.archived,
            "folder_id": self.folder_id,
            "members_count": self.members_count,
            "phone": self.phone,
            "deleted": self.deleted,
            "last_message": self.last_message,
            "last_message_date": (
                self.last_message_date.isoformat() if self.last_message_date else None
            ),
            "unread_count": self.unread_count,
        }


@dataclass
class Contact:
    """Fresh contact data returned by Telegram's contacts.getContacts API."""

    id: int
    first_name: str
    last_name: str = ""
    username: str = ""
    phone: str = ""
    is_contact: bool = True
    is_mutual_contact: bool = False
    is_bot: bool = False
    verified: bool = False
    deleted: bool = False
    status: str = "unknown"
    last_seen: datetime | None = None
    entity: Any = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "phone": self.phone,
            "is_contact": self.is_contact,
            "is_mutual_contact": self.is_mutual_contact,
            "is_bot": self.is_bot,
            "verified": self.verified,
            "deleted": self.deleted,
            "status": self.status,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


@dataclass
class _ResolvedCandidate:
    entity: Any
    id: int
    title: str
    username: str
    kind: str
    source: str


@dataclass
class Message:
    """A rich message summary returned by the personal client."""

    id: int
    chat_id: int
    sender: str
    text: str
    date: datetime
    is_outgoing: bool
    sender_id: int | None = None
    message_type: str = "text"
    reply_to_msg_id: int | None = None
    forwards: int = 0
    views: int = 0
    has_media: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "sender": self.sender,
            "text": self.text,
            "date": self.date.isoformat(),
            "is_outgoing": self.is_outgoing,
            "message_type": self.message_type,
            "reply_to_msg_id": self.reply_to_msg_id,
            "forwards": self.forwards,
            "views": self.views,
            "has_media": self.has_media,
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
        account_name: str = "اصلی",
    ) -> None:
        if not api_id or not api_hash or not phone:
            raise TelegramError(
                "telegram credentials missing: set api_id, api_hash, and phone in config"
            )
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)
        self._phone = str(phone)
        self._name = str(account_name)
        self._session_path = Path(session_path)
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._connected = False
        self._manual_disconnect = False
        self._connected_at: datetime | None = None
        self._last_error = ""
        self._last_error_code = ""
        # Stepwise login state machine:
        #   disconnected -> await_code -> await_2fa -> connected
        self._login_state = "disconnected"
        self._login_ctx: dict[str, Any] = {}

    # ---------------------------------------------------------------- I/O

    @property
    def session_path(self) -> Path:
        return self._session_path

    @property
    def account_name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connected_at(self) -> datetime | None:
        return self._connected_at

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_error_code(self) -> str:
        return self._last_error_code

    @property
    def manual_disconnect(self) -> bool:
        return self._manual_disconnect

    @property
    def login_state(self) -> str:
        """One of ``disconnected`` | ``await_code`` | ``await_2fa`` | ``connected``."""
        return "connected" if self._connected else self._login_state

    # ------------------------------------------------------ stepwise login

    def start_login(self) -> dict[str, Any]:
        """Begin login; a valid session skips the code step."""
        self._manual_disconnect = False
        self._last_error = ""
        self._last_error_code = ""
        with self._lock:
            if self._connected:
                return {"state": "connected", "message": "already connected"}
            if self._login_state != "disconnected":
                return {"state": self._login_state, "message": "login already in progress"}
            self._start_loop()
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(self._begin_login(), self._loop)
            return self._await_future(future, timeout=180)

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
            return self._await_future(future, timeout=120)

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
            return self._await_future(future, timeout=120)

    def cancel_login(self) -> None:
        """Abort an in-progress login and close the temporary session."""
        with self._lock:
            if self._login_state in {"await_code", "await_2fa"}:
                assert self._loop is not None
                future = asyncio.run_coroutine_threadsafe(self._abort_login(), self._loop)
                try:
                    future.result(timeout=15)
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    logger.debug("login abort failed: %s", type(exc).__name__)
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
        self._manual_disconnect = True
        with self._lock:
            if not self._connected:
                return
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                future.result(timeout=15)
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("disconnect failed: %s", type(exc).__name__)
            self._login_state = "disconnected"
            self._login_ctx = {}

    # ----------------------------------------------------------- Actions

    def list_chats(
        self, limit: int = 30, kind: str = "all", query: str = "", sort: str = "",
        *, offset: int = 0, archived: bool | None = None, unread_only: bool = False,
    ) -> list[Chat]:
        kind = str(kind or "all").lower()
        if kind not in {"private", "group", "supergroup", "channel", "bot", "all"}:
            raise TelegramError(
                "نوع چت باید private، group، supergroup، channel، bot یا all باشد",
                code="invalid_input",
            )
        sort = str(sort or "").lower()
        if sort not in {"", "unread", "recent"}:
            raise TelegramError("مرتب‌سازی باید recent یا unread باشد", code="invalid_input")
        return self._run_read(lambda: self._list_chats(
                max(1, int(limit or 30)), kind=kind, query=query, sort=sort,
                offset=max(0, int(offset or 0)), archived=archived,
                unread_only=bool(unread_only),
            ))

    def send_message(self, chat: str | int, text: str) -> Message:
        if not isinstance(text, str) or not text:
            raise TelegramError("text must be a non-empty string")
        return self._run(self._send_message(chat, text))

    def send_photo(self, chat: str | int, path: str | os.PathLike, caption: str = "") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"تصویر پیدا نشد: {target}", code="local_file_missing")
        return self._run(self._send_file(chat, target, caption=caption, is_photo=True))

    def send_file(self, chat: str | int, path: str | os.PathLike, caption: str = "") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"فایل پیدا نشد: {target}", code="local_file_missing")
        return self._run(self._send_file(chat, target, caption=caption, is_photo=False))

    def search_messages(self, chat: str | int, query: str, limit: int = 30) -> list[Message]:
        return self._run_read(lambda: self._search_messages(chat, query, limit))

    def get_me(self) -> dict[str, Any]:
        return self._run_read(self._get_me)

    def search_contacts(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._run_read(
            lambda: self._search_contacts(query, max(1, int(limit or 30)))
        )]

    def get_chat_history(self, chat: str | int, limit: int = 30, offset_id: int = 0) -> list[Message]:
        return self._run_read(lambda: self._get_chat_history(chat, limit, offset_id))

    def get_profile(self, chat: str | int, media_dir: Path) -> dict[str, Any]:
        return self._run_read(lambda: self._get_profile(chat, media_dir))

    def send_media(self, chat: str | int, path: str | os.PathLike, caption: str = "",
                   *, kind: str = "document") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"فایل پیدا نشد: {target}", code="local_file_missing")
        return self._run(self._send_media(chat, target, caption=caption, kind=kind))

    def send_location(self, chat: str | int, lat: float, lng: float) -> Message:
        return self._run(self._send_location(chat, float(lat), float(lng)))

    def download_media(self, chat: str | int, msg_id: int, filename: str, media_dir: Path) -> Path:
        return self._run(self._download_media(chat, int(msg_id), filename, media_dir))

    def reply_to(self, chat: str | int, msg_id: int, text: str) -> Message:
        if not isinstance(text, str) or not text:
            raise TelegramError("text must be a non-empty string")
        return self._run(self._reply_to(chat, int(msg_id), text))

    def forward_message(self, chat: str | int, from_chat: str | int, msg_id: int) -> Message:
        return self._run(self._forward_message(chat, from_chat, int(msg_id)))

    def mark_read(self, chat: str | int) -> None:
        self._run(self._mark_read(chat))

    def resolve_username(self, username: str) -> dict[str, Any]:
        return self._run_read(lambda: self._resolve_username(username))

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        """Resolve an ID, username, phone or display name using live Telegram data."""
        return self._run_read(lambda: self._resolve_target(target))

    def get_statistics(self) -> dict[str, Any]:
        return self._run_read(self._get_statistics)

    def list_unread_chats(self, limit: int = 30) -> list[Chat]:
        return self._run_read(lambda: self._list_unread_chats(max(1, int(limit or 30))))

    def get_chat_statistics(self, chat: str | int, limit: int = 500) -> dict[str, Any]:
        return self._run_read(lambda: self._get_chat_statistics(chat, max(1, min(int(limit or 500), 5000))))

    def export_chat(
        self, chat: str | int, output_dir: Path, *, fmt: str = "json", limit: int = 1000
    ) -> Path:
        fmt = str(fmt or "json").lower()
        if fmt not in {"json", "txt"}:
            raise TelegramError("فرمت خروجی باید json یا txt باشد", code="invalid_input")
        return self._run_read(
            lambda: self._export_chat(chat, output_dir, fmt=fmt, limit=max(1, min(int(limit), 5000)))
        )

    def download_media_batch(
        self, chat: str | int, media_dir: Path, *, limit: int = 100,
        media_types: list[str] | None = None,
    ) -> list[Path]:
        return self._run_read(lambda: self._download_media_batch(
            chat, media_dir, limit=max(1, min(int(limit), 500)),
            media_types=media_types or [],
        ))

    def refresh_summary(self) -> dict[str, Any]:
        return self._run_read(self._refresh_summary)

    def delete_message(self, chat: str | int, msg_id: int) -> None:
        self._run(self._delete_message(chat, int(msg_id)))

    def edit_message(self, chat: str | int, msg_id: int, text: str) -> Message:
        return self._run(self._edit_message(chat, int(msg_id), text))

    def list_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._run_read(
            lambda: self._list_contacts(max(1, int(limit or 100)))
        )]

    def get_contact_info(self, contact: str | int) -> dict[str, Any]:
        return self._run(self._get_contact_info(contact))

    def add_contact(self, phone: str, first_name: str, last_name: str = "") -> dict[str, Any]:
        return self._run(self._add_contact(phone, first_name, last_name))

    def delete_contact(self, contact: str | int) -> None:
        self._run(self._delete_contact(contact))

    def block_user(self, contact: str | int) -> None:
        self._run(self._block_user(contact))

    def unblock_user(self, contact: str | int) -> None:
        self._run(self._unblock_user(contact))

    def join_channel(self, channel: str | int) -> None:
        self._run(self._join_channel(channel))

    def leave_channel(self, channel: str | int) -> None:
        self._run(self._leave_channel(channel))

    def list_members(self, chat: str | int, limit: int = 100, admins: bool = False) -> list[dict[str, Any]]:
        return self._run(self._list_members(chat, limit, admins))

    def update_profile(self, first_name: str = "", last_name: str = "", about: str = "") -> None:
        self._run(self._update_profile(first_name, last_name, about))

    def update_username(self, username: str) -> None:
        self._run(self._update_username(username))

    def set_profile_photo(self, path: str | os.PathLike) -> None:
        self._run(self._set_profile_photo(Path(path)))

    def set_online_status(self, online: bool = True) -> None:
        self._run(self._set_online_status(online))

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

    def _await_future(self, future, *, timeout: int):
        try:
            result = future.result(timeout=timeout)
            self._last_error = ""
            self._last_error_code = ""
            return result
        except FutureTimeoutError as exc:
            future.cancel()
            error = TelegramError(
                "پاسخ تلگرام بیش از حد طول کشید؛ دوباره تلاش کنید",
                code="timeout", retryable=True,
            )
            self._last_error = str(error)
            self._last_error_code = error.code
            raise error from exc
        except Exception as exc:
            error = _translate_telegram_error(exc)
            self._last_error = str(error)
            self._last_error_code = error.code
            if error is exc:
                raise
            raise error from exc

    def _run(self, coro, *, timeout: int = 120):
        if not self._connected:
            coro.close()  # avoid 'coroutine was never awaited' warnings
            error = TelegramError(
                "تلگرام شخصی متصل نیست؛ ابتدا اتصال اکانت را برقرار کنید",
                code="not_connected",
            )
            self._last_error = str(error)
            self._last_error_code = error.code
            raise error
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            result = future.result(timeout=timeout)
            self._last_error = ""
            self._last_error_code = ""
            return result
        except FutureTimeoutError as exc:
            future.cancel()
            error = TelegramError(
                "پاسخ تلگرام بیش از حد طول کشید؛ دوباره تلاش کنید",
                code="timeout", retryable=True,
            )
            self._last_error = str(error)
            self._last_error_code = error.code
            raise error from exc
        except Exception as exc:
            error = _translate_telegram_error(exc)
            self._last_error = str(error)
            self._last_error_code = error.code
            if error is exc:
                raise
            raise error from exc

    def _run_read(self, factory, *, retries: int = 1, timeout: int = 120):
        """Run an idempotent live read with one bounded transient retry."""
        attempt = 0
        while True:
            try:
                return self._run(factory(), timeout=timeout)
            except TelegramError as exc:
                if attempt >= retries or not exc.retryable or exc.code == "flood_wait":
                    raise
                attempt += 1
                logger.info("retrying Telegram read after transient %s (%s/%s)", exc.code, attempt, retries)
                time.sleep(min(0.25 * attempt, 0.5))

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
            self._connected_at = datetime.now(UTC)
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
            raise _translate_telegram_error(sign_in_exc) from sign_in_exc
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
            raise _translate_telegram_error(sign_in_exc) from sign_in_exc
        return await self._finish_login()

    async def _finish_login(self) -> dict[str, Any]:
        self._login_state = "connected"
        self._connected = True
        self._connected_at = datetime.now(UTC)
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
                logger.debug("abort disconnect failed: %s", type(exc).__name__)
            self._client = None
            self._connected = False

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
                self._connected = False
                self._connected_at = None

    async def _delete_message(self, chat, msg_id: int) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.delete_messages(entity, msg_id, revoke=True)

    async def _edit_message(self, chat, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.edit_message(entity, msg_id, text)
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

    async def _fetch_contacts(self) -> list[Contact]:
        """Read the current contact list from Telegram; no application cache is used."""
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await self._client(GetContactsRequest(hash=0))
        users = list(getattr(result, "users", ()) or ())
        return [_contact_from_telethon(user) for user in users]

    async def _list_contacts(self, limit: int) -> list[Contact]:
        contacts = await self._fetch_contacts()
        contacts.sort(key=lambda item: (_normalize_text(item.name), item.id))
        return contacts[:max(1, limit)]

    async def _get_contact_info(self, contact) -> dict[str, Any]:
        entity = await self._resolve_contact(contact)
        info = _contact_from_telethon(entity).to_dict()
        info["bio"] = str(getattr(entity, "about", "") or "")
        return info

    async def _add_contact(self, phone: str, first_name: str, last_name: str) -> dict[str, Any]:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact
        result = await self._client(ImportContactsRequest([InputPhoneContact(
            client_id=0, phone=str(phone), first_name=str(first_name), last_name=str(last_name))]))
        users = getattr(result, "users", [])
        return {"id": int(users[0].id)} if users else {"added": False}

    async def _delete_contact(self, contact) -> None:
        from telethon.tl.functions.contacts import DeleteContactsRequest
        entity = await self._resolve_contact(contact)
        await self._client(DeleteContactsRequest(id=[entity]))

    async def _block_user(self, contact) -> None:
        from telethon.tl.functions.contacts import BlockRequest
        await self._client(BlockRequest(id=await self._resolve_contact(contact)))

    async def _unblock_user(self, contact) -> None:
        from telethon.tl.functions.contacts import UnblockRequest
        await self._client(UnblockRequest(id=await self._resolve_contact(contact)))

    async def _join_channel(self, channel) -> None:
        from telethon.tl.functions.channels import JoinChannelRequest
        await self._client(JoinChannelRequest(await self._resolve_entity(channel)))

    async def _leave_channel(self, channel) -> None:
        from telethon.tl.functions.channels import LeaveChannelRequest
        await self._client(LeaveChannelRequest(await self._resolve_entity(channel)))

    async def _list_members(self, chat, limit: int, admins: bool) -> list[dict[str, Any]]:
        entity = await self._resolve_entity(chat)
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if admins:
            from telethon.tl.types import ChannelParticipantsAdmins
            kwargs["filter"] = ChannelParticipantsAdmins()
        users = await self._client.get_participants(entity, **kwargs)
        return [{"id": int(u.id), "name": " ".join(p for p in (getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or "") if p),
                 "username": getattr(u, "username", "") or ""} for u in users]

    async def _update_profile(self, first_name: str, last_name: str, about: str) -> None:
        from telethon.tl.functions.account import UpdateProfileRequest
        await self._client(UpdateProfileRequest(first_name=first_name, last_name=last_name, about=about))

    async def _update_username(self, username: str) -> None:
        from telethon.tl.functions.account import UpdateUsernameRequest
        await self._client(UpdateUsernameRequest(username=str(username).lstrip("@")))

    async def _set_profile_photo(self, path: Path) -> None:
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        await self._client(UploadProfilePhotoRequest(file=await self._client.upload_file(str(path))))

    async def _set_online_status(self, online: bool) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest
        await self._client(UpdateStatusRequest(offline=not online))

    async def _resolve_entity(self, target):
        """Resolve a target safely from fresh dialogs and contacts.

        Numeric peer IDs, explicit usernames and phone numbers are handled
        directly.  Display-name matching scans Telegram live on every call,
        ranks exact matches before partial ones and never silently chooses an
        ambiguous recipient.
        """
        cleaned = str(target).strip()
        if not cleaned:
            raise TelegramError("نام یا شناسهٔ چت خالی است")
        normalized = _normalize_text(cleaned)
        compact = normalized.replace(" ", "")
        if compact in _SAVED_MESSAGES_ALIASES:
            return await self._client.get_me()

        if _looks_like_phone(cleaned):
            phone_candidates: dict[tuple[str, int], _ResolvedCandidate] = {}
            query_phones = _phone_variants(cleaned)
            async for dialog in self._client.iter_dialogs(limit=None):
                entity_phone = _phone_variants(getattr(dialog.entity, "phone", ""))
                if query_phones & entity_phone:
                    chat = _chat_from_dialog(dialog)
                    candidate = _candidate_from_dialog(dialog, chat)
                    phone_candidates[_entity_key(candidate.entity)] = candidate
            for contact in await self._fetch_contacts():
                if query_phones & _phone_variants(contact.phone):
                    candidate = _candidate_from_contact(contact)
                    phone_candidates[_entity_key(candidate.entity)] = candidate
            matches = list(phone_candidates.values())
            if len(matches) == 1:
                return matches[0].entity
            if len(matches) > 1:
                raise TelegramError(_ambiguous_target_message(cleaned, matches), code="target_ambiguous")
            try:
                return await self._client.get_entity(cleaned)
            except Exception as exc:
                error = _translate_telegram_error(exc)
                if error.code not in {"peer_invalid", "rpc_error", "telegram_error"}:
                    raise error from exc
                raise TelegramError(
                    f"شماره «{cleaned}» در تلگرام پیدا نشد", code="peer_invalid"
                ) from exc

        if isinstance(target, int) or cleaned.lstrip("-").isdigit():
            try:
                return await self._client.get_entity(int(cleaned))
            except Exception as exc:
                error = _translate_telegram_error(exc)
                if error.code not in {"peer_invalid", "rpc_error", "telegram_error"}:
                    raise error from exc
                raise TelegramError(
                    f"شناسهٔ تلگرام {cleaned} پیدا نشد", code="peer_invalid"
                ) from exc

        if cleaned.startswith("@"):
            try:
                return await self._client.get_entity(cleaned)
            except Exception as exc:
                error = _translate_telegram_error(exc)
                if error.code not in {"peer_invalid", "rpc_error", "telegram_error"}:
                    raise error from exc
                raise TelegramError(
                    f"نام کاربری «{cleaned}» در تلگرام پیدا نشد", code="peer_invalid"
                ) from exc

        candidates: dict[tuple[str, int], tuple[int, _ResolvedCandidate]] = {}

        def add_candidate(priority: int, candidate: _ResolvedCandidate) -> None:
            key = _entity_key(candidate.entity)
            current = candidates.get(key)
            if current is None or priority < current[0]:
                candidates[key] = (priority, candidate)

        async for dialog in self._client.iter_dialogs(limit=None):
            chat = _chat_from_dialog(dialog)
            title = _normalize_text(chat.title)
            username = _normalize_text(chat.username).lstrip("@")
            candidate = _candidate_from_dialog(dialog, chat)
            if username and normalized == username:
                add_candidate(0, candidate)
            elif normalized == title:
                add_candidate(1, candidate)
            elif username and normalized in username:
                add_candidate(2, candidate)
            elif normalized in title:
                add_candidate(3, candidate)

        for contact in await self._fetch_contacts():
            name = _normalize_text(contact.name)
            username = _normalize_text(contact.username).lstrip("@")
            candidate = _candidate_from_contact(contact)
            if username and normalized == username:
                add_candidate(0, candidate)
            elif normalized == name:
                add_candidate(1, candidate)
            elif username and normalized in username:
                add_candidate(2, candidate)
            elif normalized in name:
                add_candidate(3, candidate)

        if candidates:
            best_priority = min(priority for priority, _ in candidates.values())
            best = [candidate for priority, candidate in candidates.values() if priority == best_priority]
            if len(best) == 1:
                return best[0].entity
            raise TelegramError(_ambiguous_target_message(cleaned, best), code="target_ambiguous")

        # A public username may not be present in dialogs or contacts yet.
        try:
            return await self._client.get_entity(cleaned)
        except Exception as exc:
            raise TelegramError(
                f"مقصد «{cleaned}» در چت‌ها، مخاطبین یا نام‌های کاربری تلگرام پیدا نشد",
                code="peer_invalid",
            ) from exc

    async def _resolve_target(self, target) -> dict[str, Any]:
        entity = await self._resolve_entity(target)
        return _entity_summary(entity)

    async def _list_chats(
        self, limit: int, *, kind: str = "all", query: str = "", sort: str = "",
        offset: int = 0, archived: bool | None = None, unread_only: bool = False,
    ) -> list[Chat]:
        """Read live dialogs and apply filters before the result limit.

        Filtered requests deliberately do not pass ``limit`` to Telethon's
        dialog iterator.  Otherwise ``limit=30, kind=private`` would only
        inspect the first 30 mixed dialogs and could return far fewer than
        30 private conversations.  No dialog/message cache is consulted.
        """
        requested = max(1, int(limit))
        normalized_query = _normalize_text(query).lstrip("@")
        filtered = (
            kind != "all" or bool(normalized_query) or offset > 0
            or archived is not None or unread_only
        )
        # unread sorting needs every matching dialog to produce a true global order.
        iterator_limit = None if filtered or sort == "unread" else requested
        chats: list[Chat] = []
        matched = 0

        async for dialog in self._client.iter_dialogs(limit=iterator_limit):
            chat = _chat_from_dialog(dialog)
            if not _chat_matches_kind(chat, kind):
                continue
            if normalized_query:
                searchable = _normalize_text(f"{chat.title} {chat.username or ''}")
                if normalized_query not in searchable:
                    continue
            if archived is not None and chat.archived is not archived:
                continue
            if unread_only and chat.unread_count <= 0:
                continue
            if sort != "unread" and matched < offset:
                matched += 1
                continue
            chats.append(chat)
            matched += 1
            # Dialogs arrive newest-first. Once enough filtered results are
            # collected no older result can displace them, except unread sort.
            if sort != "unread" and len(chats) >= requested:
                break

        if sort == "unread":
            chats.sort(
                key=lambda item: (
                    item.unread_count,
                    item.last_message_date or datetime.min.replace(tzinfo=UTC),
                ),
                reverse=True,
            )
            return chats[offset:offset + requested]
        return chats[:requested]

    async def _send_message(self, chat, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.send_message(entity, text)
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

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
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

    async def _search_messages(self, chat, query: str, limit: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = _marked_peer_id(entity)
        results: list[Message] = []
        async for msg in self._client.iter_messages(entity, search=query, limit=max(1, limit)):
            results.append(_message_from_telethon(msg, chat_id=chat_id))
        return results

    async def _get_me(self) -> dict[str, Any]:
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": getattr(me, "last_name", "") or "",
            "username": getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", ""),
        }

    async def _search_contacts(self, query: str, limit: int) -> list[Contact]:
        normalized_query = _normalize_text(query).lstrip("@")
        query_phones = _phone_variants(query)
        if not normalized_query and not query_phones:
            raise TelegramError("عبارت جست‌وجوی مخاطب خالی است")

        matches: list[tuple[int, Contact]] = []
        for contact in await self._fetch_contacts():
            name = _normalize_text(contact.name)
            username = _normalize_text(contact.username).lstrip("@")
            phones = _phone_variants(contact.phone)
            exact = normalized_query in {name, username} or bool(query_phones & phones)
            partial = (
                bool(normalized_query)
                and (normalized_query in name or normalized_query in username)
            ) or any(q in p or p in q for q in query_phones for p in phones)
            if exact or partial:
                matches.append((0 if exact else 1, contact))

        matches.sort(key=lambda pair: (pair[0], _normalize_text(pair[1].name), pair[1].id))
        return [contact for _, contact in matches[:max(1, limit)]]

    async def _resolve_contact(self, target):
        """Resolve a contact against a fresh Telegram contact snapshot."""
        if isinstance(target, int) or str(target).strip().lstrip("-").isdigit():
            return await self._client.get_entity(int(target))
        query = _normalize_text(target).lstrip("@")
        phone_query = _phone_variants(target)
        exact = []
        partial = []
        for contact in await self._fetch_contacts():
            values = {_normalize_text(contact.name), _normalize_text(contact.username).lstrip("@")}
            phone_match = bool(phone_query & _phone_variants(contact.phone))
            if query in values or phone_match:
                exact.append(contact)
            elif query and any(query in value for value in values if value):
                partial.append(contact)
        candidates = exact or partial
        if not candidates:
            raise TelegramError(f"مخاطب {target!r} پیدا نشد")
        if len(candidates) > 1:
            ids = "، ".join(str(item.id) for item in candidates[:5])
            raise TelegramError(
                f"نام مخاطب «{target}» مبهم است؛ شناسه‌های مطابق: {ids}",
                code="target_ambiguous",
            )
        return await self._client.get_entity(candidates[0].id)

    async def _get_chat_history(self, chat, limit: int, offset_id: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = _marked_peer_id(entity)
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if offset_id:
            kwargs["offset_id"] = int(offset_id)
        out: list[Message] = []
        async for msg in self._client.iter_messages(entity, **kwargs):
            out.append(_message_from_telethon(msg, chat_id=chat_id))
        return out

    async def _get_profile(self, chat, media_dir: Path) -> dict[str, Any]:
        entity = await self._resolve_entity(chat)
        summary = _entity_summary(entity)
        info: dict[str, Any] = {
            "id": summary["id"],
            "name": summary["name"],
            "username": summary["username"],
            "phone": summary.get("phone", ""),
            "kind": summary["kind"],
            "is_group": summary["kind"] in {"group", "supergroup"},
            "is_channel": summary["kind"] == "channel",
            "is_bot": summary["kind"] == "bot",
            "verified": bool(getattr(entity, "verified", False)),
            "deleted": bool(getattr(entity, "deleted", False)),
            "bio": "",
            "members_count": getattr(entity, "participants_count", None),
        }
        try:
            full = await _get_full_entity(self._client, entity)
            if full is not None:
                info["bio"] = str(getattr(full, "about", "") or "")
                count = getattr(full, "participants_count", None)
                if count is not None:
                    info["members_count"] = int(count)
        except Exception as exc:  # noqa: BLE001 - base profile remains useful
            logger.debug("full Telegram profile unavailable: %s", type(exc).__name__)

        photo_path = ""
        if getattr(entity, "photo", None) is not None:
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                filename = media_dir / f"profile_{getattr(entity, 'id', 'unknown')}.jpg"
                await self._client.download_profile_photo(entity, file=str(filename))
                if filename.is_file():
                    photo_path = str(filename)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("profile photo download failed: %s", type(exc).__name__)
        info["photo_path"] = photo_path
        return info

    async def _send_media(self, chat, path: Path, *, caption: str, kind: str) -> Message:
        entity = await self._resolve_entity(chat)
        kwargs: dict[str, Any] = {"caption": caption or ""}
        if kind == "voice":
            kwargs["voice_note"] = True
        elif kind == "video_note":
            kwargs["video_note"] = True
        elif kind == "document":
            kwargs["force_document"] = True
        elif kind == "photo":
            kwargs["force_document"] = False
        # ``audio``/``video``/``sticker``/``animation`` let telethon infer the
        # type from the file content.
        result = await self._client.send_file(entity, str(path), **kwargs)
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

    async def _send_location(self, chat, lat: float, lng: float) -> Message:
        entity = await self._resolve_entity(chat)
        from telethon.tl.types import InputGeoPoint

        geo = InputGeoPoint(lat=lat, long=lng)
        result = await self._client.send_file(entity, geo)
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

    async def _download_media(self, chat, msg_id: int, filename: str, media_dir: Path) -> Path:
        entity = await self._resolve_entity(chat)
        messages = await self._client.get_messages(entity, ids=msg_id)
        if not messages:
            raise TelegramError(f"پیامی با شناسهٔ {msg_id} پیدا نشد", code="message_invalid")
        safe = Path(filename or f"{msg_id}").name
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / safe
        try:
            out = await messages.download_media(file=str(target))
        except Exception as exc:
            raise _translate_telegram_error(exc) from exc
        if out is None:
            raise TelegramError("این پیام مدیا ندارد", code="media_invalid")
        return Path(str(out))

    async def _reply_to(self, chat, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.send_message(entity, text, reply_to=msg_id)
        return _message_from_telethon(result, chat_id=_marked_peer_id(entity))

    async def _forward_message(self, chat, from_chat, msg_id: int) -> Message:
        target = await self._resolve_entity(chat)
        source = await self._resolve_entity(from_chat)
        result = await self._client.forward_messages(target, msg_id, source)
        return _message_from_telethon(result, chat_id=_marked_peer_id(target))

    async def _mark_read(self, chat) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.send_read_acknowledge(entity)

    async def _get_statistics(self) -> dict[str, Any]:
        counts = {"private": 0, "group": 0, "supergroup": 0, "channel": 0, "bot": 0}
        total = unread_chats = total_unread = 0
        async for dialog in self._client.iter_dialogs(limit=None):
            chat = _chat_from_dialog(dialog)
            total += 1
            counts[chat.kind] = counts.get(chat.kind, 0) + 1
            total_unread += chat.unread_count
            if chat.unread_count:
                unread_chats += 1
        return {
            "total_chats": total,
            **{f"{kind}_chats": value for kind, value in counts.items()},
            "unread_chats": unread_chats,
            "total_unread": total_unread,
            "source": "live",
        }

    async def _list_unread_chats(self, limit: int) -> list[Chat]:
        chats = []
        async for dialog in self._client.iter_dialogs(limit=None):
            chat = _chat_from_dialog(dialog)
            if chat.unread_count > 0:
                chats.append(chat)
        chats.sort(key=lambda item: (item.unread_count, _datetime_key(item.last_message_date)), reverse=True)
        return chats[:limit]

    async def _get_chat_statistics(self, chat, limit: int) -> dict[str, Any]:
        entity = await self._resolve_entity(chat)
        types: dict[str, int] = {}
        senders: dict[int, int] = {}
        total = outgoing = 0
        async for raw in self._client.iter_messages(entity, limit=limit):
            message = _message_from_telethon(raw, chat_id=_marked_peer_id(entity))
            total += 1
            outgoing += int(message.is_outgoing)
            types[message.message_type] = types.get(message.message_type, 0) + 1
            if message.sender_id is not None:
                senders[message.sender_id] = senders.get(message.sender_id, 0) + 1
        return {
            "chat": _entity_summary(entity),
            "sampled_messages": total,
            "outgoing": outgoing,
            "incoming": total - outgoing,
            "message_types": dict(sorted(types.items())),
            "top_senders": [
                {"sender_id": sender_id, "messages": count}
                for sender_id, count in sorted(senders.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
            "source": "live",
        }

    async def _export_chat(self, chat, output_dir: Path, *, fmt: str, limit: int) -> Path:
        entity = await self._resolve_entity(chat)
        chat_info = _entity_summary(entity)
        messages = []
        async for raw in self._client.iter_messages(entity, limit=limit):
            messages.append(_message_from_telethon(raw, chat_id=chat_info["id"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w .-]+", "_", chat_info["name"], flags=re.UNICODE).strip(" ._") or str(chat_info["raw_id"])
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        target = output_dir / f"telegram_{safe_name}_{stamp}.{fmt}"
        if fmt == "json":
            payload = {
                "chat": chat_info,
                "exported_at": datetime.now(UTC).isoformat(),
                "message_count": len(messages),
                "messages": [message.to_dict() for message in messages],
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            lines = [f"چت: {chat_info['name']} (id={chat_info['id']})", ""]
            for message in messages:
                lines.append(
                    f"[{message.date.isoformat()}] id={message.id} {message.sender}: "
                    f"{message.text or '[' + message.message_type + ']'}"
                )
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    async def _download_media_batch(
        self, chat, media_dir: Path, *, limit: int, media_types: list[str]
    ) -> list[Path]:
        entity = await self._resolve_entity(chat)
        allowed = {str(item).lower() for item in media_types if str(item).strip()}
        target_dir = media_dir / f"telegram_{abs(_marked_peer_id(entity))}"
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded = []
        async for message in self._client.iter_messages(entity, limit=limit):
            kind = _message_type(message)
            if getattr(message, "media", None) is None or (allowed and kind not in allowed):
                continue
            try:
                output = await self._client.download_media(message, file=str(target_dir))
            except Exception as exc:
                error = _translate_telegram_error(exc)
                if error.code not in {"media_invalid", "message_invalid", "rpc_error"}:
                    raise error from exc
                logger.debug(
                    "batch media download skipped message %s: %s", message.id, error.code
                )
                continue
            if output:
                downloaded.append(Path(str(output)))
        return downloaded

    async def _refresh_summary(self) -> dict[str, Any]:
        statistics = await self._get_statistics()
        contacts = await self._fetch_contacts()
        return {**statistics, "total_contacts": len(contacts), "refreshed_at": datetime.now(UTC).isoformat()}

    async def _resolve_username(self, username: str) -> dict[str, Any]:
        cleaned = str(username or "").strip().lstrip("@")
        if not cleaned:
            raise TelegramError("نام کاربری خالی است")
        entity = await self._client.get_entity(cleaned)
        summary = _entity_summary(entity)
        summary["is_group"] = summary["kind"] in {"group", "supergroup"}
        return summary
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate_telegram_error(exc: Exception) -> TelegramError:
    if isinstance(exc, TelegramError):
        return exc
    name = type(exc).__name__
    lowered = f"{name} {exc}".lower()
    seconds = getattr(exc, "seconds", None)
    if "floodwait" in lowered or "flood_wait" in lowered:
        wait = int(seconds or 0) or None
        suffix = f"؛ {wait} ثانیه دیگر دوباره تلاش کنید" if wait else ""
        return TelegramError(
            "تلگرام به‌دلیل تعداد زیاد درخواست‌ها موقتاً محدود کرده است" + suffix,
            code="flood_wait", retryable=False, retry_after=wait,
        )
    if name in {"TimeoutError", "ServerError", "TimedOutError"} or "timed out" in lowered:
        return TelegramError(
            "پاسخ تلگرام به‌موقع دریافت نشد؛ اتصال را بررسی و دوباره تلاش کنید",
            code="timeout", retryable=True,
        )
    if isinstance(exc, (ConnectionError, OSError)) or any(
        token in lowered for token in (
            "connection to telegram failed", "connection reset", "connection aborted",
            "network is unreachable", "name resolution", "getaddrinfo", "temporarily unavailable",
            "incompleteread", "server closed the connection", "server disconnected",
        )
    ):
        return TelegramError(
            "ارتباط با سرور تلگرام برقرار نشد؛ اینترنت و VPN/فیلترشکن را بررسی کنید",
            code="network", retryable=True,
        )
    if any(token in name for token in ("SessionRevoked", "AuthKeyUnregistered", "AuthKeyInvalid")):
        return TelegramError(
            "سشن تلگرام باطل یا از دستگاه‌ها خارج شده است؛ اکانت را دوباره متصل کنید",
            code="session_revoked",
        )
    if any(token in name for token in ("AuthKeyDuplicated", "Unauthorized")):
        return TelegramError(
            "مجوز این سشن تلگرام معتبر نیست یا دسترسی لازم وجود ندارد",
            code="authorization_required",
        )
    if any(token in name for token in ("UserDeactivated", "UserBanned", "PhoneNumberBanned")):
        return TelegramError(
            "این حساب تلگرام غیرفعال یا محدود شده است",
            code="account_restricted",
        )
    if "PrivacyRestricted" in name or "privacy" in lowered:
        return TelegramError(
            "تنظیمات حریم خصوصی کاربر اجازهٔ این عملیات را نمی‌دهد",
            code="privacy_restricted",
        )
    if any(token in name for token in ("ChatAdminRequired", "AdminRankInvalid", "RightForbidden")):
        return TelegramError(
            "برای این عملیات دسترسی مدیر گروه یا کانال لازم است",
            code="admin_required",
        )
    if any(token in name for token in (
        "ChatWriteForbidden", "UserIsBlocked", "YouBlockedUser", "ChatRestricted",
        "ChannelPrivate", "UserNotMutualContact", "UserNotParticipant",
    )):
        return TelegramError(
            "ارسال یا دسترسی به این مقصد توسط تلگرام مجاز نیست",
            code="write_forbidden",
        )
    if any(token in name for token in (
        "PeerIdInvalid", "UserIdInvalid", "ChatIdInvalid", "ChannelInvalid",
        "UsernameNotOccupied", "UsernameInvalid",
    )):
        return TelegramError(
            "شناسه، نام کاربری یا مقصد تلگرام معتبر نیست یا دیگر وجود ندارد",
            code="peer_invalid",
        )
    if any(token in name for token in ("MessageIdInvalid", "MessageNotModified", "MessageDeleteForbidden")):
        return TelegramError(
            "پیام موردنظر وجود ندارد یا انجام این عملیات روی آن مجاز نیست",
            code="message_invalid",
        )
    if any(token in name for token in ("FileReferenceExpired", "FileIdInvalid", "MediaInvalid")):
        return TelegramError(
            "مرجع فایل یا رسانه منقضی یا نامعتبر شده است؛ اطلاعات را تازه کنید",
            code="media_invalid", retryable=True,
        )
    if "rpcerror" in lowered or name.endswith("Error"):
        return TelegramError(
            f"عملیات تلگرام ناموفق بود ({name})",
            code="rpc_error",
        )
    return TelegramError("عملیات تلگرام ناموفق بود", code="telegram_error")


_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TEXT_TRANSLATION = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})


def _normalize_text(value: Any) -> str:
    """Normalize Persian/Arabic text, digits, whitespace and zero-width marks."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_DIGIT_TRANSLATION)
    text = text.translate(_TEXT_TRANSLATION).replace("\u200c", " ").replace("\u200f", " ")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _phone_variants(value: Any) -> set[str]:
    normalized = _normalize_text(value)
    # Do not interpret digits embedded in a person's name/username as a phone query.
    if any(ch.isalpha() for ch in normalized):
        return set()
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if not digits:
        return set()
    variants = {digits}
    if digits.startswith("00"):
        variants.add(digits[2:])
    if digits.startswith("09") and len(digits) == 11:
        variants.add("98" + digits[1:])
    if digits.startswith("98") and len(digits) == 12:
        variants.add("0" + digits[2:])
    return variants


def _contact_from_telethon(user: Any) -> Contact:
    status_obj = getattr(user, "status", None)
    status_name = type(status_obj).__name__.removeprefix("UserStatus").lower() if status_obj else "unknown"
    last_seen = getattr(status_obj, "was_online", None)
    return Contact(
        id=int(user.id),
        first_name=str(getattr(user, "first_name", "") or ""),
        last_name=str(getattr(user, "last_name", "") or ""),
        username=str(getattr(user, "username", "") or ""),
        phone=str(getattr(user, "phone", "") or ""),
        is_contact=bool(getattr(user, "contact", True)),
        is_mutual_contact=bool(getattr(user, "mutual_contact", False)),
        is_bot=bool(getattr(user, "bot", False)),
        verified=bool(getattr(user, "verified", False)),
        deleted=bool(getattr(user, "deleted", False)),
        status=status_name,
        last_seen=last_seen,
        entity=user,
    )


def _dialog_folder_id(dialog: Any) -> int | None:
    value = getattr(dialog, "folder_id", None)
    if value is None:
        value = getattr(getattr(dialog, "dialog", None), "folder_id", None)
    return int(value) if value is not None else None


def _dialog_is_muted(dialog: Any) -> bool:
    settings = getattr(dialog, "notify_settings", None)
    if settings is None:
        settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None:
        return False
    if isinstance(mute_until, datetime):
        now = datetime.now(mute_until.tzinfo or UTC)
        return mute_until > now
    return bool(mute_until)


def _chat_from_dialog(dialog: Any) -> Chat:
    entity = dialog.entity
    title = getattr(entity, "title", None) or " ".join(
        part for part in (
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
        ) if part
    ) or str(dialog.id)
    type_name = type(entity).__name__.lower()
    is_bot = bool(getattr(entity, "bot", False))
    is_supergroup = bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False))
    is_channel_entity = type_name == "channel" or hasattr(entity, "broadcast")
    is_channel = bool(is_channel_entity and not is_supergroup)
    is_basic_group = bool(type_name == "chat" or getattr(entity, "is_group", False))
    is_group = bool(is_basic_group or is_supergroup)
    is_private = bool(not is_bot and not is_group and not is_channel)
    message = getattr(dialog, "message", None)
    message_text = getattr(message, "message", None) if message is not None else None
    message_date = getattr(message, "date", None) or getattr(dialog, "date", None)
    folder_id = _dialog_folder_id(dialog)
    members = getattr(entity, "participants_count", None)
    return Chat(
        id=int(dialog.id),
        title=str(title),
        username=getattr(entity, "username", None),
        is_group=is_group,
        is_channel=is_channel,
        is_bot=is_bot,
        is_private=is_private,
        is_supergroup=is_supergroup,
        is_forum=bool(getattr(entity, "forum", False)),
        verified=bool(getattr(entity, "verified", False)),
        pinned=bool(getattr(dialog, "pinned", False)),
        muted=_dialog_is_muted(dialog),
        archived=folder_id == 1,
        folder_id=folder_id,
        members_count=int(members) if members is not None else None,
        phone=str(getattr(entity, "phone", "") or "") or None,
        deleted=bool(getattr(entity, "deleted", False)),
        last_message=(str(message_text)[:140] if message_text else None),
        last_message_date=message_date,
        unread_count=int(getattr(dialog, "unread_count", 0) or 0),
    )


def _chat_matches_kind(chat: Chat, kind: str) -> bool:
    if kind == "all":
        return True
    if kind == "group":
        return chat.is_group
    return bool(getattr(chat, f"is_{kind}", False))


def _datetime_key(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


_SAVED_MESSAGES_ALIASES = {
    "saved",
    "savedmessages",
    "savedmessage",
    "پیامهایذخیرهشده",
    "پیامذخیرهشده",
    "ذخیرهشده",
    "خودم",
}


def _looks_like_phone(value: Any) -> bool:
    text = _normalize_text(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    starts_like_phone = (
        str(value).strip().startswith("+")
        or digits.startswith("0")
        or (digits.startswith("98") and len(digits) == 12)
    )
    return len(digits) >= 7 and starts_like_phone and not any(ch.isalpha() for ch in text)


def _entity_key(entity: Any) -> tuple[str, int]:
    return (type(entity).__name__.lower(), int(getattr(entity, "id", 0)))


def _marked_peer_id(entity: Any) -> int:
    try:
        from telethon import utils

        return int(utils.get_peer_id(entity))
    except Exception:  # noqa: BLE001 - test doubles and unusual entities
        entity_id = int(getattr(entity, "id", 0))
        type_name = type(entity).__name__.lower()
        is_channel = type_name == "channel" or hasattr(entity, "broadcast")
        if is_channel:
            return -(1_000_000_000_000 + entity_id)
        if type_name == "chat":
            return -entity_id
        return entity_id


def _entity_kind(entity: Any) -> str:
    if bool(getattr(entity, "bot", False)):
        return "bot"
    if bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)):
        return "supergroup"
    type_name = type(entity).__name__.lower()
    if type_name == "channel" or hasattr(entity, "broadcast"):
        return "channel"
    if type_name == "chat" or bool(getattr(entity, "is_group", False)):
        return "group"
    return "private"


def _entity_title(entity: Any) -> str:
    return str(getattr(entity, "title", None) or " ".join(
        part for part in (
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
        ) if part
    ) or getattr(entity, "username", None) or getattr(entity, "id", "?"))


def _entity_summary(entity: Any) -> dict[str, Any]:
    return {
        "id": _marked_peer_id(entity),
        "raw_id": int(getattr(entity, "id", 0)),
        "name": _entity_title(entity),
        "username": str(getattr(entity, "username", "") or ""),
        "phone": str(getattr(entity, "phone", "") or ""),
        "kind": _entity_kind(entity),
        "is_bot": bool(getattr(entity, "bot", False)),
        "verified": bool(getattr(entity, "verified", False)),
        "deleted": bool(getattr(entity, "deleted", False)),
    }


def _candidate_from_dialog(dialog: Any, chat: Chat) -> _ResolvedCandidate:
    return _ResolvedCandidate(
        entity=dialog.entity,
        id=chat.id,
        title=chat.title,
        username=str(chat.username or ""),
        kind=chat.kind,
        source="chat",
    )


def _candidate_from_contact(contact: Contact) -> _ResolvedCandidate:
    return _ResolvedCandidate(
        entity=contact.entity,
        id=contact.id,
        title=contact.name or str(contact.id),
        username=contact.username,
        kind="bot" if contact.is_bot else "private",
        source="contact",
    )


def _ambiguous_target_message(target: str, candidates: list[_ResolvedCandidate]) -> str:
    rendered = []
    for item in sorted(candidates, key=lambda candidate: (candidate.title, candidate.id))[:8]:
        username = f", @{item.username.lstrip('@')}" if item.username else ""
        rendered.append(f"{item.title} (id={item.id}, نوع={item.kind}{username})")
    return (
        f"مقصد «{target}» مبهم است و خودکار انتخاب نشد. "
        f"یکی از شناسه‌ها یا نام‌های کاربری دقیق را استفاده کنید: {'؛ '.join(rendered)}"
    )


async def _get_full_entity(client: Any, entity: Any) -> Any | None:
    type_name = type(entity).__name__.lower()
    if type_name == "user" or (hasattr(entity, "first_name") and not hasattr(entity, "title")):
        from telethon.tl.functions.users import GetFullUserRequest

        result = await client(GetFullUserRequest(entity))
        return getattr(result, "full_user", None)
    if type_name == "channel" or hasattr(entity, "broadcast"):
        from telethon.tl.functions.channels import GetFullChannelRequest

        result = await client(GetFullChannelRequest(entity))
        return getattr(result, "full_chat", None)
    if type_name == "chat" or bool(getattr(entity, "is_group", False)):
        from telethon.tl.functions.messages import GetFullChatRequest

        result = await client(GetFullChatRequest(int(entity.id)))
        return getattr(result, "full_chat", None)
    return None


def _message_type(msg: Any) -> str:
    for attribute, kind in (
        ("poll", "poll"),
        ("geo", "location"),
        ("contact", "contact"),
        ("photo", "photo"),
        ("sticker", "sticker"),
        ("gif", "gif"),
        ("voice", "voice"),
        ("video_note", "video_note"),
        ("video", "video"),
        ("audio", "audio"),
        ("document", "document"),
    ):
        if getattr(msg, attribute, None) is not None:
            return kind
    return "other" if getattr(msg, "media", None) is not None else "text"


def _message_from_telethon(msg, *, chat_id: int) -> Message:
    sender_obj = getattr(msg, "sender", None)
    sender = "کانال" if sender_obj is None else (
        getattr(sender_obj, "username", None)
        or getattr(sender_obj, "first_name", None)
        or "?"
    )
    reply = getattr(msg, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None) or getattr(msg, "reply_to_msg_id", None)
    return Message(
        id=int(msg.id),
        chat_id=int(chat_id),
        sender_id=getattr(msg, "sender_id", None),
        sender=str(sender),
        text=str(getattr(msg, "message", "") or ""),
        date=msg.date,
        is_outgoing=bool(getattr(msg, "out", False)),
        message_type=_message_type(msg),
        reply_to_msg_id=int(reply_id) if reply_id is not None else None,
        forwards=int(getattr(msg, "forwards", 0) or 0),
        views=int(getattr(msg, "views", 0) or 0),
        has_media=getattr(msg, "media", None) is not None,
    )
