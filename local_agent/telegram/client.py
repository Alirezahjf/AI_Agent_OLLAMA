"""Telethon wrapper for the local assistant — v2 (enriched).

The wrapper owns a ``TelegramClient`` singleton per account and exposes
a rich set of methods that the agent loop calls.  A separate ``connect()``
step is required before any other method.

Compared to v1 this module adds:
  * In-session dialog cache for fast ``list_chats`` / ``resolve_entity``.
  * Fuzzy entity resolution: int IDs, @usernames, +phone numbers, and
    partial name matches against the cache — no more "cannot find entity".
  * Richer Chat model: members_count, is_muted, phone, bio, description.
  * FloodWaitError-aware send / forward (auto-sleep and retry).
  * ``search_contacts`` with a dialog-based fallback when the contacts
    API returns fewer results than expected.
  * ``get_statistics`` for an at-a-glance account overview.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
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
    is_channel: bool = False
    is_bot: bool = False
    is_private: bool = False
    is_forum: bool = False
    verified: bool = False
    pinned: bool = False
    members_count: int = 0
    is_muted: bool = False
    phone: str = ""
    bio: str = ""
    description: str = ""
    last_message_date: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "username": self.username,
            "is_group": self.is_group,
            "is_channel": self.is_channel,
            "is_bot": self.is_bot,
            "is_private": self.is_private,
            "is_forum": self.is_forum,
            "verified": self.verified,
            "pinned": self.pinned,
            "members_count": self.members_count,
            "is_muted": self.is_muted,
            "phone": self.phone,
            "bio": self.bio,
            "description": self.description,
            "last_message": self.last_message,
            "last_message_date": self.last_message_date.isoformat() if self.last_message_date else None,
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
    is_reply: bool = False
    reply_to_msg_id: int | None = None
    forwards: int = 0
    views: int = 0
    media_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "sender": self.sender,
            "text": self.text,
            "date": self.date.isoformat(),
            "is_outgoing": self.is_outgoing,
            "is_reply": self.is_reply,
            "reply_to_msg_id": self.reply_to_msg_id,
            "forwards": self.forwards,
            "views": self.views,
            "media_type": self.media_type,
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
        # In-session dialog cache: id -> Chat
        self._dialogs_cache: dict[int, Chat] = {}

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
        """Connect and (if needed) complete the interactive login."""
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
            self._dialogs_cache.clear()

    # ----------------------------------------------------------- Actions

    def list_chats(self, limit: int = 30, kind: str = "all", query: str = "", sort: str = "") -> list[Chat]:
        kind = str(kind or "all").lower()
        if kind not in {"private", "group", "channel", "bot", "all"}:
            raise TelegramError("نوع چت باید private، group، channel، bot یا all باشد")
        return self._run(self._list_chats(limit, kind=kind, query=query, sort=sort))

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
        return self._run(self._search_contacts(query, limit))

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
        return self._run(self._list_contacts(limit))

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

    def get_statistics(self) -> dict[str, Any]:
        """Return an at-a-glance overview of the account."""
        return self._run(self._get_statistics())

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
            self._login_state = "connected"
            self._connected = True
            self._connected_at = datetime.now()
            me = await client.get_me()
            # Pre-populate the dialog cache so the first list_chats is fast.
            asyncio.ensure_future(self._refresh_dialogs_cache(limit=500))
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
        self._connected_at = datetime.now()
        me = await self._get_me()
        # Pre-populate the dialog cache in the background.
        asyncio.ensure_future(self._refresh_dialogs_cache(limit=500))
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

    # -------------------------------------------------- Dialog cache

    async def _refresh_dialogs_cache(self, limit: int = 500) -> None:
        """(Re)populate the in-session dialog cache from the server.

        This runs once after login and again whenever ``list_chats`` is
        called with a large limit, so later ``resolve_entity`` / search
        calls hit the cache instead of issuing slow API calls.
        """
        try:
            new_cache: dict[int, Chat] = {}
            async for dialog in self._client.iter_dialogs(limit=max(1, limit)):
                chat = self._dialog_to_chat(dialog)
                new_cache[chat.id] = chat
            self._dialogs_cache.clear()
            self._dialogs_cache.update(new_cache)
            logger.debug("dialog cache refreshed: %d chats", len(self._dialogs_cache))
        except Exception as exc:  # noqa: BLE001 - cache is best-effort
            logger.debug("dialog cache refresh failed: %s", exc)

    @staticmethod
    def _dialog_to_chat(dialog) -> Chat:
        """Extract a :class:`Chat` from a Telethon dialog, safely."""
        entity = dialog.entity
        title = getattr(entity, "title", None) or " ".join(
            p for p in (getattr(entity, "first_name", "") or "",
                        getattr(entity, "last_name", "") or "") if p
        ) or "?"
        username = getattr(entity, "username", None)
        type_name = type(entity).__name__.lower()
        is_channel = type_name == "channel" and not bool(getattr(entity, "megagroup", False))
        is_group = bool(
            getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
            or getattr(entity, "is_group", False) or type_name == "chat"
        )
        is_bot = bool(type_name == "user" and getattr(entity, "bot", False))
        is_private = bool(type_name == "user" and not is_bot)

        # members_count
        members_count = int(getattr(entity, "participants_count", 0) or 0)

        # muted — safe extraction
        is_muted = False
        try:
            ns = getattr(dialog, "notify_settings", None)
            if ns is None:
                ns = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
            if ns:
                mute_until = getattr(ns, "mute_until", None)
                is_muted = mute_until is not None
        except Exception:
            is_muted = False

        # phone / bio for private chats
        phone = getattr(entity, "phone", "") or ""
        bio = getattr(entity, "about", "") or ""
        description = getattr(entity, "about", "") or ""

        # last message date
        last_msg_date = None
        if dialog.message is not None:
            last_msg_date = getattr(dialog.message, "date", None)

        return Chat(
            id=int(dialog.id),
            title=str(title),
            username=username,
            is_group=is_group,
            is_channel=is_channel,
            is_bot=is_bot,
            is_private=is_private,
            is_forum=bool(getattr(entity, "forum", False)),
            verified=bool(getattr(entity, "verified", False)),
            pinned=bool(getattr(dialog, "pinned", False)),
            last_message=((dialog.message.message or "")[:140] if dialog.message is not None else None),
            unread_count=int(dialog.unread_count or 0),
            members_count=members_count,
            is_muted=is_muted,
            phone=phone,
            bio=bio,
            description=description,
            last_message_date=last_msg_date,
        )

    # -------------------------------------------------- Resolve entity

    async def _resolve_entity(self, target):
        """Resolve a chat target with fuzzy fallback against the cache.

        Accepts:
          * ``int`` — direct entity ID
          * ``str`` starting with ``+`` — phone number
          * ``str`` starting with ``@`` — username
          * ``str`` that is a numeric string — treated as int
          * any other ``str`` — fuzzy match against cache (title, username,
            phone, name), then ``get_entity`` as last resort
        * Special names: ``saved``, ``savedmessages``, ``خودم``
        """
        client = self._client

        # --- int ---
        if isinstance(target, int):
            try:
                return await client.get_entity(target)
            except Exception:
                pass
            # Fallback: the cache may hold this ID even when get_entity fails
            # (e.g. for users we have dialogs with but haven't resolved).
            if target in self._dialogs_cache:
                try:
                    return await client.get_entity(target)
                except Exception as exc:
                    raise TelegramError(f"چت با شناسهٔ {target} پیدا نشد: {exc}") from exc
            raise TelegramError(f"چت با شناسهٔ عددی {target} پیدا نشد")

        cleaned = str(target).strip()
        if not cleaned:
            raise TelegramError("نام چت خالی است")

        # --- Saved Messages ---
        lowered = cleaned.lower().replace(" ", "")
        if lowered in {"saved", "savedmessages", "خودم", "ذخیره‌شده"}:
            return await client.get_me()

        # --- numeric string → int ---
        try:
            numeric_id = int(cleaned)
            try:
                return await client.get_entity(numeric_id)
            except Exception:
                pass
        except (ValueError, TypeError):
            pass

        # --- phone number ---
        if cleaned.startswith("+"):
            try:
                return await client.get_entity(cleaned)
            except Exception:
                pass

        # --- @username ---
        if cleaned.startswith("@"):
            try:
                return await client.get_entity(cleaned)
            except Exception:
                pass
            # Try without @
            try:
                return await client.get_entity(cleaned.lstrip("@"))
            except Exception:
                pass

        # --- exact match in cache (title, username, phone) ---
        target_lower = cleaned.lower()
        # Pass 1: exact match
        for chat in self._dialogs_cache.values():
            if chat.title.lower() == target_lower:
                try:
                    return await client.get_entity(chat.id)
                except Exception:
                    continue
            if chat.username and chat.username.lower() == target_lower:
                try:
                    return await client.get_entity(chat.id)
                except Exception:
                    continue
            if chat.phone and chat.phone == cleaned.lstrip("+"):
                try:
                    return await client.get_entity(chat.id)
                except Exception:
                    continue

        # Pass 2: partial / fuzzy match (substring in title, username, phone, name)
        best_match: Chat | None = None
        best_score = 0
        for chat in self._dialogs_cache.values():
            score = 0
            title_lower = chat.title.lower()
            if target_lower in title_lower:
                # Longer overlap → better score
                score = max(score, len(target_lower) / max(len(title_lower), 1) * 100)
            if chat.username and target_lower in chat.username.lower():
                score = max(score, 80)
            if chat.phone and target_lower in chat.phone:
                score = max(score, 70)
            if score > best_score:
                best_score = score
                best_match = chat

        if best_match and best_score >= 30:
            try:
                return await client.get_entity(best_match.id)
            except Exception:
                pass

        # --- direct get_entity as last resort ---
        try:
            return await client.get_entity(cleaned)
        except Exception as exc:
            # Build a helpful error with suggestions from the cache
            suggestions = []
            for chat in list(self._dialogs_cache.values())[:500]:
                if target_lower in chat.title.lower():
                    suggestions.append(f"«{chat.title}» (id={chat.id})")
                    if len(suggestions) >= 3:
                        break
            hint = ""
            if suggestions:
                hint = " — شاید منظور شما: " + "، ".join(suggestions)
            raise TelegramError(f"چت «{cleaned}» پیدا نشد{hint}") from exc

    # -------------------------------------------------- List chats

    async def _list_chats(self, limit: int, *, kind: str = "all",
                          query: str = "", sort: str = "") -> list[Chat]:
        # For large requests, refresh the cache first so subsequent calls are fast.
        effective_limit = max(1, limit)
        if effective_limit >= 100 and not self._dialogs_cache:
            await self._refresh_dialogs_cache(limit=effective_limit)

        # If the cache is populated and the request is within its range, use it.
        if self._dialogs_cache and effective_limit <= len(self._dialogs_cache):
            chats = list(self._dialogs_cache.values())
        else:
            # Fetch fresh from the server and update the cache.
            chats = []
            async for dialog in self._client.iter_dialogs(limit=effective_limit):
                chat = self._dialog_to_chat(dialog)
                self._dialogs_cache[chat.id] = chat
                chats.append(chat)

        # Filter by kind
        if kind != "all":
            chats = [c for c in chats if getattr(c, f"is_{kind}", False)]

        # Filter by query (fuzzy: title, username, phone)
        if query:
            q = query.lower()
            filtered = []
            for c in chats:
                haystack = f"{c.title} {c.username or ''} {c.phone} {c.bio}".lower()
                if q in haystack:
                    filtered.append(c)
            chats = filtered

        # Sort
        if sort == "unread":
            chats.sort(key=lambda item: item.unread_count, reverse=True)
        elif sort == "recent":
            chats.sort(key=lambda item: item.last_message_date or datetime.min, reverse=True)
        elif sort == "name":
            chats.sort(key=lambda item: item.title.lower())

        return chats[:effective_limit]

    # -------------------------------------------------- Contacts

    async def _list_contacts(self, limit: int) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await self._client(GetContactsRequest(hash=0))
        users = getattr(result, "users", []) or []
        out = []
        for user in users[:max(1, limit)]:
            last_seen = _extract_last_seen(user)
            out.append({
                "id": int(user.id),
                "first_name": getattr(user, "first_name", "") or "",
                "last_name": getattr(user, "last_name", "") or "",
                "username": getattr(user, "username", "") or "",
                "phone": getattr(user, "phone", "") or "",
                "is_bot": bool(getattr(user, "bot", False)),
                "is_contact": bool(getattr(user, "contact", False)),
                "is_mutual_contact": bool(getattr(user, "mutual_contact", False)),
                "last_seen": last_seen,
            })
        return out

    async def _get_contact_info(self, contact) -> dict[str, Any]:
        entity = await self._resolve_entity(contact)
        last_seen = _extract_last_seen(entity)
        return {
            "id": int(entity.id),
            "first_name": getattr(entity, "first_name", "") or "",
            "last_name": getattr(entity, "last_name", "") or "",
            "username": getattr(entity, "username", "") or "",
            "phone": getattr(entity, "phone", "") or "",
            "is_bot": bool(getattr(entity, "bot", False)),
            "bio": getattr(entity, "about", "") or "",
            "last_seen": last_seen,
        }

    async def _search_contacts(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search contacts with a dialog-based fallback.

        Primary: ``GetContactsRequest`` — the official contacts list.
        Fallback: private chats from the dialog cache — many users chat
        with people who are not in their phone contacts.
        """
        q = str(query or "").strip()
        if not q:
            raise TelegramError("عبارت جست‌وجوی مخاطب خالی است")
        q_lower = q.lower()
        maximum = max(1, limit)
        results: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        # --- Primary: official contacts ---
        try:
            from telethon.tl.functions.contacts import GetContactsRequest
            contact_result = await self._client(GetContactsRequest(hash=0))
            users = getattr(contact_result, "users", []) or []
            for user in users:
                name = " ".join(
                    p for p in (getattr(user, "first_name", ""),
                                getattr(user, "last_name", "")) if p
                )
                username = getattr(user, "username", "") or ""
                phone = getattr(user, "phone", "") or ""
                haystack = f"{name} {username} {phone}".lower()
                if q_lower in haystack:
                    seen_ids.add(int(user.id))
                    results.append({
                        "id": int(user.id),
                        "name": name,
                        "username": username,
                        "phone": phone,
                        "source": "contacts",
                    })
                    if len(results) >= maximum:
                        break
        except Exception as exc:  # noqa: BLE001 - fallback below
            logger.debug("GetContactsRequest failed: %s", exc)

        # --- Fallback: private chats from dialog cache ---
        if len(results) < maximum:
            # Ensure the cache is populated.
            if not self._dialogs_cache:
                await self._refresh_dialogs_cache(limit=500)
            for chat in self._dialogs_cache.values():
                if len(results) >= maximum:
                    break
                if not chat.is_private or chat.id in seen_ids:
                    continue
                haystack = f"{chat.title} {chat.username or ''} {chat.phone}".lower()
                if q_lower in haystack:
                    seen_ids.add(chat.id)
                    results.append({
                        "id": chat.id,
                        "name": chat.title,
                        "username": chat.username or "",
                        "phone": chat.phone,
                        "source": "chats",
                    })

        return results

    # -------------------------------------------------- Statistics

    async def _get_statistics(self) -> dict[str, Any]:
        """Return an overview of the account's chats and contacts."""
        if not self._dialogs_cache:
            await self._refresh_dialogs_cache(limit=500)

        chats = list(self._dialogs_cache.values())
        private = [c for c in chats if c.is_private]
        groups = [c for c in chats if c.is_group and not c.is_channel]
        channels = [c for c in chats if c.is_channel]
        bots = [c for c in chats if c.is_bot]
        unread = [c for c in chats if c.unread_count > 0]
        total_unread = sum(c.unread_count for c in chats)

        # Contacts count (best-effort)
        contacts_count = 0
        try:
            from telethon.tl.functions.contacts import GetContactsRequest
            contact_result = await self._client(GetContactsRequest(hash=0))
            contacts_count = len(getattr(contact_result, "users", []) or [])
        except Exception:
            pass

        return {
            "total_chats": len(chats),
            "private_chats": len(private),
            "groups": len(groups),
            "channels": len(channels),
            "bots": len(bots),
            "total_contacts": contacts_count,
            "unread_chats": len(unread),
            "total_unread_messages": total_unread,
            "top_unread": [
                {"title": c.title, "id": c.id, "unread": c.unread_count}
                for c in sorted(unread, key=lambda x: x.unread_count, reverse=True)[:10]
            ],
        }

    # -------------------------------------------------- Other async methods

    async def _add_contact(self, phone: str, first_name: str, last_name: str) -> dict[str, Any]:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact
        result = await self._client(ImportContactsRequest([InputPhoneContact(
            client_id=0, phone=str(phone), first_name=str(first_name), last_name=str(last_name))]))
        users = getattr(result, "users", [])
        return {"id": int(users[0].id)} if users else {"added": False}

    async def _delete_contact(self, contact) -> None:
        from telethon.tl.functions.contacts import DeleteContactsRequest
        entity = await self._resolve_entity(contact)
        await self._client(DeleteContactsRequest(id=[entity]))

    async def _block_user(self, contact) -> None:
        from telethon.tl.functions.contacts import BlockRequest
        await self._client(BlockRequest(id=await self._resolve_entity(contact)))

    async def _unblock_user(self, contact) -> None:
        from telethon.tl.functions.contacts import UnblockRequest
        await self._client(UnblockRequest(id=await self._resolve_entity(contact)))

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
        return [
            {
                "id": int(u.id),
                "name": " ".join(p for p in (getattr(u, "first_name", "") or "",
                                              getattr(u, "last_name", "") or "") if p),
                "username": getattr(u, "username", "") or "",
            }
            for u in users
        ]

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

    # -------------------------------------------------- Send / receive

    async def _send_message(self, chat, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._flood_aware_send(entity, text)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _flood_aware_send(self, entity, text: str):
        """Send a message with automatic FloodWaitError handling."""
        try:
            return await self._client.send_message(entity, text)
        except Exception as exc:
            if "FloodWaitError" in type(exc).__name__:
                wait = getattr(exc, "seconds", 5)
                logger.warning("FloodWait: sleeping %ds", wait)
                await asyncio.sleep(min(wait, 30))
                return await self._client.send_message(entity, text)
            raise

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
                p for p in (getattr(entity, "first_name", ""),
                            getattr(entity, "last_name", "")) if p
            ) or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                             or getattr(entity, "is_group", False)),
        }
        if not info["is_group"]:
            info["phone"] = getattr(entity, "phone", "") or ""
            info["bio"] = getattr(entity, "about", "") or ""
            info["last_seen"] = _extract_last_seen(entity)
        # members count for groups/channels
        info["members_count"] = int(getattr(entity, "participants_count", 0) or 0)
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
        try:
            result = await self._client.forward_messages(target, msg_id, source)
        except Exception as exc:
            if "FloodWaitError" in type(exc).__name__:
                wait = getattr(exc, "seconds", 5)
                logger.warning("FloodWait on forward: sleeping %ds", wait)
                await asyncio.sleep(min(wait, 30))
                result = await self._client.forward_messages(target, msg_id, source)
            else:
                raise
        return _message_from_telethon(result, chat_id=getattr(target, "id", 0))

    async def _mark_read(self, chat) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.send_read_acknowledge(entity)

    async def _delete_message(self, chat, msg_id: int) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.delete_messages(entity, msg_id, revoke=True)

    async def _edit_message(self, chat, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.edit_message(entity, msg_id, text)
        return _message_from_telethon(result, chat_id=getattr(entity, "id", 0))

    async def _resolve_username(self, username: str) -> dict[str, Any]:
        cleaned = str(username or "").strip().lstrip("@")
        if not cleaned:
            raise TelegramError("نام کاربری خالی است")
        entity = await self._client.get_entity(cleaned)
        return {
            "id": entity.id,
            "name": getattr(entity, "title", None) or " ".join(
                p for p in (getattr(entity, "first_name", ""),
                            getattr(entity, "last_name", "")) if p
            ) or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                             or getattr(entity, "is_group", False)),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_last_seen(user) -> str:
    """Extract a human-readable last-seen string from a Telethon user."""
    try:
        status = getattr(user, "status", None)
        if status is None:
            return ""
        type_name = type(status).__name__
        if type_name == "UserStatusOnline":
            return "online"
        if type_name == "UserStatusOffline":
            was = getattr(status, "was_online", None)
            if was:
                return was.isoformat()
            return "offline"
        if type_name == "UserStatusRecently":
            return "recently"
        if type_name == "UserStatusLastWeek":
            return "last week"
        if type_name == "UserStatusLastMonth":
            return "last month"
        if type_name == "UserStatusEmpty":
            return ""
    except Exception:
        pass
    return ""


def _detect_media_type(msg) -> str:
    """Classify the media type of a Telethon message."""
    media = getattr(msg, "media", None)
    if media is None:
        return "text"
    type_name = type(media).__name__
    if type_name == "MessageMediaPhoto":
        return "photo"
    if type_name == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        if doc:
            mime = getattr(doc, "mime_type", "") or ""
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("audio/"):
                return "audio"
            if "image/gif" in mime:
                return "gif"
            if mime.startswith("application/"):
                return "document"
        return "document"
    if type_name == "MessageMediaGeo":
        return "location"
    if type_name == "MessageMediaContact":
        return "contact"
    if type_name == "MessageMediaPoll":
        return "poll"
    if type_name == "MessageMediaWebPage":
        return "webpage"
    if type_name == "MessageMediaVenue":
        return "venue"
    if type_name == "MessageMediaDice":
        return "dice"
    if type_name == "MessageMediaInvoice":
        return "invoice"
    if type_name == "MessageMediaGame":
        return "game"
    return "other"


def _message_from_telethon(msg, *, chat_id: int) -> Message:
    sender = "کانال" if getattr(msg, "sender", None) is None else "?"
    if getattr(msg, "sender", None) is not None:
        sender_obj = msg.sender
        sender = getattr(sender_obj, "username", None) or getattr(sender_obj, "first_name", None) or "?"

    reply_to = getattr(msg, "reply_to", None)
    reply_to_msg_id = None
    is_reply = False
    if reply_to is not None:
        is_reply = True
        reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)

    return Message(
        id=int(msg.id),
        chat_id=chat_id,
        sender=str(sender),
        text=str(msg.message or ""),
        date=msg.date,
        is_outgoing=bool(getattr(msg, "out", False)),
        is_reply=is_reply,
        reply_to_msg_id=reply_to_msg_id,
        forwards=int(getattr(msg, "forwards", 0) or 0),
        views=int(getattr(msg, "views", 0) or 0),
        media_type=_detect_media_type(msg),
    )
