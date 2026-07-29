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
