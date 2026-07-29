"""Window control: list, focus, resize, move, and close windows.

Uses pywinauto when available (Windows) with a graceful fallback to
Win32 calls via ctypes. Returns human-readable strings for the agent.
"""

from __future__ import annotations

import time
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.platform import (
    iter_windows_windows,
    is_windows,
    move_resize_window,
    minimize_window,
    maximize_window,
)
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.window")


def register_window_control(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="list_windows",
        description=(
            "List the titles of all visible top-level windows. Use to discover what "
            "is currently open and to find a window by its title."
        ),
        parameters={
            "filter": {"type": "string", "description": "Substring filter (case-insensitive)."},
        },
    )(list_windows)

    registry.decorator(
        name="move_window",
        description=(
            "Move and optionally resize a window by partial title. Coordinates are "
            "in screen pixels; -1 for any coordinate means 'leave unchanged'."
        ),
        parameters={
            "title": {"type": "string", "description": "Partial window title."},
            "x": {"type": "integer", "description": "New X position (-1 to keep)."},
            "y": {"type": "integer", "description": "New Y position (-1 to keep)."},
            "width": {"type": "integer", "description": "New width (-1 to keep)."},
            "height": {"type": "integer", "description": "New height (-1 to keep)."},
        },
        required=("title",),
    )(move_window)

    registry.decorator(
        name="minimize_window",
        description="Minimize a window by partial title.",
        parameters={"title": {"type": "string"}},
        required=("title",),
    )(minimize_window_action)

    registry.decorator(
        name="maximize_window",
        description="Maximize a window by partial title.",
        parameters={"title": {"type": "string"}},
        required=("title",),
    )(maximize_window_action)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_windows(*, filter: str = "", context: ActionContext) -> str:
    needle = (filter or "").strip().lower()
    titles = [t for t in iter_windows_windows() if not needle or needle in t.lower()]
    if not titles:
        return f"no windows matched filter {needle!r}."
    return "\n".join(f"  • {t}" for t in titles[:80])


@risk(Risk.SAFE)
def move_window(
    *,
    title: str,
    x: int = -1,
    y: int = -1,
    width: int = -1,
    height: int = -1,
    context: ActionContext,
) -> str:
    matched = _focus_by_title(title)
    if not matched:
        raise AssistantError(f"no window matching {title!r} could be focused")
    try:
        move_resize_window(matched, x, y, width, height)
    except (AssistantError, OSError) as exc:
        raise AssistantError(str(exc)) from exc
    return f"moved {matched!r} to ({x},{y},{width},{height})"


@risk(Risk.SAFE)
def minimize_window_action(*, title: str, context: ActionContext) -> str:
    matched = _focus_by_title(title)
    if not matched:
        raise AssistantError(f"no window matching {title!r} could be focused")
    minimize_window(matched)
    return f"minimised {matched!r}"


@risk(Risk.SAFE)
def maximize_window_action(*, title: str, context: ActionContext) -> str:
    matched = _focus_by_title(title)
    if not matched:
        raise AssistantError(f"no window matching {title!r} could be focused")
    maximize_window(matched)
    return f"maximised {matched!r}"


# ---------------------------------------------------------------------------
# Helpers shared with app_control
# ---------------------------------------------------------------------------


def _focus_by_title(title: str) -> str | None:
    """Find a window matching ``title`` and bring it to the foreground.

    Returns the actual matched title, or None. Uses Win32 SetForegroundWindow
    via ctypes when pywinauto is unavailable.
    """
    needle = title.strip().lower()
    if not needle:
        return None
    for actual in iter_windows_windows():
        if needle in actual.lower():
            _set_foreground(actual)
            return actual
    return None


def _wait_for_window(needle: str, deadline: float) -> str | None:
    needle = needle.lower()
    while time.time() < deadline:
        for title in iter_windows_windows():
            if needle in title.lower():
                _set_foreground(title)
                return title
        time.sleep(0.5)
    return None


def _set_foreground(title: str) -> None:
    """Bring a window to the foreground. Best-effort."""
    try:
        import ctypes
        from ctypes import wintypes

        EnumWindows = ctypes.windll.user32.EnumWindows
        GetWindowTextW = ctypes.windll.user32.GetWindowTextW
        SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow
        IsWindowVisible = ctypes.windll.user32.IsWindowVisible

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        )

        found: list[int] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            if not IsWindowVisible(hwnd):
                return True
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buff = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buff, length + 1)
            text = buff.value or ""
            if title.lower() in text.lower():
                found.append(hwnd)
            return True

        EnumWindows(EnumWindowsProc(callback), 0)
        for hwnd in found:
            SetForegroundWindow(hwnd)
            return
    except (OSError, AttributeError) as exc:
        logger.debug("set_foreground failed for %s: %s", title, exc)
