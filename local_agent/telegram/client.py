"""Telethon wrapper for the local assistant.

``PersonalTelegram`` owns a single Telethon ``TelegramClient`` running on a
private background event loop.  Every *public* method is **synchronous** (safe
to call from the agent loop / actions layer); it submits a coroutine to the
background loop via :meth:`_run` and blocks until the result is ready.

A two-step interactive login is exposed for the web UI state machine:
:meth:`start_login` → :meth:`submit_code` → (optional) :meth:`submit_password`.

The wrapper intentionally hides every Telethon-specific type behind the small
``Chat`` / ``Message`` dataclasses so the action layer never imports Telethon.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from .storage import TelegramStorage

logger = get_logger("telegram")


class TelegramError(AssistantError):
    """A user-facing failure from the personal Telegram client."""


# --------------------------------------------------------------------------- #
# Public data models (stable contract used by the actions layer + tests)
# --------------------------------------------------------------------------- #


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


def _install_hint() -> str:
    return (
        "پکیج telethon نصب نیست. برای نصب با همان interpreter ربات:\n"
        "python -m pip install telethon\n"
        "سپس api_id / api_hash / phone را از https://my.telegram.org بگیرید."
    )


class PersonalTelegram:
    """Async client for the user's personal Telegram account.

    The synchronous public API is a thin shell over a background asyncio loop.
    All Telethon access is lazy so the module imports even when Telethon is not
    installed (the actions layer can then surface a helpful error instead of an
    import crash).
    """

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: Path,
        account_name: str = "اصلی",
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        if not api_id or not api_hash or not phone:
            raise TelegramError(
                "اطلاعات تلگرام ناقص است: api_id، api_hash و phone را تنظیم کنید"
            )
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)
        self._phone = str(phone)
        self.account_name = str(account_name)
        self.session_path = Path(session_path)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)

        # Local SQLite mirror for fast fuzzy entity lookup + selective caching.
        self.db = TelegramStorage(self.session_path.parent / f"tg_{self.account_name}.db")

        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._manual_disconnect = False
        self.connected_at: datetime | None = None
        self.last_error = ""
        self._on_event = on_event

        # Login state machine (values seen by the web UI):
        # disconnected → await_code → await_2fa → connected
        self._login_state = "disconnected"
        self._login_ctx: dict[str, Any] = {}

        # Best-effort op timeout (seconds); login uses a longer budget.
        self._timeout = 90

        self._start_loop()
        logger.info("PersonalTelegram «%s» مقداردهی شد", self.account_name)

    # ------------------------------------------------------------------ #
    # Public introspection
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def login_state(self) -> str:
        return self._login_state

    @property
    def state(self) -> str:  # backwards-compatible alias
        return self._login_state

    # ------------------------------------------------------------------ #
    # Background loop plumbing
    # ------------------------------------------------------------------ #

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

        self._thread = threading.Thread(target=runner, name=f"tg-{self.account_name}-loop", daemon=True)
        self._thread.start()
        ready_evt.wait(timeout=10)
        self._loop = loop_holder["loop"]

    def _run(self, coro: Any, *, timeout: float | None = None, require_connected: bool = True) -> Any:
        """Submit *coro* to the background loop and block until it resolves.

        Operations require an active connection unless ``require_connected`` is
        False (used by the login / connect / disconnect primitives themselves).
        """
        if require_connected and not self._connected:
            raise TelegramError("ابتدا به تلگرام وصل شوید.")
        if self._loop is None or not self._loop.is_running():
            raise TelegramError("حلقهٔ داخلی تلگرام آماده نیست؛ دوباره تلاش کنید.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout or self._timeout)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise TelegramError("عملیات تلگرام بیش از حد طول کشید.") from exc

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from telethon import TelegramClient
        except ImportError as exc:  # pragma: no cover - depends on env
            raise TelegramError(_install_hint()) from exc
        self._client = TelegramClient(str(self.session_path), self._api_id, self._api_hash)
        return self._client

    # ------------------------------------------------------------------ #
    # Live event monitoring + DB mirroring
    # ------------------------------------------------------------------ #

    async def _setup_event_handlers(self) -> None:
        """Register live listeners for incoming messages and presence."""
        from telethon import events

        @self._client.on(events.NewMessage())
        async def handler(event: Any) -> None:
            if event.message is None:
                return
            try:
                chat = await event.get_chat()
                await self._sync_entity_to_db(chat)
            except Exception as exc:  # noqa: BLE001 - monitoring must not crash
                logger.debug("event chat sync failed: %s", exc)
            if self._on_event is not None:
                self._on_event(
                    {
                        "type": "new_message",
                        "chat_id": event.chat_id,
                        "text": event.raw_text,
                        "sender_id": event.sender_id,
                    }
                )

        @self._client.on(events.UserUpdate())
        async def user_handler(event: Any) -> None:
            if self._on_event is not None:
                self._on_event(
                    {
                        "type": "user_update",
                        "user_id": event.user_id,
                        "online": getattr(event, "online", None),
                    }
                )

    async def _sync_entity_to_db(self, entity: Any) -> None:
        """Mirror a Telethon entity into the local SQLite cache."""
        try:
            from telethon.tl.types import Channel, Chat

            e_id = getattr(entity, "id", None)
            if e_id is None:
                return
            e_type = "user"
            if isinstance(entity, Channel):
                e_type = "supergroup" if getattr(entity, "megagroup", False) else "channel"
            elif isinstance(entity, Chat):
                e_type = "group"
            title = (
                getattr(entity, "title", None)
                or " ".join(
                    p
                    for p in (getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or "")
                    if p
                )
                or None
            )
            data = {
                "id": e_id,
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None),
                "title": title,
                "first_name": getattr(entity, "first_name", None),
                "last_name": getattr(entity, "last_name", None),
                "type": e_type,
                "bio": None,
                "about": getattr(entity, "about", None),
                "participants_count": getattr(entity, "participants_count", 0),
                "unread_count": 0,
            }
            self.db.save_entity(data)
        except Exception as exc:  # noqa: BLE001 - mirroring is best-effort
            logger.debug("entity sync failed for %s: %s", entity, exc)

    async def _finish_login(self) -> dict[str, Any]:
        self._login_state = "connected"
        self._connected = True
        self.connected_at = datetime.now()
        self.last_error = ""
        try:
            await self._setup_event_handlers()
            asyncio.create_task(self._initial_sync())  # noqa: RUF006 - fire & forget
        except Exception as exc:  # noqa: BLE001 - monitoring is optional
            logger.debug("event handlers setup failed: %s", exc)
        me = await self._get_me()
        return {
            "state": "connected",
            "message": f"وصل شدید به‌عنوان {me.get('username') or me.get('first_name') or '?'}",
            "user": me,
        }

    async def _initial_sync(self) -> None:
        """Background mirror of dialogs + contacts to local SQLite."""
        try:
            async for dialog in self._client.iter_dialogs(limit=1000):
                await self._sync_entity_to_db(dialog.entity)
            from telethon.tl.functions.contacts import GetContactsRequest

            result = await self._client(GetContactsRequest(hash=0))
            for user in getattr(result, "users", []):
                await self._sync_entity_to_db(user)
        except Exception as exc:  # noqa: BLE001 - background sync
            logger.warning("initial sync failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Connection + interactive login (web UI state machine)
    # ------------------------------------------------------------------ #

    async def _connect(self) -> dict[str, Any]:
        client = await self._ensure_client()
        await client.connect()
        return client

    async def _disconnect(self) -> None:
        self._connected = False
        self._login_state = "disconnected"
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("disconnect failed: %s", exc)

    async def _start_login(self) -> dict[str, Any]:
        try:
            client = await self._connect()
            if self._connected:
                return {"state": "connected", "message": "از قبل وصل است"}
            if await client.is_user_authorized():
                return await self._finish_login()
            sent = await client.send_code_request(self._phone)
            self._login_ctx = {"phone_code_hash": sent.phone_code_hash}
            self._login_state = "await_code"
            self._connected = False
            return {"state": "await_code", "message": "کد تأیید ارسال شد"}
        except (TelegramError, ConnectionError, TimeoutError, OSError):
            raise
        except Exception as exc:  # noqa: BLE001 - surface a readable error
            self.last_error = str(exc)
            raise TelegramError(f"شروع ورود ناموفق بود: {exc}") from exc

    async def _submit_code(self, code: str) -> dict[str, Any]:
        if self._login_state != "await_code" or self._client is None:
            raise TelegramError("ابتدا اتصال را شروع کنید (start_login).")
        try:
            from telethon.errors import SessionPasswordNeededError

            try:
                await self._client.sign_in(
                    phone=self._phone,
                    code=str(code),
                    phone_code_hash=self._login_ctx.get("phone_code_hash", ""),
                )
            except SessionPasswordNeededError:
                self._login_state = "await_2fa"
                self._connected = False
                return {"state": "await_2fa", "message": "رمز دو مرحله‌ای لازم است"}
            return await self._finish_login()
        except (TelegramError, ConnectionError, TimeoutError, OSError):
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            raise TelegramError(f"کد واردشده پذیرفته نشد: {exc}") from exc

    async def _submit_password(self, password: str) -> dict[str, Any]:
        if self._login_state != "await_2fa" or self._client is None:
            raise TelegramError("رمز دو مرحله‌ای درخواست نشده است.")
        try:
            await self._client.sign_in(password=str(password))
            return await self._finish_login()
        except (TelegramError, ConnectionError, TimeoutError, OSError):
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            raise TelegramError(f"رمز دو مرحله‌ای پذیرفته نشد: {exc}") from exc

    async def _connect_flow(
        self,
        code_callback: Callable[[], str] | None,
        password_callback: Callable[[], str] | None,
    ) -> dict[str, Any]:
        """One-shot connect that pulls code/password from callbacks."""
        from telethon.errors import SessionPasswordNeededError

        client = await self._connect()
        if await client.is_user_authorized():
            return await self._finish_login()
        sent = await client.send_code_request(self._phone)
        self._login_ctx = {"phone_code_hash": getattr(sent, "phone_code_hash", "")}
        if code_callback is None:
            self._login_state = "await_code"
            self._connected = False
            return {"state": "await_code", "message": "کد تأیید ارسال شد"}
        code = code_callback()
        try:
            await client.sign_in(
                phone=self._phone,
                code=str(code),
                phone_code_hash=self._login_ctx.get("phone_code_hash", ""),
            )
        except SessionPasswordNeededError:
            if password_callback is None:
                self._login_state = "await_2fa"
                self._connected = False
                return {"state": "await_2fa", "message": "رمز دو مرحله‌ای لازم است"}
            await client.sign_in(password=str(password_callback()))
        return await self._finish_login()

    # ---- synchronous login shells (used by handlers.py + tests) ---------- #

    def start_login(self) -> dict[str, Any]:
        return self._run(self._start_login(), timeout=180, require_connected=False)

    def submit_code(self, code: str) -> dict[str, Any]:
        return self._run(self._submit_code(code), timeout=60, require_connected=False)

    def submit_password(self, password: str) -> dict[str, Any]:
        return self._run(self._submit_password(password), timeout=60, require_connected=False)

    def connect(
        self,
        *,
        code_callback: Callable[[], str] | None = None,
        password_callback: Callable[[], str] | None = None,
    ) -> str:
        """One-shot connect using optional callbacks; returns a status string."""
        try:
            result = self._run(
                self._connect_flow(code_callback, password_callback),
                timeout=180,
                require_connected=False,
            )
            return f"{result.get('state', 'connected')} — {result.get('message', '')}"
        except TelegramError as exc:
            return f"error: {exc}"

    def cancel_login(self) -> None:
        """Abort an in-progress login flow (resets state to disconnected)."""
        self._login_state = "disconnected"
        self._connected = False
        self._login_ctx = {}
        if self._loop is not None and self._loop.is_running():
            try:
                self._run(self._disconnect(), timeout=15, require_connected=False)
            except Exception as exc:  # noqa: BLE001 - teardown
                logger.debug("cancel_login disconnect failed: %s", exc)

    def disconnect(self) -> None:
        if self._loop is not None and self._loop.is_running():
            try:
                self._run(self._disconnect(), timeout=20, require_connected=False)
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("disconnect wrapper failed: %s", exc)
        else:
            self._connected = False
            self._login_state = "disconnected"

    # ------------------------------------------------------------------ #
    # Sessions / privacy / devices  (God-Mode)
    # ------------------------------------------------------------------ #

    async def _get_sessions(self) -> list[dict[str, Any]]:
        from telethon.tl.functions.account import GetAuthorizationsRequest

        await self._ensure_client()
        result = await self._client(GetAuthorizationsRequest())
        sessions: list[dict[str, Any]] = []
        for auth in result.authorizations:
            sessions.append(
                {
                    "hash": auth.hash,
                    "device_model": auth.device_model,
                    "platform": auth.platform,
                    "system_version": auth.system_version,
                    "api_id": auth.api_id,
                    "app_name": auth.app_name,
                    "app_version": auth.app_version,
                    "date_created": auth.date_created.isoformat() if auth.date_created else None,
                    "date_active": auth.date_active.isoformat() if auth.date_active else None,
                    "ip": auth.ip,
                    "country": auth.country,
                    "region": auth.region,
                }
            )
        return sessions

    async def _terminate_session(self, session_hash: int) -> bool:
        from telethon.tl.functions.account import ResetAuthorizationRequest

        await self._ensure_client()
        await self._client(ResetAuthorizationRequest(hash=int(session_hash)))
        return True

    async def _get_privacy_settings(self) -> dict[str, Any]:
        from telethon.tl.functions.account import GetPrivacyRequest
        from telethon.tl.types import (
            InputPrivacyKeyChatInvite,
            InputPrivacyKeyPhoneNumber,
            InputPrivacyKeyStatusTimestamp,
        )

        await self._ensure_client()
        keys = {
            "phone_number": InputPrivacyKeyPhoneNumber(),
            "last_seen": InputPrivacyKeyStatusTimestamp(),
            "group_invites": InputPrivacyKeyChatInvite(),
        }
        out: dict[str, Any] = {}
        for label, key in keys.items():
            res = await self._client(GetPrivacyRequest(key=key))
            out[label] = ", ".join(type(rule).__name__ for rule in (res.rules or []))
        return out

    def get_sessions(self) -> list[dict[str, Any]]:
        return self._run(self._get_sessions())

    def terminate_session(self, session_hash: int) -> bool:
        return self._run(self._terminate_session(session_hash))

    def get_privacy_settings(self) -> dict[str, Any]:
        return self._run(self._get_privacy_settings())

    # ------------------------------------------------------------------ #
    # Entity resolution (id → DB fuzzy → @username → +phone → server)
    # ------------------------------------------------------------------ #

    async def _resolve_entity(self, target: Any) -> Any:
        """Resolve an id / @username / phone / name to a Telethon entity."""
        await self._ensure_client()
        # 1) Numeric id (int or numeric string)
        try:
            e_id = int(target)
            return await self._client.get_entity(e_id)
        except (ValueError, TypeError):
            pass

        text = str(target).strip()
        # 2) Local DB fuzzy match (title / username / phone / name)
        local = self.db.search_entities(text, limit=1)
        if local:
            try:
                return await self._client.get_entity(local[0]["id"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("db entity resolve failed: %s", exc)
        # 3) Direct server lookup (handles @username, +phone, t.me links)
        try:
            return await self._client.get_entity(text)
        except Exception as exc:
            raise TelegramError(f"«{target}» در حافظه و سرور پیدا نشد.") from exc

    async def _entity_id(self, target: Any) -> int:
        entity = await self._resolve_entity(target)
        return int(getattr(entity, "id", target))

    # ------------------------------------------------------------------ #
    # Read-only chat / message operations
    # ------------------------------------------------------------------ #

    async def _list_chats(
        self, limit: int, *, kind: str = "all", query: str = "", sort: str = ""
    ) -> list[Chat]:
        chats: list[Chat] = []
        async for dialog in self._client.iter_dialogs(limit=max(1, limit)):
            entity = dialog.entity
            title = getattr(entity, "title", None) or " ".join(
                p
                for p in (getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or "")
                if p
            ) or "?"
            username = getattr(entity, "username", None)
            type_name = type(entity).__name__.lower()
            is_channel = type_name == "channel" and not bool(getattr(entity, "megagroup", False))
            is_group = bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or getattr(entity, "is_group", False)
                or type_name == "chat"
            )
            is_bot = bool(type_name == "user" and getattr(entity, "bot", False))
            is_private = bool(type_name == "user" and not is_bot)
            chat = Chat(
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
            )
            if kind != "all" and not getattr(chat, f"is_{kind}", False):
                continue
            if query and str(query).lower() not in f"{chat.title} {chat.username or ''}".lower():
                continue
            chats.append(chat)
        if sort == "unread":
            chats.sort(key=lambda item: item.unread_count, reverse=True)
        return chats[: max(1, limit)]

    async def _search_messages(self, chat: Any, query: str, limit: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = int(getattr(entity, "id", 0))
        out: list[Message] = []
        async for msg in self._client.iter_messages(entity, search=query, limit=max(1, limit)):
            out.append(_message_from_telethon(msg, chat_id=chat_id))
        return out

    async def _get_chat_history(self, chat: Any, limit: int, offset_id: int) -> list[Message]:
        entity = await self._resolve_entity(chat)
        chat_id = int(getattr(entity, "id", 0))
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if offset_id:
            kwargs["offset_id"] = int(offset_id)
        out: list[Message] = []
        async for msg in self._client.iter_messages(entity, **kwargs):
            out.append(_message_from_telethon(msg, chat_id=chat_id))
        return out

    async def _get_me(self) -> dict[str, Any]:
        await self._ensure_client()
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": getattr(me, "last_name", "") or "",
            "username": getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", "") or "",
        }

    async def _search_contacts(self, query: str, limit: int) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetContactsRequest

        q = str(query or "").strip()
        if not q:
            raise TelegramError("عبارت جست‌وجوی مخاطب خالی است")
        result = await self._client(GetContactsRequest(hash=0))
        out: list[dict[str, Any]] = []
        for user in getattr(result, "users", []):
            name = " ".join(p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p)
            username = getattr(user, "username", "") or ""
            phone = getattr(user, "phone", "") or ""
            if q.lower() in f"{name} {username} {phone}".lower():
                out.append({"id": user.id, "name": name, "username": username, "phone": phone})
                if len(out) >= max(1, limit):
                    break
        return out

    async def _list_contacts(self, limit: int) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await self._client(GetContactsRequest(hash=0))
        out: list[dict[str, Any]] = []
        for user in getattr(result, "users", [])[: max(1, limit)]:
            out.append(
                {
                    "id": user.id,
                    "first_name": getattr(user, "first_name", "") or "",
                    "last_name": getattr(user, "last_name", "") or "",
                    "username": getattr(user, "username", "") or "",
                    "phone": getattr(user, "phone", "") or "",
                }
            )
        return out

    async def _get_contact_info(self, contact: Any) -> dict[str, Any]:
        from telethon.tl.functions.users import GetFullUserRequest

        entity = await self._resolve_entity(contact)
        info: dict[str, Any] = {
            "id": entity.id,
            "first_name": getattr(entity, "first_name", "") or "",
            "last_name": getattr(entity, "last_name", "") or "",
            "username": getattr(entity, "username", "") or "",
            "phone": getattr(entity, "phone", "") or "",
            "bio": "",
            "is_blocked": False,
        }
        try:
            res = await self._client(GetFullUserRequest(id=entity))
            info["bio"] = getattr(res.full_user, "about", "") or ""
            info["is_blocked"] = bool(getattr(res.full_user, "blocked", False))
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            logger.debug("full user fetch failed: %s", exc)
        return info

    async def _get_profile(self, chat: Any, media_dir: Path) -> dict[str, Any]:
        entity = await self._resolve_entity(chat)
        info: dict[str, Any] = {
            "id": entity.id,
            "name": getattr(entity, "title", None)
            or " ".join(
                p for p in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if p
            )
            or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or getattr(entity, "is_group", False)
            ),
        }
        if not info["is_group"]:
            info["phone"] = getattr(entity, "phone", "") or ""
            try:
                full = await self._client.get_entity(entity)
                info["bio"] = getattr(full, "about", "") or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug("about fetch failed: %s", exc)
                info["bio"] = ""
        photo_path = ""
        if getattr(entity, "photo", None) is not None:
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                filename = media_dir / f"profile_{entity.id}.jpg"
                await self._client.download_profile_photo(entity, file=str(filename))
                if filename.is_file():
                    photo_path = str(filename)
            except Exception as exc:  # noqa: BLE001
                logger.debug("profile photo download failed: %s", exc)
        info["photo_path"] = photo_path
        return info

    async def _resolve_username(self, username: str) -> dict[str, Any]:
        cleaned = str(username or "").strip().lstrip("@")
        if not cleaned:
            raise TelegramError("نام کاربری خالی است")
        entity = await self._client.get_entity(cleaned)
        return {
            "id": entity.id,
            "name": getattr(entity, "title", None)
            or " ".join(
                p for p in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if p
            )
            or "?",
            "username": getattr(entity, "username", "") or "",
            "is_group": bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or getattr(entity, "is_group", False)
            ),
        }

    # ------------------------------------------------------------------ #
    # Sending / replying / forwarding
    # ------------------------------------------------------------------ #

    async def _send_message(self, chat: Any, text: str) -> Message:
        from telethon.errors import FloodWaitError

        entity = await self._resolve_entity(chat)
        chat_id = int(getattr(entity, "id", 0))
        try:
            result = await self._client.send_message(entity, text)
        except FloodWaitError as exc:  # retry once after the server-imposed wait
            await asyncio.sleep(int(getattr(exc, "seconds", 1)) + 1)
            result = await self._client.send_message(entity, text)
        return _message_from_telethon(result, chat_id=chat_id)

    async def _send_media(self, chat: Any, path: Path, *, caption: str, kind: str) -> Message:
        resolved = Path(str(path))
        if not resolved.is_file():
            raise TelegramError(f"فایل پیدا نشد: {path}")
        entity = await self._resolve_entity(chat)
        uploaded = await self._client.upload_file(str(resolved))
        kwargs: dict[str, Any] = {"caption": caption or ""}
        if kind == "voice":
            kwargs["voice_note"] = True
        elif kind == "video_note":
            kwargs["video_note"] = True
        elif kind == "document":
            kwargs["force_document"] = True
        elif kind == "photo":
            kwargs["force_document"] = False
        result = await self._client.send_file(entity, uploaded, **kwargs)
        return _message_from_telethon(result, chat_id=int(getattr(entity, "id", 0)))

    async def _send_location(self, chat: Any, lat: float, lng: float) -> Message:
        from telethon.tl.types import InputGeoPoint

        entity = await self._resolve_entity(chat)
        geo = InputGeoPoint(lat=float(lat), long=float(lng))
        result = await self._client.send_file(entity, geo)
        return _message_from_telethon(result, chat_id=int(getattr(entity, "id", 0)))

    async def _reply_to(self, chat: Any, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.send_message(entity, text, reply_to=int(msg_id))
        return _message_from_telethon(result, chat_id=int(getattr(entity, "id", 0)))

    async def _forward_message(self, chat: Any, from_chat: Any, msg_id: int) -> Message:
        target = await self._resolve_entity(chat)
        source = await self._resolve_entity(from_chat)
        result = await self._client.forward_messages(target, int(msg_id), source)
        return _message_from_telethon(result, chat_id=int(getattr(target, "id", 0)))

    async def _delete_message(self, chat: Any, msg_id: int) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.delete_messages(entity, [int(msg_id)])

    async def _edit_message(self, chat: Any, msg_id: int, text: str) -> Message:
        entity = await self._resolve_entity(chat)
        result = await self._client.edit_message(entity, int(msg_id), text)
        return _message_from_telethon(result, chat_id=int(getattr(entity, "id", 0)))

    async def _mark_read(self, chat: Any) -> None:
        entity = await self._resolve_entity(chat)
        await self._client.send_read_acknowledge(entity)

    async def _bulk_send(self, targets: list[Any], text: str) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for target in targets:
            try:
                await self._send_message(target, text)
                out[str(target)] = True
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                logger.debug("bulk send to %s failed: %s", target, exc)
                out[str(target)] = False
            await asyncio.sleep(2)  # gentle pacing to avoid FloodWait
        return out

    async def _bulk_forward(
        self, from_chat: Any, to_chats: list[Any], msg_id: int
    ) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for target in to_chats:
            try:
                await self._forward_message(target, from_chat, int(msg_id))
                out[str(target)] = True
            except Exception as exc:  # noqa: BLE001
                logger.debug("bulk forward to %s failed: %s", target, exc)
                out[str(target)] = False
            await asyncio.sleep(1)
        return out

    # ------------------------------------------------------------------ #
    # Media
    # ------------------------------------------------------------------ #

    async def _download_media(self, chat: Any, msg_id: int, filename: str, media_dir: Path) -> Path:
        entity = await self._resolve_entity(chat)
        messages = await self._client.get_messages(entity, ids=int(msg_id))
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

    async def _download_all_media(
        self,
        chat: Any,
        limit: int,
        media_types: list[str] | None,
        media_dir: Path,
    ) -> list[str]:
        entity = await self._resolve_entity(chat)
        media_dir.mkdir(parents=True, exist_ok=True)
        wanted = set(media_types or [])
        saved: list[str] = []
        async for msg in self._client.iter_messages(entity, limit=max(1, limit)):
            if msg is None or msg.media is None:
                continue
            if wanted:
                kind = _media_kind(msg)
                if kind not in wanted:
                    continue
            try:
                out = await msg.download_media(file=str(media_dir))
                if out:
                    saved.append(str(out))
            except Exception as exc:  # noqa: BLE001 - keep going
                logger.debug("media download skipped: %s", exc)
        return saved

    async def _download_profile_photo(
        self, target: Any, path: str | None, media_dir: Path
    ) -> str:
        entity = await self._resolve_entity(target)
        media_dir.mkdir(parents=True, exist_ok=True)
        filename = path or f"profile_{getattr(entity, 'id', 'user')}.jpg"
        out_path = media_dir / Path(filename).name
        result = await self._client.download_profile_photo(entity, file=str(out_path))
        if not result:
            raise TelegramError("این کاربر/چت عکس پروفایل ندارد")
        return str(out_path)

    # ------------------------------------------------------------------ #
    # Contacts management
    # ------------------------------------------------------------------ #

    async def _add_contact(self, phone: str, first_name: str, last_name: str) -> dict[str, Any]:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        result = await self._client(
            ImportContactsRequest(
                [
                    InputPhoneContact(
                        client_id=0,
                        phone=str(phone),
                        first_name=str(first_name),
                        last_name=str(last_name or ""),
                    )
                ]
            )
        )
        users = getattr(result, "users", []) or []
        if users:
            u = users[0]
            return {"id": u.id, "first_name": getattr(u, "first_name", ""), "last_name": getattr(u, "last_name", "")}
        return {"id": None, "first_name": first_name, "last_name": last_name}

    async def _delete_contact(self, contact: Any) -> None:
        from telethon.tl.functions.contacts import DeleteContactsRequest

        entity = await self._resolve_entity(contact)
        await self._client(DeleteContactsRequest(id=[entity]))

    async def _block_user(self, contact: Any) -> None:
        from telethon.tl.functions.contacts import BlockRequest

        entity = await self._resolve_entity(contact)
        await self._client(BlockRequest(id=entity))

    async def _unblock_user(self, contact: Any) -> None:
        from telethon.tl.functions.contacts import UnblockRequest

        entity = await self._resolve_entity(contact)
        await self._client(UnblockRequest(id=entity))

    # ------------------------------------------------------------------ #
    # Channels / groups
    # ------------------------------------------------------------------ #

    async def _join_channel(self, channel: Any) -> None:
        from telethon.tl.functions.channels import JoinChannelRequest

        entity = await self._resolve_entity(channel)
        await self._client(JoinChannelRequest(entity))

    async def _leave_channel(self, channel: Any) -> None:
        from telethon.tl.functions.channels import LeaveChannelRequest

        entity = await self._resolve_entity(channel)
        await self._client(LeaveChannelRequest(entity))

    async def _list_members(self, chat: Any, limit: int, admins: bool) -> list[dict[str, Any]]:
        from telethon.tl.types import ChannelParticipantsAdmins

        entity = await self._resolve_entity(chat)
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if admins:
            kwargs["filter"] = ChannelParticipantsAdmins
        participants = await self._client.get_participants(entity, **kwargs)
        out: list[dict[str, Any]] = []
        for user in participants:
            name = " ".join(
                p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
            ) or "?"
            out.append({"id": user.id, "name": name, "username": getattr(user, "username", "") or ""})
        return out

    # ------------------------------------------------------------------ #
    # Profile management
    # ------------------------------------------------------------------ #

    async def _update_profile(self, first_name: str, last_name: str, about: str) -> None:
        from telethon.tl.functions.account import UpdateProfileRequest

        await self._client(
            UpdateProfileRequest(
                first_name=first_name or "",
                last_name=last_name or "",
                about=about or "",
            )
        )

    async def _update_username(self, username: str) -> None:
        from telethon.tl.functions.account import UpdateUsernameRequest

        await self._client(UpdateUsernameRequest(username=str(username)))

    async def _set_profile_photo(self, path: str) -> None:
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        uploaded = await self._client.upload_file(str(path))
        await self._client(UploadProfilePhotoRequest(file=uploaded))

    async def _set_online_status(self, online: bool) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest

        await self._client(UpdateStatusRequest(offline=not bool(online)))

    # ------------------------------------------------------------------ #
    # Global search / deep content
    # ------------------------------------------------------------------ #

    async def _global_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import SearchRequest

        result = await self._client(SearchRequest(q=str(query), limit=max(1, limit)))
        found: list[dict[str, Any]] = []
        for user in getattr(result, "users", []):
            found.append(
                {
                    "id": user.id,
                    "name": " ".join(
                        p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
                    ),
                    "username": getattr(user, "username", "") or "",
                    "type": "user",
                }
            )
        for chat in getattr(result, "chats", []):
            found.append(
                {
                    "id": chat.id,
                    "title": getattr(chat, "title", "") or "",
                    "username": getattr(chat, "username", "") or "",
                    "type": "channel/group",
                }
            )
        return found

    async def _get_full_chat_details(self, chat: Any) -> dict[str, Any]:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import User

        entity = await self._resolve_entity(chat)
        full_info: dict[str, Any] = {}
        try:
            if isinstance(entity, User):
                res = await self._client(GetFullUserRequest(id=entity))
                full_info = {
                    "id": entity.id,
                    "about": getattr(res.full_user, "about", "") or "",
                    "common_chats": getattr(res.full_user, "common_chats_count", 0),
                    "is_blocked": bool(getattr(res.full_user, "blocked", False)),
                    "verified": bool(getattr(entity, "verified", False)),
                    "premium": bool(getattr(entity, "premium", False)),
                }
            else:
                res = await self._client(GetFullChannelRequest(channel=entity))
                full_info = {
                    "id": entity.id,
                    "about": getattr(res.full_chat, "about", "") or "",
                    "participants_count": getattr(res.full_chat, "participants_count", 0),
                    "admins_count": getattr(res.full_chat, "admins_count", 0),
                    "linked_chat_id": getattr(res.full_chat, "linked_chat_id", None),
                    "slowmode_enabled": bool(getattr(res.full_chat, "slowmode_enabled", False)),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("full chat details failed: %s", exc)
            full_info = {"id": getattr(entity, "id", "unknown"), "error": str(exc)}
        return full_info

    async def _get_pinned_messages(self, chat: Any) -> list[dict[str, Any]]:
        entity = await self._resolve_entity(chat)
        out: list[dict[str, Any]] = []
        async for msg in self._client.iter_messages(entity, pinned=True):
            out.append({"id": msg.id, "text": msg.text or "[رسانه]"})
        return out

    # ------------------------------------------------------------------ #
    # Export (json / txt)
    # ------------------------------------------------------------------ #

    async def _export_chat(self, chat: Any, limit: int = 1000, fmt: str = "json") -> str:
        import json

        entity = await self._resolve_entity(chat)
        eid = int(getattr(entity, "id", 0))
        ename = (
            getattr(entity, "title", None)
            or " ".join(
                p for p in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if p
            )
            or str(eid)
        )
        rows: list[dict[str, Any]] = []
        async for msg in self._client.iter_messages(entity, limit=max(1, limit)):
            rows.append(
                {
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "sender": str(msg.sender_id),
                    "text": msg.text or "[رسانه]",
                }
            )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in ename if c.isalnum() or c in (" ", "-", "_")).strip() or "chat"
        fmt_ext = "txt" if str(fmt).lower().startswith("t") else "json"
        export_dir = self.session_path.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        fp = export_dir / f"export_{safe}_{ts}.{fmt_ext}"
        if fmt_ext == "json":
            payload = {
                "chat_id": eid,
                "chat_name": ename,
                "export_date": datetime.now().isoformat(),
                "total": len(rows),
                "messages": rows,
            }
            fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            lines = [f"چت: {ename}", f"تاریخ خروجی: {datetime.now().isoformat()}", f"تعداد پیام: {len(rows)}", ""]
            for m in rows:
                lines.append(f"[{m['date']}] ({m['sender']}): {m['text']}")
            fp.write_text("\n".join(lines), encoding="utf-8")
        return str(fp)

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    async def _get_statistics(self) -> dict[str, Any]:
        chats = await self._list_chats(400)
        private = [c for c in chats if c.is_private]
        groups = [c for c in chats if c.is_group]
        channels = [c for c in chats if c.is_channel]
        bots = [c for c in chats if c.is_bot]
        unread = [c for c in chats if c.unread_count > 0]
        return {
            "total_chats": len(chats),
            "private_chats": len(private),
            "groups": len(groups),
            "channels": len(channels),
            "bots": len(bots),
            "unread_chats": len(unread),
            "total_unread": sum(c.unread_count for c in chats),
        }

    async def _get_chat_statistics(self, chat: Any, limit: int = 500) -> dict[str, Any]:
        entity = await self._resolve_entity(chat)
        messages = await self._get_chat_history(entity, limit=max(1, limit), offset_id=0)
        sender_counts: dict[Any, int] = {}
        type_counts: dict[str, int] = {}
        for msg in messages:
            if msg.sender:
                sender_counts[msg.sender] = sender_counts.get(msg.sender, 0) + 1
            kind = "outgoing" if msg.is_outgoing else "incoming"
            type_counts[kind] = type_counts.get(kind, 0) + 1
        top = sorted(sender_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "total_messages": len(messages),
            "type_breakdown": type_counts,
            "top_senders": top,
        }

    # ------------------------------------------------------------------ #
    # Synchronous public shells (called by the actions layer)
    # ------------------------------------------------------------------ #

    def list_chats(self, limit: int = 30, *, kind: str = "all", query: str = "", sort: str = "") -> list[Chat]:
        return self._run(self._list_chats(limit, kind=kind, query=query, sort=sort))

    def search_messages(self, chat: Any, query: str, limit: int = 30) -> list[Message]:
        return self._run(self._search_messages(chat, query, limit))

    def get_chat_history(self, chat: Any, limit: int = 30, offset_id: int = 0) -> list[Message]:
        return self._run(self._get_chat_history(chat, limit, offset_id))

    def get_me(self) -> dict[str, Any]:
        return self._run(self._get_me())

    def search_contacts(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        return self._run(self._search_contacts(query, limit))

    def list_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._run(self._list_contacts(limit))

    def get_contact_info(self, contact: Any) -> dict[str, Any]:
        return self._run(self._get_contact_info(contact))

    def get_profile(self, chat: Any, media_dir: Path) -> dict[str, Any]:
        return self._run(self._get_profile(chat, media_dir))

    def resolve_username(self, username: str) -> dict[str, Any]:
        return self._run(self._resolve_username(username))

    def mark_read(self, chat: Any) -> None:
        self._run(self._mark_read(chat))

    def send_message(self, chat: Any, text: str) -> Message:
        return self._run(self._send_message(chat, text))

    def send_media(self, chat: Any, path: Any, *, caption: str = "", kind: str = "document") -> Message:
        return self._run(self._send_media(chat, Path(str(path)), caption=caption, kind=kind))

    def send_file(self, chat: Any, path: Any, *, caption: str = "") -> Message:
        return self.send_media(chat, path, caption=caption, kind="document")

    def send_photo(self, chat: Any, path: Any, *, caption: str = "") -> Message:
        return self.send_media(chat, path, caption=caption, kind="photo")

    def send_location(self, chat: Any, lat: float, lng: float) -> Message:
        return self._run(self._send_location(chat, lat, lng))

    def reply_to(self, chat: Any, msg_id: int, text: str) -> Message:
        return self._run(self._reply_to(chat, msg_id, text))

    def forward_message(self, chat: Any, from_chat: Any, msg_id: int) -> Message:
        return self._run(self._forward_message(chat, from_chat, msg_id))

    def delete_message(self, chat: Any, msg_id: int) -> None:
        self._run(self._delete_message(chat, msg_id))

    def edit_message(self, chat: Any, msg_id: int, text: str) -> Message:
        return self._run(self._edit_message(chat, msg_id, text))

    def download_media(self, chat: Any, msg_id: int, filename: str, media_dir: Path) -> Path:
        return self._run(self._download_media(chat, msg_id, filename, media_dir))

    def download_all_media(
        self, chat: Any, limit: int = 50, media_types: list[str] | None = None, media_dir: Path | None = None
    ) -> list[str]:
        media_dir = media_dir or (self.session_path.parent / "media")
        return self._run(self._download_all_media(chat, limit, media_types, media_dir))

    def download_profile_photo(self, target: Any, path: str | None = None, media_dir: Path | None = None) -> str:
        media_dir = media_dir or (self.session_path.parent / "media")
        return self._run(self._download_profile_photo(target, path, media_dir))

    def add_contact(self, phone: str, first_name: str, last_name: str = "") -> dict[str, Any]:
        return self._run(self._add_contact(phone, first_name, last_name))

    def delete_contact(self, contact: Any) -> None:
        self._run(self._delete_contact(contact))

    def block_user(self, contact: Any) -> None:
        self._run(self._block_user(contact))

    def unblock_user(self, contact: Any) -> None:
        self._run(self._unblock_user(contact))

    def join_channel(self, channel: Any) -> None:
        self._run(self._join_channel(channel))

    def leave_channel(self, channel: Any) -> None:
        self._run(self._leave_channel(channel))

    def list_members(self, chat: Any, limit: int = 100, admins: bool = False) -> list[dict[str, Any]]:
        return self._run(self._list_members(chat, limit, admins))

    def update_profile(self, first_name: str = "", last_name: str = "", about: str = "") -> None:
        self._run(self._update_profile(first_name, last_name, about))

    def update_username(self, username: str) -> None:
        self._run(self._update_username(username))

    def set_profile_photo(self, path: str) -> None:
        self._run(self._set_profile_photo(path))

    def set_online_status(self, online: bool = True) -> None:
        self._run(self._set_online_status(online))

    def bulk_send(self, targets: list[Any], text: str) -> dict[str, bool]:
        return self._run(self._bulk_send(list(targets), text))

    def bulk_forward(self, from_chat: Any, to_chats: list[Any], msg_id: int) -> dict[str, bool]:
        return self._run(self._bulk_forward(from_chat, list(to_chats), msg_id))

    def global_search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._run(self._global_search(query, limit))

    def get_full_chat_details(self, chat: Any) -> dict[str, Any]:
        return self._run(self._get_full_chat_details(chat))

    def get_pinned_messages(self, chat: Any) -> list[dict[str, Any]]:
        return self._run(self._get_pinned_messages(chat))

    def export_chat(self, chat: Any, limit: int = 1000, fmt: str = "json") -> str:
        return self._run(self._export_chat(chat, limit, fmt))

    def get_statistics(self) -> dict[str, Any]:
        return self._run(self._get_statistics())

    def get_chat_statistics(self, chat: Any, limit: int = 500) -> dict[str, Any]:
        return self._run(self._get_chat_statistics(chat, limit))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _media_kind(msg: Any) -> str:
    media = getattr(msg, "media", None)
    if media is None:
        return "text"
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return "photo"
    if name == "MessageMediaDocument":
        doc = getattr(msg, "document", None)
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if "image/gif" in mime:
            return "gif"
        return "document"
    return "other"


def _message_from_telethon(msg: Any, *, chat_id: int) -> Message:
    sender = "?"
    sender_obj = getattr(msg, "sender", None)
    if sender_obj is None:
        # Channel posts and some service messages have no sender; label them.
        if getattr(msg, "post", False):
            sender = "کانال"
    else:
        sender = (
            getattr(sender_obj, "username", None)
            or getattr(sender_obj, "first_name", None)
            or getattr(sender_obj, "title", None)
            or "?"
        )
    return Message(
        id=int(msg.id),
        chat_id=chat_id,
        sender=str(sender),
        text=str(msg.message or ""),
        date=msg.date,
        is_outgoing=bool(getattr(msg, "out", False)),
    )
