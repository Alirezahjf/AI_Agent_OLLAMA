"""Tests for the Bridge Telegram/Bale bot front-end.

Only the pure presentation logic is covered here (inline keyboards and
help text) — no network, no event loop.  ``python-telegram-bot`` is a
core dependency so these always run.
"""

from __future__ import annotations

import pytest

pytest.importorskip("telegram", reason="python-telegram-bot is not installed")


def _bot():
    from local_agent.bridge.telegram_bot.bot import BridgeTelegramBot
    from local_agent.core.config import AssistantSettings

    return BridgeTelegramBot(AssistantSettings(), object(), "token")


def test_menu_keyboard_layout() -> None:
    rows = _bot()._menu().inline_keyboard
    assert len(rows) == 3
    data = [button.callback_data for row in rows for button in row]
    assert data == [
        "menu:status", "menu:actions", "menu:history", "menu:reset", "menu:help",
    ]
    labels = [button.text for row in rows for button in row]
    assert all(label.strip() for label in labels)


def test_approval_keyboard_encodes_the_request_id() -> None:
    markup = _bot()._approval_keyboard("abc123")
    buttons = markup.inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["ok:abc123", "no:abc123"]


def test_approval_keyboard_escalates_wording_for_system_risk() -> None:
    bot = _bot()
    normal = bot._approval_keyboard("x", "destructive").inline_keyboard[0][0].text
    system = bot._approval_keyboard("x", "system").inline_keyboard[0][0].text
    assert normal != system
    assert "مطمئن" in system


def test_help_text_lists_the_commands() -> None:
    text = _bot()._help_text()
    for command in ("/status", "/actions", "/history", "/model", "/reset"):
        assert command in text


def test_bale_bot_uses_settings_bale_base_url() -> None:
    """P3: Bale bot must read bale_base_url from settings, not AttributeError."""
    from local_agent.bridge.telegram_bot.bot import BridgeBaleBot
    from local_agent.core.config import AssistantSettings

    settings = AssistantSettings(bale_base_url="https://tapi.bale.ai")
    bot = BridgeBaleBot(settings, object(), "test-token")
    # The application() method should not crash when building the URL
    # (it will fail when trying to connect, but the URL construction must work)
    try:
        app = bot.application()
    except Exception as exc:
        # We don't expect the bot to actually connect, but we also don't
        # expect AttributeError for missing bale_base_url
        assert "bale_base_url" not in str(exc).lower()
        assert "AttributeError" not in str(exc)


def test_settings_has_bot_tokens() -> None:
    """P3: AssistantSettings must have telegram_token and bale_token."""
    from local_agent.core.config import AssistantSettings
    settings = AssistantSettings(
        telegram_token="tg-123",
        bale_token="bale-456",
        bale_base_url="https://tapi.bale.ai",
        allowed_user_ids=frozenset({1, 2, 3}),
    )
    assert settings.telegram_token == "tg-123"
    assert settings.bale_token == "bale-456"
    assert settings.bale_base_url == "https://tapi.bale.ai"
    assert settings.allowed_user_ids == frozenset({1, 2, 3})


def test_settings_bot_tokens_serialize() -> None:
    """P3: Bot tokens must survive JSON round-trip."""
    from local_agent.core.config import AssistantSettings
    settings = AssistantSettings(
        telegram_token="tg-123",
        bale_token="bale-456",
        allowed_user_ids=frozenset({1, 2}),
    )
    d = settings.to_dict()
    assert d["telegram_token"] == "tg-123"
    assert d["bale_token"] == "bale-456"
    assert d["allowed_user_ids"] == [1, 2]
