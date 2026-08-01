"""Window control: list, focus, resize, move, and close windows.

Windows: uses Win32 API via ctypes.
Linux: uses wmctrl or xdotool when available, otherwise refuses with
       an actionable message.

Returns human-readable strings for the agent.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.encoding import TEXT_IO, decode_output
from ..utils.platform import (
    iter_windows_windows,
    is_linux,
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
            "is currently open and to find a window by its title. On Linux requires "
            "wmctrl."
        ),
        parameters={
            "filter": {"type": "string", "description": "Substring filter (case-insensitive)."},
        },
    )(list_windows)

    registry.decorator(
        name="move_window",
        description=(
            "Move and optionally resize a window by partial title. Coordinates are "
            "in screen pixels; -1 for any coordinate means 'leave unchanged'. "
            "On Linux requires wmctrl."
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
        description="Minimize a window by partial title. On Linux requires xdotool.",
        parameters={"title": {"type": "string"}},
        required=("title",),
    )(minimize_window_action)

    registry.decorator(
        name="maximize_window",
        description="Maximize a window by partial title. On Linux requires wmctrl.",
        parameters={"title": {"type": "string"}},
        required=("title",),
    )(maximize_window_action)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_windows(*, filter: str = "", context: ActionContext) -> str:
    needle = (filter or "").strip().lower()

    if is_windows():
        titles = [t for t in iter_windows_windows() if not needle or needle in t.lower()]
        if not titles:
            return f"no windows matched filter {needle!r}."
        return "\n".join(f"  • {t}" for t in titles[:80])

    # Linux: use wmctrl
    if not shutil.which("wmctrl"):
        raise DependencyMissing(
            "برای فهرست کردن پنجره‌ها روی لینوکس، ابزار wmctrl لازم است. "
            "نصب کنید: sudo apt install wmctrl",
            install_hint="sudo apt install wmctrl",
        )
    try:
        completed = subprocess.run(
            ["wmctrl", "-l"],
            capture_output=True,
            **TEXT_IO,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"wmctrl failed: {exc}") from exc
    if completed.returncode != 0:
        return "no windows found (wmctrl returned an error)."
    stdout = decode_output(completed.stdout)
    lines = stdout.strip().splitlines()
    titles = []
    for line in lines:
        # wmctrl output: <id> <desktop> <hostname> <title>
        parts = line.split(None, 3)
        if len(parts) >= 4:
            title = parts[3]
            if not needle or needle in title.lower():
                titles.append(title)
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
    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"no window matching {title!r} could be focused")
        try:
            move_resize_window(matched, x, y, width, height)
        except (AssistantError, OSError) as exc:
            raise AssistantError(str(exc)) from exc
        return f"moved {matched!r} to ({x},{y},{width},{height})"

    # Linux: use wmctrl
    if not shutil.which("wmctrl"):
        raise DependencyMissing(
            "برای جابجایی پنجره روی لینوکس، ابزار wmctrl لازم است. "
            "نصب کنید: sudo apt install wmctrl",
            install_hint="sudo apt install wmctrl",
        )
    # wmctrl -r <title> -e <gravity>,<x>,<y>,<w>,<h>
    gravity = 0
    args = ["wmctrl", "-r", title, "-e", f"{gravity},{x},{y},{width},{height}"]
    try:
        subprocess.run(args, capture_output=True, **TEXT_IO, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"wmctrl move failed: {exc}") from exc
    return f"moved {title!r} to ({x},{y},{width},{height})"


@risk(Risk.SAFE)
def minimize_window_action(*, title: str, context: ActionContext) -> str:
    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"no window matching {title!r} could be focused")
        minimize_window(matched)
        return f"minimised {matched!r}"

    # Linux: use xdotool
    if not shutil.which("xdotool"):
        raise DependencyMissing(
            "برای کمینه‌سازی پنجره روی لینوکس، ابزار xdotool لازم است. "
            "نصب کنید: sudo apt install xdotool",
            install_hint="sudo apt install xdotool",
        )
    try:
        # Search for the window and minimize
        completed = subprocess.run(
            ["xdotool", "search", "--name", title],
            capture_output=True,
            **TEXT_IO,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            wid = completed.stdout.strip().splitlines()[0]
            subprocess.run(["xdotool", "windowminimize", wid], capture_output=True, timeout=5, check=False)
            return f"minimised {title!r}"
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise AssistantError(f"no window matching {title!r} could be minimised")


@risk(Risk.SAFE)
def maximize_window_action(*, title: str, context: ActionContext) -> str:
    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"no window matching {title!r} could be focused")
        maximize_window(matched)
        return f"maximised {matched!r}"

    # Linux: use wmctrl
    if not shutil.which("wmctrl"):
        raise DependencyMissing(
            "برای بیشینه‌سازی پنجره روی لینوکس، ابزار wmctrl لازم است. "
            "نصب کنید: sudo apt install wmctrl",
            install_hint="sudo apt install wmctrl",
        )
    try:
        subprocess.run(
            ["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"],
            capture_output=True,
            **TEXT_IO,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"wmctrl maximize failed: {exc}") from exc
    return f"maximised {title!r}"


# ---------------------------------------------------------------------------
# Helpers shared with app_control
# ---------------------------------------------------------------------------


def _focus_by_title(title: str) -> str | None:
    """Find a window matching ``title`` and bring it to the foreground.

    Returns the actual matched title, or None. Uses Win32 SetForegroundWindow
    via ctypes when pywinauto is unavailable. On Linux, uses wmctrl.
    """
    needle = title.strip().lower()
    if not needle:
        return None

    if is_windows():
        for actual in iter_windows_windows():
            if needle in actual.lower():
                _set_foreground(actual)
                return actual
        return None

    # Linux: use wmctrl to activate
    if shutil.which("wmctrl"):
        try:
            subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True,
                **TEXT_IO,
                timeout=5,
                check=False,
            )
            # Check if the window was found
            completed = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                **TEXT_IO,
                timeout=5,
                check=False,
            )
            for line in completed.stdout.strip().splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 4 and needle in parts[3].lower():
                    return parts[3]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _wait_for_window(needle: str, deadline: float) -> str | None:
    needle = needle.lower()
    while time.time() < deadline:
        if is_windows():
            for title in iter_windows_windows():
                if needle in title.lower():
                    _set_foreground(title)
                    return title
        else:
            # Linux: use wmctrl
            if shutil.which("wmctrl"):
                try:
                    completed = subprocess.run(
                        ["wmctrl", "-l"],
                        capture_output=True,
                        **TEXT_IO,
                        timeout=5,
                        check=False,
                    )
                    for line in completed.stdout.strip().splitlines():
                        parts = line.split(None, 3)
                        if len(parts) >= 4 and needle in parts[3].lower():
                            return parts[3]
                except (OSError, subprocess.TimeoutExpired):
                    pass
        time.sleep(0.5)
    return None


def _set_foreground(title: str) -> None:
    """Bring a window to the foreground. Best-effort (Windows-only)."""
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
