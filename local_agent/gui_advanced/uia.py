"""Windows UI Automation (UIA) wrapper.

The wrapper is intentionally small.  Its main jobs are:

  * Provide a clean, type-safe way to enumerate controls.
  * Provide a ``win_shortcut`` helper that uses virtual key codes, so
    keyboard shortcuts work regardless of the active input language
    (the user's sample script demonstrated why this matters: an
    English shortcut like Ctrl+F is sent with the wrong glyph when
    the keyboard layout is Persian).

uiautomation is an optional dependency.  The class ``AdvancedGUI``
degrades gracefully when it is not installed: methods that need UIA
raise :class:`DependencyMissing` with an install hint.
"""

from __future__ import annotations

import ctypes
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger


logger = get_logger("gui.uia")


# ---------------------------------------------------------------------------
# Virtual key codes (subset, sufficient for common shortcuts)
# ---------------------------------------------------------------------------


class VK:
    """Virtual key codes used by ``win_shortcut``."""

    CTRL = 0x11
    ALT = 0x12
    SHIFT = 0x10
    WIN = 0x5B
    A = 0x41
    B = 0x42
    C = 0x43
    D = 0x44
    E = 0x45
    F = 0x46
    G = 0x47
    H = 0x48
    I = 0x49
    J = 0x4A
    K = 0x4B
    L = 0x4C
    M = 0x4D
    N = 0x4E
    O = 0x4F
    P = 0x50
    Q = 0x51
    R = 0x52
    S = 0x53
    T = 0x54
    U = 0x55
    V = 0x56
    W = 0x57
    X = 0x58
    Y = 0x59
    Z = 0x5A
    F1 = 0x70
    F12 = 0x7B
    ESC = 0x1B
    TAB = 0x09
    ENTER = 0x0D
    BACKSPACE = 0x08
    SPACE = 0x20
    DELETE = 0x2E
    HOME = 0x24
    END = 0x23
    LEFT = 0x25
    UP = 0x26
    RIGHT = 0x27
    DOWN = 0x28


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def win_shortcut(*vk_codes: int) -> None:
    """Send a keyboard shortcut using virtual key codes.

    Unlike ``pyautogui.hotkey('ctrl', 'f')``, this method does not
    depend on the active keyboard layout.  ``win_shortcut(VK.CTRL, VK.F)``
    always produces Ctrl+F even when the layout is Persian.

    Falls back to ``pyautogui.hotkey`` on non-Windows hosts.
    """
    if not _is_windows():
        from ..automation.gui import _pyautogui

        pg = _pyautogui()
        keys = []
        for code in vk_codes:
            char = chr(code) if 0x30 <= code <= 0x5A or 0x41 <= code <= 0x5A else None
            if char and char.isalnum():
                keys.append(char.lower())
            else:
                # Map a few common ones
                mapping = {
                    VK.ENTER: "enter",
                    VK.ESC: "escape",
                    VK.TAB: "tab",
                    VK.BACKSPACE: "backspace",
                    VK.SPACE: "space",
                    VK.DELETE: "delete",
                    VK.LEFT: "left",
                    VK.RIGHT: "right",
                    VK.UP: "up",
                    VK.DOWN: "down",
                }
                keys.append(mapping.get(code, "space"))
        pg.hotkey(*keys)
        return

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    for code in vk_codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
    for code in reversed(vk_codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Optional uiautomation import
# ---------------------------------------------------------------------------


def is_uia_available() -> bool:
    """Return True if the ``uiautomation`` package is importable."""
    try:
        import uiautomation  # type: ignore  # noqa: F401

        return True
    except ImportError:
        return False


def _require_uia():
    if not is_uia_available():
        raise DependencyMissing(
            "uiautomation is required for this action",
            install_hint="pip install uiautomation",
        )
    import uiautomation  # type: ignore

    return uiautomation


# ---------------------------------------------------------------------------
# Control info
# ---------------------------------------------------------------------------


@dataclass
class ControlInfo:
    """A simplified description of a UIA control."""

    name: str = ""
    class_name: str = ""
    automation_id: str = ""
    control_type: str = ""
    handle: int = 0
    bounding_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    text: str = ""
    children: list["ControlInfo"] = field(default_factory=list)

    @property
    def centre(self) -> tuple[int, int]:
        x, y, w, h = self.bounding_rect
        return x + w // 2, y + h // 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "class_name": self.class_name,
            "automation_id": self.automation_id,
            "control_type": self.control_type,
            "handle": self.handle,
            "bounding_rect": self.bounding_rect,
            "text": self.text,
            "children": [c.to_dict() for c in self.children],
        }


def _describe(ctrl) -> ControlInfo:
    try:
        rect = ctrl.BoundingRectangle
        x, y, w, h = int(rect.left), int(rect.top), int(rect.width), int(rect.height)
    except Exception:  # noqa: BLE001
        x = y = w = h = 0
    return ControlInfo(
        name=str(ctrl.Name or ""),
        class_name=str(ctrl.ClassName or ""),
        automation_id=str(ctrl.AutomationId or ""),
        control_type=str(ctrl.ControlTypeName or ""),
        handle=int(ctrl.NativeWindowHandle) if ctrl.NativeWindowHandle else 0,
        bounding_rect=(x, y, w, h),
        text="",
    )


# ---------------------------------------------------------------------------
# High-level wrapper
# ---------------------------------------------------------------------------


class AdvancedGUI:
    """High-level wrapper over UI Automation and pywinauto."""

    def __init__(self) -> None:
        self.auto = _require_uia() if is_uia_available() else None

    # -------------------------------------------------------- window ops

    def list_windows(self, *, max_depth: int = 8) -> list[ControlInfo]:
        if self.auto is None:
            return self._fallback_windows()
        results: list[ControlInfo] = []
        try:
            desktop = self.auto.GetRootControl() if hasattr(self.auto, "GetRootControl") else self.auto.WindowControl(searchDepth=1)
        except Exception:  # noqa: BLE001
            return self._fallback_windows()
        for ctrl, _ in self.auto.WalkControl(desktop, maxDepth=max_depth):
            info = _describe(ctrl)
            if info.name:
                results.append(info)
        return results

    def _fallback_windows(self) -> list[ControlInfo]:
        from ..utils.platform import iter_windows_windows

        return [
            ControlInfo(name=title, control_type="Window")
            for title in iter_windows_windows()
        ]

    def find_window(self, title_substring: str, *, exact: bool = False, timeout: float = 5.0) -> ControlInfo | None:
        if self.auto is None:
            for title in self._fallback_windows():
                if (exact and title.name == title_substring) or (
                    not exact and title_substring.lower() in title.name.lower()
                ):
                    return title
            return None
        deadline = time.time() + max(0.0, timeout)
        needle = title_substring.lower()
        while time.time() < deadline:
            for ctrl, _ in self.auto.WalkControl(self.auto.GetRootControl(), maxDepth=4):
                name = str(ctrl.Name or "")
                if (exact and name == title_substring) or (
                    not exact and needle in name.lower()
                ):
                    return _describe(ctrl)
            time.sleep(0.3)
        return None

    def focus_window(self, title_substring: str) -> bool:
        info = self.find_window(title_substring)
        if info is None or info.handle == 0:
            return False
        return _bring_to_front(info.handle)

    def find_controls(
        self,
        *,
        name: str | None = None,
        class_name: str | None = None,
        automation_id: str | None = None,
        control_type: str | None = None,
        parent: Any = None,
        max_depth: int = 12,
    ) -> list[ControlInfo]:
        if self.auto is None:
            raise DependencyMissing(
                "uiautomation is required to enumerate controls",
                install_hint="pip install uiautomation",
            )
        root = parent or self.auto.GetRootControl()
        matches: list[ControlInfo] = []
        for ctrl, _ in self.auto.WalkControl(root, maxDepth=max_depth):
            if name and name not in str(ctrl.Name or ""):
                continue
            if class_name and class_name != str(ctrl.ClassName or ""):
                continue
            if automation_id and automation_id != str(ctrl.AutomationId or ""):
                continue
            if control_type and control_type != str(ctrl.ControlTypeName or ""):
                continue
            matches.append(_describe(ctrl))
        return matches

    def click_control(self, control: ControlInfo) -> None:
        cx, cy = control.centre
        if not cx and not cy:
            raise AssistantError(f"control {control.name!r} has no bounding rectangle")
        from ..automation.gui import _pyautogui

        pg = _pyautogui()
        pg.click(cx, cy)

    def type_into(self, control: ControlInfo, text: str, *, interval: float = 0.0) -> None:
        self.click_control(control)
        from ..automation.gui import _pyautogui

        pg = _pyautogui()
        if interval > 0:
            pg.typewrite(text, interval=interval)
        else:
            pg.write(text, interval=0.0)

    # -------------------------------------------------------- utilities

    @contextmanager
    def foreground_window(self, title_substring: str, *, timeout: float = 5.0):
        info = self.find_window(title_substring, timeout=timeout)
        if info is None:
            raise AssistantError(f"window not found: {title_substring!r}")
        previous = _current_foreground()
        _bring_to_front(info.handle)
        try:
            yield info
        finally:
            if previous:
                _bring_to_front(previous)


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------


def _bring_to_front(hwnd: int) -> bool:
    if not _is_windows() or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Press and release Alt to escape the foreground-lock on Windows
        user32.keybd_event(VK.ALT, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK.ALT, 0, 0x0002, 0)
        return True
    except (OSError, AttributeError) as exc:
        logger.debug("bring_to_front(%s) failed: %s", hwnd, exc)
        return False


def _current_foreground() -> int:
    if not _is_windows():
        return 0
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except (OSError, AttributeError):
        return 0
