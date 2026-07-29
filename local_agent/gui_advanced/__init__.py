"""Advanced GUI automation: UI Automation, accessibility tree, virtual keys.

This module wraps ``uiautomation`` (preferred on Windows) and
``pywinauto`` (fallback) and exposes them through a stable API. The
``automation.gui`` module still owns mouse / keyboard primitives;
this one focuses on *recognising* and *interacting with* arbitrary
controls by name, ID, or class — which is what you need to drive
Telegram Desktop reliably.
"""

from .uia import (
    AdvancedGUI,
    ControlInfo,
    is_uia_available,
    VK,
    win_shortcut,
)
from .telegram_desktop import TelegramDesktop, send_message_via_telegram_desktop

__all__ = [
    "AdvancedGUI",
    "ControlInfo",
    "is_uia_available",
    "VK",
    "win_shortcut",
    "TelegramDesktop",
    "send_message_via_telegram_desktop",
]
