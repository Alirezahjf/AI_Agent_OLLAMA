"""Telethon wrapper for the local assistant.

The wrapper is intentionally small: it owns a single ``TelegramClient``
singleton and exposes a handful of methods that the agent loop calls.
A separate ``connect()`` step is required before any other method.
"""

from __future__ import annotations

import asyncio
import os
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("telegram")


class TelegramError(AssistantError):
    """A user-facing failure from the personal Telegram client."""


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
        self._manual_disconnect = True
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

    def list_chats(self, limit: int = 30, kind: str = "all", query: str = "", sort: str = "") -> list[Chat]:
        kind = str(kind or "all").lower()
        if kind not in {"private", "group", "supergroup", "channel", "bot", "all"}:
            raise TelegramError(
                "نوع چت باید private، group، supergroup، channel، bot یا all باشد"
            )
        sort = str(sort or "").lower()
        if sort not in {"", "unread", "recent"}:
            raise TelegramError("مرتب‌سازی باید recent یا unread باشد")
        return self._run(
            self._list_chats(max(1, int(limit or 30)), kind=kind, query=query, sort=sort)
        )

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

    def search_contacts(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._run(
            self._search_contacts(query, max(1, int(limit or 30)))
        )]

    def get_chat_history(self, chat: str | int, limit: int = 30, offset_id: int = 0) -> list[Message]:
        return self._run(self._get_chat_history(chat, limit, offset_id))

    def get_profile(self, chat: str | int, media_dir: Path) -> dict[str, Any]:
        return self._run(self._get_profile(chat, media_dir))

    def send_media(self, chat: str | int, path: str | os.PathLike, caption: str = "",
                   *, kind: str = "document") -> Message:
        target = Path(path).expanduser()
        if not target.is_file():
            raise TelegramError(f"فایل پیدا نشد: {target}")
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
        return self._run(self._resolve_username(username))

    def delete_message(self, chat: str | int, msg_id: int) -> None:
        self._run(self._delete_message(chat, int(msg_id)))

    def edit_message(self, chat: str | int, msg_id: int, text: str) -> Message:
        return self._run(self._edit_message(chat, int(msg_id), text))

    def list_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._run(
            self._list_contacts(max(1, int(limit or 100)))
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
                self._connected_at = None

    async def _delete_message(self, chat, msg_id: int) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.delete_messages(entity, msg_id, revoke=True)

    async def _edit_message(self, chat, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.edit_message(entity, msg_id, text)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _fetch_contacts(self) -> list[Contact]:
        """Read the current contact list from Telegram; no application cache is used."""
        from telethon.tl.functions.contacts import GetContactsRequest

        try:
            result = await self._client(GetContactsRequest(hash=0))
        except Exception as exc:
            raise TelegramError(f"دریافت مخاطبین از تلگرام ناموفق بود: {exc}") from exc
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
        """Resolve a chat target; special-cases the user's own «Saved Messages»."""
        if isinstance(target, int):
            return await self._client.get_entity(target)
        cleaned = str(target).strip()
        if not cleaned:
            raise TelegramError("نام چت خالی است")
        lowered = cleaned.lower().replace(" ", "")
        if lowered in {"saved", "savedmessages", "خودم", "ذخیره‌شده"}:
            return await self._client.get_me()
        try:
            return await self._client.get_entity(cleaned)
        except Exception as exc:
            raise TelegramError(f"چت {target!r} پیدا نشد: {exc}") from exc

    async def _list_chats(
        self, limit: int, *, kind: str = "all", query: str = "", sort: str = ""
    ) -> list[Chat]:
        """Read live dialogs and apply filters before the result limit.

        Filtered requests deliberately do not pass ``limit`` to Telethon's
        dialog iterator.  Otherwise ``limit=30, kind=private`` would only
        inspect the first 30 mixed dialogs and could return far fewer than
        30 private conversations.  No dialog/message cache is consulted.
        """
        requested = max(1, int(limit))
        normalized_query = _normalize_text(query).lstrip("@")
        filtered = kind != "all" or bool(normalized_query)
        # unread sorting needs every matching dialog to produce a true global order.
        iterator_limit = None if filtered or sort == "unread" else requested
        chats: list[Chat] = []

        async for dialog in self._client.iter_dialogs(limit=iterator_limit):
            chat = _chat_from_dialog(dialog)
            if not _chat_matches_kind(chat, kind):
                continue
            if normalized_query:
                searchable = _normalize_text(f"{chat.title} {chat.username or ''}")
                if normalized_query not in searchable:
                    continue
            chats.append(chat)
            # Dialogs arrive newest-first.  Once enough filtered results are
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
        return chats[:requested]

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
            raise TelegramError(f"نام مخاطب «{target}» مبهم است؛ شناسه‌های مطابق: {ids}")
        return await self._client.get_entity(candidates[0].id)

    async def _get_chat_history(self, chat, limit: int, offset_id: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = getattr(entity, "id", 0)
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if offset_id:
            kwargs["offset_id"] = int(offset_id)
        out: list[Message] = []
        async for msg in self._client.iter_messages(entity, **kwargs):
            out.append(_message_from_telethon(msg, chat_id=chat_id))
        return out

    async def _get_profile(self, chat, media_dir: Path) -> dict[str, Any]:
        entity = await self._resolve_entity(chat)
        info: dict[str, Any] = {
            "id": entity.id,
            "name": getattr(entity, "title", None) or " ".join(
                p for p in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if p
            ) or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                             or getattr(entity, "is_group", False)),
        }
        if not info["is_group"]:
            info["phone"] = getattr(entity, "phone", "") or ""
            about = await self._client.get_entity(entity)
            info["bio"] = getattr(about, "about", "") or ""
        photo_path = ""
        if getattr(entity, "photo", None) is not None:
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                filename = media_dir / f"profile_{entity.id}.jpg"
                await self._client.download_profile_photo(entity, file=str(filename))
                if filename.is_file():
                    photo_path = str(filename)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("profile photo download failed: %s", exc)
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
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _send_location(self, chat, lat: float, lng: float) -> Message:
        entity = await self._resolve_entity(chat)
        from telethon.tl.types import InputGeoPoint

        geo = InputGeoPoint(lat=lat, long=lng)
        result = await self._client.send_file(entity, geo)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _download_media(self, chat, msg_id: int, filename: str, media_dir: Path) -> Path:
        entity = await self._resolve_entity(chat)
        messages = await self._client.get_messages(entity, ids=msg_id)
        if not messages:
            raise TelegramError(f"پیامی با شناسهٔ {msg_id} پیدا نشد")
        safe = Path(filename or f"{msg_id}").name
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / safe
        try:
            out = await messages.download_media(file=str(target))
        except Exception as exc:
            raise TelegramError(f"دانلود مدیا ناموفق بود: {exc}") from exc
        if out is None:
            raise TelegramError("این پیام مدیا ندارد")
        return Path(str(out))

    async def _reply_to(self, chat, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.send_message(entity, text, reply_to=msg_id)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _forward_message(self, chat, from_chat, msg_id: int) -> Message:
        target = await self._resolve_entity(chat)
        source = await self._resolve_entity(from_chat)
        result = await self._client.forward_messages(target, msg_id, source)
        return _message_from_telethon(result, chat_id=getattr(target, "id", 0))

    async def _mark_read(self, chat) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.send_read_acknowledge(entity)

    async def _resolve_username(self, username: str) -> dict[str, Any]:
        cleaned = str(username or "").strip().lstrip("@")
        if not cleaned:
            raise TelegramError("نام کاربری خالی است")
        entity = await self._client.get_entity(cleaned)
        return {
            "id": entity.id,
            "name": getattr(entity, "title", None) or " ".join(
                p for p in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if p
            ) or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                             or getattr(entity, "is_group", False)),
        }
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _message_from_telethon(msg, *, chat_id: int) -> Message:
    sender = "کانال" if getattr(msg, "sender", None) is None else "?"
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
