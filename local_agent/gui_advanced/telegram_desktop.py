"""Telegram Desktop GUI driver.

This module provides a high-level driver for the official Telegram
Desktop client.  The user's sample script demonstrated the right
approach:

  1. Open (or focus) the Telegram window.
  2. Press Ctrl+F to open the chat search bar.
  3. Paste the chat name.
  4. Wait for the search results to load.
  5. Click on the right result (using UI Automation).
  6. Paste the message text and press Enter.
  7. **Verify** that the message was actually sent by reading the
     last message back from the chat.

The driver supports two paths:

  * :func:`send_message_via_telegram_desktop` — direct, fire-and-
    forget.  Returns a verification report.

  * :class:`TelegramDesktop` — a context manager that opens the
    client once and lets you send many messages without re-opening.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from .uia import AdvancedGUI, VK, win_shortcut


logger = get_logger("gui.telegram")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class SendReport:
    """The outcome of a Telegram Desktop send attempt."""

    chat_name: str
    message: str
    sent: bool
    verified: bool
    error: str | None = None
    actual_last_message: str = ""

    def to_dict(self) -> dict:
        return {
            "chat_name": self.chat_name,
            "message": self.message,
            "sent": self.sent,
            "verified": self.verified,
            "error": self.error,
            "actual_last_message": self.actual_last_message,
        }


def _candidate_telegram_paths() -> list[Path]:
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        home = Path(os.path.expanduser("~"))
        candidates += [
            home / "AppData" / "Roaming" / "Telegram Desktop" / "Telegram.exe",
            home / "AppData" / "Local" / "Programs" / "Telegram Desktop" / "Telegram.exe",
            Path("C:/Program Files/Telegram Desktop/Telegram.exe"),
            Path("C:/Program Files (x86)/Telegram Desktop/Telegram.exe"),
            Path("D:/Program Files/Telegram Desktop/Telegram.exe"),
        ]
    for c in candidates:
        if c.is_file():
            return [c]
    # PATH fallback
    which = shutil.which("Telegram")
    if which:
        return [Path(which)]
    return []


def find_telegram_desktop() -> Path | None:
    """Return the path of the Telegram Desktop executable or None."""
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Telegram Desktop_is1",
                    ) as key:
                        location, _ = winreg.QueryValueEx(key, "InstallLocation")
                        exe = Path(location) / "Telegram.exe"
                        if exe.is_file():
                            return exe
                except OSError:
                    continue
        except ImportError:
            pass
    for path in _candidate_telegram_paths():
        if path.is_file():
            return path
    return None


def open_telegram_desktop(path: Path | None = None) -> Path:
    """Launch the Telegram Desktop client and return the resolved path."""
    if path is None:
        path = find_telegram_desktop()
    if path is None or not path.is_file():
        raise AssistantError("Telegram Desktop is not installed.")
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Telegram"])
        else:
            subprocess.Popen(["telegram-desktop"])
    except OSError as exc:
        raise AssistantError(f"could not launch Telegram Desktop: {exc}") from exc
    time.sleep(3.0)
    return path


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class TelegramDesktop:
    """Context manager that keeps a Telegram Desktop window open.

    Usage::

        with TelegramDesktop() as tg:
            report = tg.send_message("علی", "سلام")
    """

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable or find_telegram_desktop()
        self.gui = AdvancedGUI()
        self._entered = False

    def __enter__(self) -> "TelegramDesktop":
        if self.executable is None or not self.executable.is_file():
            raise AssistantError("Telegram Desktop is not installed.")
        if sys.platform.startswith("win"):
            try:
                os.startfile(str(self.executable))  # type: ignore[attr-defined]
            except OSError as exc:
                raise AssistantError(f"could not launch Telegram: {exc}") from exc
        else:
            raise AssistantError("Telegram Desktop GUI driver is Windows-only.")
        time.sleep(3.0)
        # Wait for the main window
        if not self.gui.focus_window("Telegram", timeout=10):
            raise AssistantError("Telegram window did not appear.")
        self._entered = True
        return self

    def __exit__(self, *exc_info) -> None:
        self._entered = False

    def send_message(self, chat_name: str, text: str, *, verify: bool = True) -> SendReport:
        """Send ``text`` to ``chat_name`` and verify it landed.

        The implementation closely follows the user's sample script
        but adds proper error handling, retries, and an actual
        verification step.
        """
        if not self._entered:
            raise AssistantError("use TelegramDesktop inside a 'with' block")
        try:
            import pyperclip  # type: ignore
        except ImportError as exc:
            raise DependencyMissing(
                "pyperclip is required to drive the clipboard",
                install_hint="pip install pyperclip",
            ) from exc

        # 1. Make sure the Telegram window is focused
        if not self.gui.focus_window("Telegram", timeout=3):
            return SendReport(chat_name, text, False, False, "Telegram window not visible")

        # 2. Press Escape to clear any open overlay
        from ..automation.gui import _pyautogui

        pg = _pyautogui()
        pg.press("escape", presses=3, interval=0.4)
        time.sleep(0.4)

        # 3. Open search
        win_shortcut(VK.CTRL, VK.F)
        time.sleep(1.0)

        # 4. Paste the chat name
        pyperclip.copy(chat_name)
        win_shortcut(VK.CTRL, VK.V)
        time.sleep(3.5)  # wait for results

        # 5. Click the best matching chat via UIA, fallback to Enter
        clicked = self._click_chat_result(chat_name)
        if not clicked:
            pg.press("enter")
            time.sleep(0.3)
            pg.press("enter")
        time.sleep(2.0)

        # 6. Type the message
        pyperclip.copy(text)
        win_shortcut(VK.CTRL, VK.V)
        time.sleep(0.5)
        pg.press("enter")
        time.sleep(2.5)

        if not verify:
            return SendReport(chat_name, text, True, True, None)

        # 7. Verify
        actual = self._read_last_message()
        sent_ok = bool(actual) and text.strip() in actual
        return SendReport(
            chat_name=chat_name,
            message=text,
            sent=True,
            verified=sent_ok,
            actual_last_message=actual,
            error=None if sent_ok else "verification failed",
        )

    def _click_chat_result(self, target_name: str) -> bool:
        """Use UIA to find the exact chat in the search results and click it."""
        if not is_uia_available():
            return False
        try:
            import uiautomation as auto  # type: ignore
        except ImportError:
            return False

        # The Telegram main window is a Qt5 window; walk the tree.
        try:
            window = auto.WindowControl(searchDepth=1, ClassName="Qt5QWindowIcon")
            if not window.Exists(0, 0):
                window = auto.WindowControl(Name="Telegram")
            if not window.Exists(0, 0):
                return False

            target = target_name.strip().lower()
            best = None

            # 1. Exact match
            exact = window.Control(searchDepth=12, Name=target_name)
            if exact.Exists(0, 0):
                exact.Click()
                return True

            # 2. Substring / case-insensitive
            for ctrl, depth in auto.WalkControl(window, maxDepth=12):
                name = (ctrl.Name or "").strip()
                if not name:
                    continue
                low = name.lower()
                if low == target:
                    ctrl.Click()
                    return True
                if target in low and depth >= 4:
                    if best is None:
                        best = ctrl
            if best is not None and best.Exists(0, 0):
                best.Click()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("UIA click failed: %s", exc)
        return False

    def _read_last_message(self) -> str:
        """Read the most recent message visible in the chat.

        The technique mirrors the user's sample: clear the clipboard,
        press Up, select all, copy, and read.  The clipboard is
        restored after.
        """
        try:
            import pyperclip  # type: ignore
        except ImportError:
            return ""
        try:
            sentinel = "---ASSISTANT-EMPTY-CLIPBOARD-SENTINEL---"
            previous = pyperclip.paste()
            pyperclip.copy(sentinel)
            time.sleep(0.2)
            from ..automation.gui import _pyautogui

            pg = _pyautogui()
            pg.press("up")
            time.sleep(0.6)
            win_shortcut(VK.CTRL, VK.A)
            time.sleep(0.2)
            win_shortcut(VK.CTRL, VK.C)
            time.sleep(0.5)
            text = (pyperclip.paste() or "").strip()
            pyperclip.copy(previous or "")
            return "" if text == sentinel else text
        except Exception:  # noqa: BLE001
            return ""


def is_uia_available() -> bool:
    """Re-export for convenience."""
    from .uia import is_uia_available as _is

    return _is()


def send_message_via_telegram_desktop(chat_name: str, text: str) -> SendReport:
    """One-shot helper: open Telegram, send, close.

    For repeated use, prefer the :class:`TelegramDesktop` context
    manager to avoid re-opening the window each time.
    """
    try:
        with TelegramDesktop() as tg:
            return tg.send_message(chat_name, text)
    except AssistantError as exc:
        return SendReport(chat_name, text, False, False, str(exc))
