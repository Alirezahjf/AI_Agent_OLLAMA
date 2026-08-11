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


from .storage import TelegramStorage

class PersonalTelegram:
    """Async client for the user's personal Telegram account with Live Monitoring."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: Path,
        account_name: str = "اصلی",
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
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
        
        # Database Mirror
        self.db = TelegramStorage(self._session_path.parent / f"tg_{self._name}.db")
        
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._connected = False
        self._manual_disconnect = False
        self._connected_at: datetime | None = None
        self._last_error = ""
        self._on_event = on_event
        
        self._login_state = "disconnected"
        self._login_ctx: dict[str, Any] = {}

    # ... (متدهای قبلی I/O و Login باقی می‌مانند) ...

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

        self._thread = threading.Thread(target=runner, name=f"tg-{self._name}-loop", daemon=True)
        self._thread.start()
        ready_evt.wait(timeout=10)
        self._loop = loop_holder["loop"]

    async def _setup_event_handlers(self):
        """Register live listeners for all Telegram events."""
        from telethon import events
        
        @self._client.on(events.NewMessage())
        async def handler(event):
            # Update DB with new message
            if event.message:
                chat = await event.get_chat()
                # Sync chat to DB if not exists
                await self._sync_entity_to_db(chat)
                
                # Notify Bridge
                if self._on_event:
                    self._on_event({
                        "type": "new_message",
                        "chat_id": event.chat_id,
                        "text": event.raw_text,
                        "sender_id": event.sender_id
                    })

        @self._client.on(events.UserUpdate())
        async def user_handler(event):
            # Listen for online/offline status
            if self._on_event:
                self._on_event({
                    "type": "user_update",
                    "user_id": event.user_id,
                    "online": event.online if hasattr(event, 'online') else None
                })

    async def _sync_entity_to_db(self, entity):
        """Mirror a Telethon entity to local SQLite."""
        try:
            from telethon.tl.types import User, Chat, Channel
            e_id = entity.id
            e_type = "user"
            if isinstance(entity, Channel):
                e_type = "channel" if not getattr(entity, 'megagroup', False) else "supergroup"
            elif isinstance(entity, Chat):
                e_type = "group"
                
            data = {
                "id": e_id,
                "username": getattr(entity, 'username', None),
                "phone": getattr(entity, 'phone', None),
                "title": getattr(entity, 'title', None) or f"{getattr(entity, 'first_name', '')} {getattr(entity, 'last_name', '')}".strip(),
                "first_name": getattr(entity, 'first_name', None),
                "last_name": getattr(entity, 'last_name', None),
                "type": e_type,
                "bio": None, # Needs full request
                "about": None,
                "participants_count": getattr(entity, 'participants_count', 0),
                "unread_count": 0 # Logic for unread
            }
            self.db.save_entity(data)
        except Exception as e:
            logger.debug(f"Sync failed for {entity}: {e}")

    async def _finish_login(self) -> dict[str, Any]:
        self._login_state = "connected"
        self._connected = True
        self._connected_at = datetime.now()
        
        # Start background listeners
        await self._setup_event_handlers()
        
        # Initial sync of all dialogs (in background)
        asyncio.create_task(self._initial_sync())
        
        me = await self._get_me()
        return {
            "state": "connected",
            "message": f"connected as {me.get('username') or me.get('first_name') or '?'}",
            "user": me,
        }

    async def _initial_sync(self):
        """Full sync of dialogs and contacts to the local mirror."""
        try:
            async for dialog in self._client.iter_dialogs(limit=1000):
                await self._sync_entity_to_db(dialog.entity)
            
            from telethon.tl.functions.contacts import GetContactsRequest
            result = await self._client(GetContactsRequest(hash=0))
            for user in getattr(result, 'users', []):
                await self._sync_entity_to_db(user)
        except Exception as e:
            logger.warning(f"Initial sync background failed: {e}")

    # ==================== ADVANCED MANAGEMENT METHODS ====================

    async def _get_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions/devices connected to this account."""
        from telethon.tl.functions.account import GetAuthorizationsRequest
        result = await self._client(GetAuthorizationsRequest())
        sessions = []
        for auth in result.authorizations:
            sessions.append({
                "hash": auth.hash,
                "device_model": auth.device_model,
                "platform": auth.platform,
                "system_version": auth.system_version,
                "api_id": auth.api_id,
                "app_name": auth.app_name,
                "app_version": auth.app_version,
                "date_created": auth.date_created.isoformat(),
                "date_active": auth.date_active.isoformat(),
                "ip": auth.ip,
                "country": auth.country,
                "region": auth.region
            })
        return sessions

    async def _terminate_session(self, session_hash: int) -> bool:
        """Log out a specific device by its session hash."""
        from telethon.tl.functions.account import ResetAuthorizationRequest
        await self._client(ResetAuthorizationRequest(hash=int(session_hash)))
        return True

    async def _get_privacy_settings(self) -> dict[str, Any]:
        """Read all privacy settings (Who can see phone, online status, etc)."""
        from telethon.tl.functions.account import GetPrivacyRequest
        from telethon.tl.types import InputPrivacyKeyPhoneNumber, InputPrivacyKeyStatusTimestamp, InputPrivacyKeyChatInvite
        
        keys = {
            "phone_number": InputPrivacyKeyPhoneNumber(),
            "last_seen": InputPrivacyKeyStatusTimestamp(),
            "group_invites": InputPrivacyKeyChatInvite()
        }
        results = {}
        for label, key in keys.items():
            res = await self._client(GetPrivacyRequest(key=key))
            results[label] = str(res.rules) # Simplified for now
        return results

    async def _export_chat(self, chat, limit: int = 1000) -> str:
        """Export chat history to a JSON file in workspace."""
        entity = await self._resolve_entity(chat)
        messages = []
        async for msg in self._client.iter_messages(entity, limit=limit):
            messages.append({
                "id": msg.id,
                "date": msg.date.isoformat(),
                "sender": str(msg.sender_id),
                "text": msg.text or "[Media]"
            })
        
        import json
        file_name = f"export_{getattr(entity, 'id', 'chat')}.json"
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        return file_name

    # ==================== SYNC WRAPPERS FOR REPL/CLI ====================

    def get_sessions(self) -> list[dict[str, Any]]:
        return self._run(self._get_sessions())

    def terminate_session(self, session_hash: int) -> bool:
        return self._run(self._terminate_session(session_hash))

    def get_privacy_settings(self) -> dict[str, Any]:
        return self._run(self._get_privacy_settings())

    def export_chat(self, chat, limit: int = 1000) -> str:
        return self._run(self._export_chat(chat, limit))

    # ==================== DEEP CONTENT & GLOBAL SEARCH ====================

    async def _global_search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search for users, chats, and channels across all of Telegram."""
        from telethon.tl.functions.contacts import SearchRequest
        result = await self._client(SearchRequest(q=query, limit=limit))
        found = []
        for user in result.users:
            found.append({
                "id": user.id,
                "name": f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '') or ''}".strip(),
                "username": getattr(user, 'username', ''),
                "type": "user"
            })
        for chat in result.chats:
            found.append({
                "id": chat.id,
                "title": getattr(chat, 'title', ''),
                "username": getattr(chat, 'username', ''),
                "type": "channel/group"
            })
        return found

    async def _get_full_chat_details(self, chat) -> dict[str, Any]:
        """Get every bit of info about a chat: permissions, members count, full bio, etc."""
        entity = await self._resolve_entity(chat)
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import User, Channel, Chat
        
        full_info = {}
        try:
            if isinstance(entity, User):
                res = await self._client(GetFullUserRequest(id=entity))
                full_info = {
                    "id": entity.id,
                    "about": getattr(res.full_user, 'about', ''),
                    "common_chats": res.full_user.common_chats_count,
                    "is_blocked": res.full_user.blocked,
                    "verified": entity.verified,
                    "premium": getattr(entity, 'premium', False)
                }
            else:
                res = await self._client(GetFullChannelRequest(channel=entity))
                full_info = {
                    "id": entity.id,
                    "about": res.full_chat.about,
                    "participants_count": getattr(res.full_chat, 'participants_count', 0),
                    "admins_count": getattr(res.full_chat, 'admins_count', 0),
                    "linked_chat_id": getattr(res.full_chat, 'linked_chat_id', None),
                    "slowmode_enabled": getattr(res.full_chat, 'slowmode_enabled', False)
                }
        except Exception as e:
            logger.debug(f"Full details failed: {e}")
            full_info = {"id": getattr(entity, 'id', 'unknown'), "error": "Could not fetch full details"}
            
        return full_info

    async def _get_pinned_messages(self, chat) -> list[dict[str, Any]]:
        """Fetch all pinned messages in a chat."""
        entity = await self._resolve_entity(chat)
        messages = []
        async for msg in self._client.iter_messages(entity, pinned=True):
            messages.append({"id": msg.id, "text": msg.text or "[Media]"})
        return messages

    # ==================== REPL WRAPPERS ====================

    def global_search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._run(self._global_search(query, limit))

    def get_full_chat_details(self, chat) -> dict[str, Any]:
        return self._run(self._get_full_chat_details(chat))

    def get_pinned_messages(self, chat) -> list[dict[str, Any]]:
        return self._run(self._get_pinned_messages(chat))

    async def _resolve_entity(self, target):
        """Advanced entity resolver: ID -> Cache -> DB -> Server Search."""
        # 1. Direct Numeric ID
        try:
            e_id = int(target)
            return await self._client.get_entity(e_id)
        except (ValueError, TypeError):
            pass

        # 2. String Query (Local DB Fuzzy Search First)
        target_str = str(target).strip()
        local_results = self.db.search_entities(target_str, limit=1)
        if local_results:
            try:
                return await self._client.get_entity(local_results[0]['id'])
            except Exception:
                pass

        # 3. Global Server Search (Fallback)
        try:
            return await self._client.get_entity(target_str)
        except Exception as exc:
            raise TelegramError(f"چت {target!r} نه در حافظه و نه در سرور پیدا نشد.") from exc


    async def _list_chats(self, limit: int, *, kind: str = "all", query: str = "", sort: str = "") -> list[Chat]:
        chats: list[Chat] = []
        async for dialog in self._client.iter_dialogs(limit=max(1, limit)):
            entity = dialog.entity
            title = getattr(entity, "title", None) or " ".join(
                p for p in (getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or "") if p
            ) or "?"
            username = getattr(entity, "username", None)
            type_name = type(entity).__name__.lower()
            is_channel = type_name == "channel" and not bool(getattr(entity, "megagroup", False))
            is_group = bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                            or getattr(entity, "is_group", False) or type_name == "chat")
            is_bot = bool(type_name == "user" and getattr(entity, "bot", False))
            is_private = bool(type_name == "user" and not is_bot)
            chat = Chat(id=int(dialog.id), title=str(title), username=username, is_group=is_group,
                        is_channel=is_channel, is_bot=is_bot, is_private=is_private,
                        is_forum=bool(getattr(entity, "forum", False)),
                        verified=bool(getattr(entity, "verified", False)),
                        pinned=bool(getattr(dialog, "pinned", False)),
                        last_message=((dialog.message.message or "")[:140] if dialog.message is not None else None),
                        unread_count=int(dialog.unread_count or 0))
            if kind != "all" and not getattr(chat, f"is_{kind}"):
                continue
            if query and str(query).lower() not in f"{chat.title} {chat.username or ''}".lower():
                continue
            chats.append(chat)
        if sort == "unread":
            chats.sort(key=lambda item: item.unread_count, reverse=True)
        return chats[:max(1, limit)]

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

    async def _search_contacts(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            raise TelegramError("عبارت جست‌وجوی مخاطب خالی است")
        
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await self._client(GetContactsRequest(hash=0))
        users = getattr(result, "users", [])
        
        results: list[dict[str, Any]] = []
        for user in users:
            name = " ".join(
                p for p in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if p
            )
            username = getattr(user, "username", "") or ""
            phone = getattr(user, "phone", "") or ""
            haystack = f"{name} {username} {phone}".lower()
            if q.lower() in haystack:
                results.append({
                    "id": user.id,
                    "name": name,
                    "username": username,
                    "phone": phone,
                })
                if len(results) >= max(1, limit):
                    break
        return results

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
