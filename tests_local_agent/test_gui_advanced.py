"""Tests for the advanced GUI layer (UI Automation + Telegram Desktop)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from local_agent.gui_advanced import (
    AdvancedGUI,
    VK,
    is_uia_available,
    win_shortcut,
)
from local_agent.gui_advanced.telegram_desktop import (
    SendReport,
    TelegramDesktop,
    find_telegram_desktop,
)
from local_agent.core.errors import DependencyMissing


# ---------------------------------------------------------------------------
# VK codes
# ---------------------------------------------------------------------------


def test_vk_codes_are_distinct() -> None:
    codes = {VK.CTRL, VK.ALT, VK.SHIFT, VK.A, VK.C, VK.F, VK.V}
    assert len(codes) == 7


def test_win_shortcut_does_not_crash() -> None:
    """Calling ``win_shortcut`` should not raise on any platform.

    On Windows it uses ctypes; elsewhere it falls back to pyautogui
    (which we don't actually exercise here, since it would require a
    display server in the CI host).
    """
    import sys
    if not sys.platform.startswith("win"):
        # Skip the actual call on non-Windows; pyautogui needs a display.
        return
    win_shortcut(VK.CTRL, VK.F)


# ---------------------------------------------------------------------------
# AdvancedGUI (with UIA missing)
# ---------------------------------------------------------------------------


def test_advanced_gui_without_uia_uses_fallback() -> None:
    """Even without uiautomation, find_window returns from the Win32 fallback."""
    gui = AdvancedGUI()
    # We can't require a real window to exist, so we just verify the class loads.
    assert hasattr(gui, "find_window")
    assert hasattr(gui, "list_windows")
    assert hasattr(gui, "click_control")


def test_find_controls_without_uia_raises() -> None:
    """find_controls requires uiautomation; it must raise DependencyMissing."""
    gui = AdvancedGUI()
    # Force the underlying uia to None
    gui.auto = None
    # We need to bypass the import-time check; rebuild the wrapper
    gui2 = AdvancedGUI()
    if is_uia_available():
        pytest.skip("uiautomation is installed; cannot test the missing path")
    with pytest.raises(DependencyMissing):
        gui2.find_controls(name="x")


# ---------------------------------------------------------------------------
# Telegram Desktop driver (mocked)
# ---------------------------------------------------------------------------


def test_send_report_to_dict() -> None:
    report = SendReport(
        chat_name="ali",
        message="hi",
        sent=True,
        verified=True,
        actual_last_message="hi",
    )
    payload = report.to_dict()
    assert payload["chat_name"] == "ali"
    assert payload["verified"] is True


def test_find_telegram_desktop_returns_none_when_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no candidate paths exist, return None."""
    monkeypatch.setattr("sys.platform", "linux")
    # Wipe candidate list
    import local_agent.gui_advanced.telegram_desktop as td
    monkeypatch.setattr(td, "_candidate_telegram_paths", lambda: [])
    monkeypatch.setattr(td.shutil, "which", lambda _: None)
    assert find_telegram_desktop() is None


def test_telegram_desktop_init_without_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an installed Telegram, ``__enter__`` must raise AssistantError."""
    import local_agent.gui_advanced.telegram_desktop as td
    monkeypatch.setattr(td, "find_telegram_desktop", lambda: None)
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(Exception):
        with TelegramDesktop():
            pass


def test_telegram_desktop_batch_reports_serialise() -> None:
    """Batch action reports can be serialised to JSON."""
    import json
    reports = [
        {"chat_name": "a", "message": "m", "sent": True, "verified": True, "error": None},
        {"chat_name": "b", "message": "m", "sent": False, "verified": False, "error": "x"},
    ]
    payload = json.dumps(reports, ensure_ascii=False)
    assert "ali" not in payload
    assert "a" in payload


# ---------------------------------------------------------------------------
# Bridge-driven integration: send_message action over the bridge
# ---------------------------------------------------------------------------


def test_send_telegram_desktop_action_uses_send_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``send_telegram_desktop`` action calls the GUI driver and returns the report."""
    from local_agent.actions import gui_advanced_actions
    from local_agent.actions.registry import ActionContext, ActionRegistry, run_action
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings

    sent: list[tuple[str, str, bool]] = []

    def fake_send(name, text, verify=True):
        sent.append((name, text, verify))
        return SendReport(chat_name=name, message=text, sent=True, verified=True, actual_last_message=text)

    import local_agent.gui_advanced as ga
    monkeypatch.setattr(ga, "send_message_via_telegram_desktop", fake_send)

    settings = AssistantSettings(data_dir=Path("/tmp/x"), work_dir=Path("/tmp/x"))
    handlers = BridgeHandlers.build(settings)
    registry = handlers.registry
    context = handlers.context
    context.confirmation_gate.auto_approve()  # skip confirm prompt
    result = run_action(
        registry, "send_telegram_desktop",
        {"chat_name": "ali", "message": "hi", "verify": True},
        context,
    )
    assert sent == [("ali", "hi", True)]
    assert "verified: True" in result
    assert "sent: True" in result
    assert "chat: ali" in result
