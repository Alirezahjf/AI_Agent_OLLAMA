"""F2 (multi-account Telegram) + F5 (full Telethon tools) + B4.

All offline: account clients are replaced with fakes and no network is
touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from local_agent.actions import run_action
from local_agent.actions.registry import Risk
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings, TelegramAccount, TelegramSettings
from local_agent.core.errors import AssistantError
from local_agent.telegram.client import Chat, Message


class _FakeClient:
    """Stand-in for PersonalTelegram recording per-account calls."""

    def __init__(self, name: str, *, connected: bool = True) -> None:
        self.name = name
        self._connected = connected
        self.state = "connected" if connected else "disconnected"
        self.calls: list[str] = []
        self.login_calls: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def login_state(self) -> str:
        return self.state

    # ---- F5 read / send -------------------------------------------------
    def list_chats(self, limit: int = 30, **kwargs) -> list[Chat]:
        self.calls.append(f"list_chats:{self.name}")
        return [Chat(id=10, title="Alice", username="alice", is_group=False)]

    def get_me(self) -> dict[str, Any]:
        self.calls.append(f"get_me:{self.name}")
        return {"id": 1, "first_name": self.name, "username": self.name, "phone": "+1"}

    def search_contacts(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        self.calls.append(f"search_contacts:{self.name}")
        return [{"id": 5, "name": "Bob", "username": "bob", "phone": "+2"}]

    def get_chat_history(self, chat, limit: int = 30, offset_id: int = 0) -> list[Message]:
        self.calls.append(f"get_chat_history:{self.name}")
        return [Message(id=1, chat_id=10, sender="a", text="hi", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=False)]

    def get_profile(self, chat, media_dir: Path) -> dict[str, Any]:
        self.calls.append(f"get_profile:{self.name}")
        return {"id": 1, "name": "Ali", "username": "ali", "bio": "bio", "phone": "+1", "is_group": False, "photo_path": ""}

    def send_media(self, chat, path, caption: str = "", kind: str = "document") -> Message:
        self.calls.append(f"send_{kind}:{self.name}")
        return Message(id=9, chat_id=1, sender="me", text="", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def send_message(self, chat, text: str) -> Message:
        self.calls.append(f"send_message:{self.name}")
        return Message(id=9, chat_id=1, sender="me", text=text, date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def send_location(self, chat, lat, lng) -> Message:
        self.calls.append(f"send_location:{self.name}")
        return Message(id=9, chat_id=1, sender="me", text="", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def download_media(self, chat, msg_id, filename, media_dir) -> Path:
        self.calls.append(f"download_media:{self.name}")
        media_dir.mkdir(parents=True, exist_ok=True)
        p = media_dir / (filename or f"{msg_id}")
        p.write_bytes(b"x")
        return p

    def reply_to(self, chat, msg_id, text) -> Message:
        self.calls.append(f"reply_to:{self.name}")
        return Message(id=9, chat_id=1, sender="me", text=text, date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def forward_message(self, chat, from_chat, msg_id) -> Message:
        self.calls.append(f"forward_message:{self.name}")
        return Message(id=9, chat_id=1, sender="me", text="", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def mark_read(self, chat) -> None:
        self.calls.append(f"mark_read:{self.name}")

    def resolve_username(self, username) -> dict[str, Any]:
        self.calls.append(f"resolve_username:{self.name}")
        return {"id": 3, "name": "Public", "username": "public", "is_group": True}

    # ---- login ----------------------------------------------------------
    def start_login(self) -> dict[str, Any]:
        self.login_calls.append("start_login")
        self._connected = False
        self.state = "await_code"
        return {"state": "await_code", "message": "کد ارسال شد"}

    def submit_code(self, code: str) -> dict[str, Any]:
        self.login_calls.append(f"code:{code}")
        self.state = "await_2fa"
        return {"state": "await_2fa", "message": "2FA"}

    def submit_password(self, password: str) -> dict[str, Any]:
        self.login_calls.append(f"password:{password}")
        self._connected = True
        self.state = "connected"
        return {"state": "connected", "message": "ok"}

    def disconnect(self) -> None:
        self._connected = False


def _two_account_settings(data_dir: Path, work_dir: Path) -> AssistantSettings:
    return AssistantSettings(
        data_dir=data_dir, work_dir=work_dir,
        telegram=TelegramSettings(
            enabled=True, active_account="اصلی",
            accounts=(
                TelegramAccount(name="اصلی", enabled=True, api_id=111, api_hash="a" * 32,
                                phone="+989120000000", session_name="main", confirm_send=True),
                TelegramAccount(name="کار", enabled=True, api_id=222, api_hash="b" * 32,
                                phone="+989150000000", session_name="work", confirm_send=False),
            ),
        ),
    )


def _handlers_with_fakes(settings: AssistantSettings) -> BridgeHandlers:
    handlers = BridgeHandlers.build(settings)
    handlers._telegram_accounts = {
        "اصلی": _FakeClient("اصلی"),
        "کار": _FakeClient("کار"),
    }
    handlers.telegram = handlers._telegram_accounts["اصلی"]
    handlers.context.extra["telegram"] = handlers.telegram
    return handlers


# ---------------------------------------------------------------------------
# F2 — multi-account
# ---------------------------------------------------------------------------


def test_two_accounts_have_two_distinct_session_files(tmp_path: Path) -> None:
    settings = _two_account_settings(tmp_path, tmp_path)
    main = settings.telegram_session_path_for("اصلی")
    work = settings.telegram_session_path_for("کار")
    assert main != work
    assert main.name == "main.session"
    assert work.name == "work.session"
    assert main.parent == work.parent == tmp_path / "sessions"


def test_switch_account_changes_active(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    handlers.switch_telegram_account("کار")
    assert handlers.settings.telegram.active_account == "کار"
    assert handlers.telegram is handlers._telegram_accounts["کار"]
    with pytest.raises(AssistantError) as exc:
        handlers.switch_telegram_account("وجودندارد")
    assert "وجود ندارد" in str(exc.value)


def test_accounts_status_exposes_no_secrets(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    status = handlers.telegram_accounts_status()
    assert status["active_account"] == "اصلی"
    assert len(status["accounts"]) == 2
    for acc in status["accounts"]:
        assert "api_hash" not in acc
        assert acc["phone"]


def test_send_uses_active_account_by_default(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    handlers.gate.auto_approve()
    run_action(handlers.registry, "telegram.send_message",
               {"chat": "Alice", "text": "hi"}, handlers.context)
    assert handlers._telegram_accounts["اصلی"].calls == ["send_message:اصلی"]
    assert handlers._telegram_accounts["کار"].calls == []


def test_send_on_explicit_account(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    handlers.gate.auto_approve()
    run_action(handlers.registry, "telegram.send_message",
               {"chat": "Alice", "text": "hi", "account": "کار"}, handlers.context)
    assert handlers._telegram_accounts["کار"].calls == ["send_message:کار"]


def test_unknown_account_name_gives_persian_error(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    handlers.gate.auto_approve()
    with pytest.raises(AssistantError) as exc:
        run_action(handlers.registry, "telegram.send_message",
                   {"chat": "Alice", "text": "hi", "account": "ghost"}, handlers.context)
    assert "وجود ندارد" in str(exc.value)


def test_independent_login_flows_per_account(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    # Account «اصلی» logs in to await_code → await_2fa → connected.
    r = handlers.start_telegram_login("اصلی")
    assert r["state"] == "await_code"
    r = handlers.submit_telegram_code("12345", "اصلی")
    assert r["state"] == "await_2fa"
    r = handlers.submit_telegram_password("p4ss", "اصلی")
    assert r["state"] == "connected"
    assert handlers._telegram_accounts["اصلی"].login_calls == ["start_login", "code:12345", "password:p4ss"]
    # «کار» has its own independent flow.
    r = handlers.start_telegram_login("کار")
    assert r["state"] == "await_code"
    assert handlers._telegram_accounts["کار"].login_calls == ["start_login"]


def test_confirm_send_is_per_account(tmp_path: Path) -> None:
    settings = _two_account_settings(tmp_path, tmp_path)
    handlers = BridgeHandlers.build(settings)
    by_name = {a.name: a for a in handlers.registry.all()}
    action = by_name["telegram.send_message"]
    # «کار» has confirm_send=False → no confirmation even in destructive mode.
    assert action.needs_confirmation(handlers.settings.safety, {"account": "کار"}) is False
    # «اصلی» has confirm_send=True → asks.
    assert action.needs_confirmation(handlers.settings.safety, {"account": "اصلی"}) is True


def test_multi_account_actions_registered(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    names = {a.name for a in handlers.registry.all()}
    for expected in (
        "telegram.list_accounts", "telegram.switch_account",
        "telegram.search_contacts", "telegram.get_chat_history", "telegram.get_profile",
        "telegram.download_media", "telegram.mark_read", "telegram.resolve_username",
        "telegram.resolve_target", "telegram.get_statistics", "telegram.list_unread_chats",
        "telegram.get_chat_statistics", "telegram.export_chat", "telegram.download_media_batch",
        "telegram.refresh", "telegram.bulk_send", "telegram.bulk_forward",
        "telegram.send_video", "telegram.send_voice", "telegram.send_audio",
        "telegram.send_document", "telegram.send_sticker", "telegram.send_animation",
        "telegram.send_location", "telegram.reply_to", "telegram.forward_message",
    ):
        assert expected in names, expected


# ---------------------------------------------------------------------------
# F5 — full tools (action layer, fake client)
# ---------------------------------------------------------------------------


def test_f5_tools_run_and_report(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    ctx = handlers.context

    contact_result = run_action(
        handlers.registry, "telegram.search_contacts", {"query": "bo"}, ctx
    )
    assert "bob" in contact_result
    assert "id=5" in contact_result
    assert "hi" in run_action(handlers.registry, "telegram.get_chat_history", {"chat": "Alice"}, ctx)
    assert "ali" in run_action(handlers.registry, "telegram.get_profile", {"chat": "Alice"}, ctx)
    dl = run_action(handlers.registry, "telegram.download_media",
                    {"chat": "Alice", "msg_id": 3, "filename": "clip.png"}, ctx)
    assert "media" in dl
    assert (tmp_path / "media" / "clip.png").exists()
    run_action(handlers.registry, "telegram.mark_read", {"chat": "Alice"}, ctx)
    assert "public" in run_action(handlers.registry, "telegram.resolve_username", {"username": "public"}, ctx)

    handlers.gate.auto_approve()
    for name in ("send_video", "send_voice", "send_audio", "send_document",
                 "send_sticker", "send_animation"):
        result = run_action(handlers.registry, f"telegram.{name}",
                            {"chat": "Alice", "path": str(tmp_path / "x.bin")}, ctx)
        assert "ارسال شد" in result
    assert "ارسال شد" in run_action(handlers.registry, "telegram.send_location",
                                    {"chat": "Alice", "lat": 35.7, "lng": 51.4}, ctx)
    assert "ارسال شد" in run_action(handlers.registry, "telegram.reply_to",
                                    {"chat": "Alice", "msg_id": 2, "text": "پاسخ"}, ctx)
    assert "انتقال یافت" in run_action(handlers.registry, "telegram.forward_message",
                                       {"chat": "Bob", "from_chat": "Alice", "msg_id": 2}, ctx)


def test_contact_search_can_target_a_non_active_account(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    result = run_action(
        handlers.registry,
        "telegram.search_contacts",
        {"query": "bo", "account": "کار"},
        handlers.context,
    )
    assert "bob" in result
    assert handlers._telegram_accounts["کار"].calls == ["search_contacts:کار"]
    assert handlers._telegram_accounts["اصلی"].calls == []


def test_f5_risk_levels(tmp_path: Path) -> None:
    handlers = _handlers_with_fakes(_two_account_settings(tmp_path, tmp_path))
    by_name = {a.name: a for a in handlers.registry.all()}
    for safe in ("telegram.search_contacts", "telegram.get_chat_history", "telegram.get_profile",
                 "telegram.download_media", "telegram.mark_read", "telegram.resolve_username",
                 "telegram.list_accounts", "telegram.switch_account", "telegram.get_statistics",
                 "telegram.list_unread_chats", "telegram.get_chat_statistics", "telegram.export_chat",
                 "telegram.download_media_batch", "telegram.refresh"):
        assert by_name[safe].risk_level == Risk.SAFE, safe
    for dest in ("telegram.send_video", "telegram.send_voice", "telegram.send_audio",
                 "telegram.send_document", "telegram.send_sticker", "telegram.send_animation",
                 "telegram.send_location", "telegram.reply_to", "telegram.forward_message",
                 "telegram.bulk_send", "telegram.bulk_forward"):
        assert by_name[dest].risk_level == Risk.DESTRUCTIVE, dest


# ---------------------------------------------------------------------------
# B4 — GUI telegram send hidden when Telethon is enabled
# ---------------------------------------------------------------------------


def test_send_telegram_desktop_unavailable_when_telethon_enabled(tmp_path: Path) -> None:
    settings = _two_account_settings(tmp_path, tmp_path)
    handlers = BridgeHandlers.build(settings)
    by_name = {a.name: a for a in handlers.registry.all()}
    assert by_name["send_telegram_desktop"].unavailable is True


def test_send_telegram_desktop_available_when_telethon_disabled(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    handlers = BridgeHandlers.build(settings)
    by_name = {a.name: a for a in handlers.registry.all()}
    assert by_name["send_telegram_desktop"].unavailable is False
