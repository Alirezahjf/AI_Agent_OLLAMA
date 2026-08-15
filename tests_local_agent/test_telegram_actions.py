"""Tests for the telegram.* actions and the config_set action.

Everything is offline: the PersonalTelegram client is replaced with a
tiny fake and the settings live in a tmp_path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from local_agent.actions import run_action
from local_agent.actions.registry import ActionContext, Risk
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings
from local_agent.core.errors import ActionRefused, AssistantError
from local_agent.telegram.client import Chat, Message, TelegramError


class _FakeTelegram:
    """Minimal stand-in for PersonalTelegram (actions layer only)."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.calls: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    def list_chats(self, limit: int = 30, **kwargs) -> list[Chat]:
        self.calls.append("list_chats")
        return [Chat(id=10, title="Alice", username="alice", is_group=False)]

    def search_messages(self, chat, query: str, limit: int = 30) -> list[Message]:
        self.calls.append("search_messages")

        return [Message(id=1, chat_id=10, sender="alice", text="hit", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=False)]

    def get_me(self) -> dict[str, Any]:
        self.calls.append("get_me")
        return {"id": 1, "first_name": "Test", "last_name": "", "username": "tester", "phone": "+100000"}

    def resolve_target(self, target) -> dict[str, Any]:
        self.calls.append("resolve_target")
        return {
            "id": 10, "raw_id": 10, "name": "Alice", "username": "alice",
            "phone": "+100", "kind": "private",
        }

    def get_statistics(self) -> dict[str, Any]:
        self.calls.append("get_statistics")
        return {"total_chats": 4, "private_chats": 1, "bot_chats": 1, "group_chats": 1,
                "supergroup_chats": 0, "channel_chats": 1, "unread_chats": 2, "total_unread": 5}

    def list_unread_chats(self, limit=30):
        self.calls.append("list_unread_chats")
        return [Chat(id=10, title="Alice", username="alice", is_group=False, unread_count=5)]

    def refresh_summary(self):
        self.calls.append("refresh_summary")
        return {"total_chats": 4, "total_contacts": 2, "total_unread": 5, "refreshed_at": "now"}

    def send_message(self, chat, text: str) -> Message:
        self.calls.append("send_message")

        return Message(id=99, chat_id=10, sender="me", text=text, date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def send_photo(self, chat, path, caption: str = "") -> Message:
        self.calls.append("send_photo")

        return Message(id=100, chat_id=10, sender="me", text=caption or "", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)

    def send_file(self, chat, path, caption: str = "") -> Message:
        self.calls.append("send_file")

        return Message(id=101, chat_id=10, sender="me", text=caption or "", date=datetime(2024, 1, 1, tzinfo=UTC), is_outgoing=True)


@pytest.fixture
def handlers(tmp_path: Path) -> BridgeHandlers:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return BridgeHandlers.build(settings)


@pytest.fixture
def ctx(handlers: BridgeHandlers) -> ActionContext:
    return handlers.context


def test_telegram_actions_are_registered(handlers: BridgeHandlers) -> None:
    names = {a.name for a in handlers.registry.all()}
    for expected in (
        "telegram.list_chats",
        "telegram.search_messages",
        "telegram.get_me",
        "telegram.resolve_target",
        "telegram.get_statistics",
        "telegram.list_unread_chats",
        "telegram.get_chat_statistics",
        "telegram.export_chat",
        "telegram.download_media_batch",
        "telegram.refresh",
        "telegram.bulk_send",
        "telegram.bulk_forward",
        "telegram.send_message",
        "telegram.send_photo",
        "telegram.send_file",
    ):
        assert expected in names, expected


def test_telegram_actions_report_risk_levels(handlers: BridgeHandlers) -> None:
    by_name = {a.name: a for a in handlers.registry.all()}
    assert by_name["telegram.list_chats"].risk_level == Risk.SAFE
    assert by_name["telegram.search_messages"].risk_level == Risk.SAFE
    assert by_name["telegram.get_me"].risk_level == Risk.SAFE
    assert by_name["telegram.resolve_target"].risk_level == Risk.SAFE
    for name in ("telegram.get_statistics", "telegram.list_unread_chats", "telegram.refresh"):
        assert by_name[name].risk_level == Risk.SAFE
    for name in ("telegram.send_message", "telegram.send_photo", "telegram.send_file",
                 "telegram.bulk_send", "telegram.bulk_forward"):
        assert by_name[name].risk_level == Risk.DESTRUCTIVE, name


def test_telegram_actions_without_client_give_helpful_error(handlers: BridgeHandlers) -> None:
    handlers.context.extra["telegram"] = None
    from local_agent.core.errors import DependencyMissing

    with pytest.raises(DependencyMissing) as excinfo:
        run_action(handlers.registry, "telegram.list_chats", {}, handlers.context)
    assert "تلگرام" in excinfo.value.install_hint


def test_telegram_actions_disconnected_give_helpful_error(handlers: BridgeHandlers) -> None:
    handlers.context.extra["telegram"] = _FakeTelegram(connected=False)
    from local_agent.core.errors import DependencyMissing

    with pytest.raises(DependencyMissing):
        run_action(handlers.registry, "telegram.get_me", {}, handlers.context)


def test_list_chats_and_get_me_run(handlers: BridgeHandlers, ctx: ActionContext) -> None:
    fake = _FakeTelegram()
    ctx.extra["telegram"] = fake
    result = run_action(handlers.registry, "telegram.list_chats", {"limit": 5}, ctx)
    assert "Alice" in result
    me = run_action(handlers.registry, "telegram.get_me", {}, ctx)
    assert "tester" in me
    assert fake.calls == ["list_chats", "get_me"]


def test_search_messages_returns_hits(handlers: BridgeHandlers, ctx: ActionContext) -> None:
    ctx.extra["telegram"] = _FakeTelegram()
    result = run_action(handlers.registry, "telegram.search_messages", {"chat": "Alice", "query": "hit"}, ctx)
    assert "hit" in result
    assert "id=1" in result
    assert "نوع=text" in result


def test_resolve_target_action_returns_stable_identity(
    handlers: BridgeHandlers, ctx: ActionContext
) -> None:
    fake = _FakeTelegram()
    ctx.extra["telegram"] = fake
    result = run_action(handlers.registry, "telegram.resolve_target", {"target": "Alice"}, ctx)
    assert "id=10" in result
    assert "@alice" in result
    assert "نوع=private" in result
    assert fake.calls == ["resolve_target"]


def test_live_statistics_unread_and_refresh_actions(
    handlers: BridgeHandlers, ctx: ActionContext
) -> None:
    fake = _FakeTelegram()
    ctx.extra["telegram"] = fake
    assert "کل چت‌ها: 4" in run_action(handlers.registry, "telegram.get_statistics", {}, ctx)
    assert "Alice" in run_action(handlers.registry, "telegram.list_unread_chats", {}, ctx)
    assert "2 مخاطب" in run_action(handlers.registry, "telegram.refresh", {}, ctx)
    assert fake.calls == ["get_statistics", "list_unread_chats", "refresh_summary"]


def test_action_layer_preserves_structured_telegram_errors(
    handlers: BridgeHandlers, ctx: ActionContext
) -> None:
    fake = _FakeTelegram()

    def fail():
        raise TelegramError("محدودیت تلگرام", code="flood_wait", retry_after=30)

    fake.get_statistics = fail
    ctx.extra["telegram"] = fake
    with pytest.raises(TelegramError) as excinfo:
        run_action(handlers.registry, "telegram.get_statistics", {}, ctx)
    assert excinfo.value.code == "flood_wait"
    assert excinfo.value.retry_after == 30


def test_bulk_send_requires_confirmation_and_limits_targets(
    handlers: BridgeHandlers, ctx: ActionContext
) -> None:
    fake = _FakeTelegram()
    ctx.extra["telegram"] = fake
    handlers.gate.auto_deny()
    with pytest.raises(ActionRefused):
        run_action(
            handlers.registry, "telegram.bulk_send",
            {"targets": ["Alice", "Bob"], "text": "hello"}, ctx,
        )
    handlers.gate.auto_approve()
    result = run_action(
        handlers.registry, "telegram.bulk_send",
        {"targets": ["Alice", "Bob"], "text": "hello"}, ctx,
    )
    assert result.count("✅") == 2
    with pytest.raises(AssistantError, match="۲۰"):
        run_action(
            handlers.registry, "telegram.bulk_send",
            {"targets": [str(i) for i in range(21)], "text": "hello"}, ctx,
        )


def test_send_message_is_refused_without_approval(handlers: BridgeHandlers, ctx: ActionContext) -> None:
    ctx.extra["telegram"] = _FakeTelegram()
    handlers.gate.auto_deny()
    with pytest.raises(ActionRefused):
        run_action(handlers.registry, "telegram.send_message", {"chat": "Alice", "text": "سلام"}, ctx)


def test_send_message_succeeds_with_approval(handlers: BridgeHandlers, ctx: ActionContext) -> None:
    fake = _FakeTelegram()
    ctx.extra["telegram"] = fake
    handlers.gate.auto_approve()
    result = run_action(handlers.registry, "telegram.send_message", {"chat": "Alice", "text": "سلام"}, ctx)
    assert "ارسال شد" in result
    assert fake.calls == ["send_message"]


def test_confirm_send_is_honoured_even_in_never_mode(handlers: BridgeHandlers) -> None:
    """telegram.confirm_send=True must ask even when confirm_mode=never."""
    by_name = {a.name: a for a in handlers.registry.all()}
    action = by_name["telegram.send_message"]
    # Simulate confirm_mode=never while confirm_send stays True (default).
    safety = type(handlers.settings.safety)(**{**handlers.settings.safety.__dict__, "confirm_mode": "never"})
    assert action.needs_confirmation(safety) is True

    # Turning confirm_send off restores policy behaviour.
    handlers.apply_config_set("telegram.confirm_send", False)
    assert handlers.settings.telegram.confirm_send is False
    action2 = by_name["telegram.send_message"]
    assert action2.needs_confirmation(safety) is False


# ---------------------------------------------------------------------------
# config_set
# ---------------------------------------------------------------------------


def test_config_set_persists_telegram_credentials(handlers: BridgeHandlers, tmp_path: Path) -> None:
    result = run_action(
        handlers.registry,
        "config_set",
        {"path": "telegram.api_id", "value": "123456"},
        handlers.context,
    )
    assert "ذخیره شد" in result
    run_action(handlers.registry, "config_set", {"path": "telegram.api_hash", "value": "deadbeef"}, handlers.context)
    run_action(handlers.registry, "config_set", {"path": "telegram.phone", "value": "+989120000000"}, handlers.context)
    run_action(handlers.registry, "config_set", {"path": "telegram.enabled", "value": "true"}, handlers.context)

    assert handlers.settings.telegram.api_id == 123456
    assert handlers.settings.telegram.api_hash == "deadbeef"
    assert handlers.settings.telegram.phone == "+989120000000"
    assert handlers.settings.telegram.enabled is True
    # The value must be on disk for the next restart (multi-account format:
    # the active «اصلی» account carries the credentials).
    import json

    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    active = next(a for a in payload["telegram"]["accounts"] if a["name"] == "اصلی")
    assert active["api_id"] == 123456
    assert active["api_hash"] == "deadbeef"


def test_config_set_never_echoes_secrets(handlers: BridgeHandlers) -> None:
    result = run_action(
        handlers.registry,
        "config_set",
        {"path": "telegram.api_hash", "value": "super-secret-hash"},
        handlers.context,
    )
    assert "super-secret-hash" not in result
    result2 = run_action(
        handlers.registry,
        "config_set",
        {"path": "llm.openai_api_key", "value": "sk-super-secret"},
        handlers.context,
    )
    assert "sk-super-secret" not in result2


def test_config_set_rejects_unknown_path(handlers: BridgeHandlers) -> None:
    with pytest.raises(AssistantError):
        run_action(handlers.registry, "config_set", {"path": "nope.not_real", "value": "x"}, handlers.context)


def test_config_set_updates_work_dir(handlers: BridgeHandlers, tmp_path: Path) -> None:
    new_dir = tmp_path / "ws2"
    result = run_action(handlers.registry, "config_set", {"path": "work_dir", "value": str(new_dir)}, handlers.context)
    assert "ذخیره شد" in result
    assert handlers.settings.work_dir == new_dir
    assert handlers.context.work_dir == new_dir


def test_config_set_creates_telegram_client_when_enabled(handlers: BridgeHandlers) -> None:
    run_action(handlers.registry, "config_set", {"path": "telegram.api_id", "value": "111"}, handlers.context)
    run_action(handlers.registry, "config_set", {"path": "telegram.api_hash", "value": "h" * 32}, handlers.context)
    run_action(handlers.registry, "config_set", {"path": "telegram.phone", "value": "+100"}, handlers.context)
    run_action(handlers.registry, "config_set", {"path": "telegram.enabled", "value": True}, handlers.context)
    assert handlers.telegram is not None
    assert handlers.telegram.is_connected is False  # still needs the code flow
    assert handlers.context.extra["telegram"] is handlers.telegram
