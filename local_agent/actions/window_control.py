"""Window control: list, focus, resize, move, and close windows.

High-level improvements:
- Safe title handling (no shell injection, proper subprocess list, not shell)
- Case-insensitive Persian-friendly matching
- Better error messages in Persian where appropriate
- Linux: wmctrl/xdotool checks with actionable hints
- Windows: Win32 API via ctypes with fallbacks
- Validation for coordinates (avoid off-screen)
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
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
            "wmctrl. High-level: handles large lists, filters case-insensitive."
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
            "On Linux requires wmctrl. Validates title non-empty."
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
        description="Minimize a window by partial title. On Linux requires xdotool. Validates input.",
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
        try:
            titles = [t for t in iter_windows_windows() if not needle or needle in t.lower()]
        except Exception as exc:
            raise AssistantError(f"خواندن پنجره‌ها ناموفق بود: {exc}") from exc
        if not titles:
            return f"پنجره‌ای با فیلتر {filter!r} پیدا نشد." if filter else "هیچ پنجره‌ای پیدا نشد."
        # Limit and format
        return "\n".join(f"  • {t}" for t in titles[:80]) + (f"\n  ... ({len(titles)-80} بیشتر)" if len(titles) > 80 else "")

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
            text=True,
                encoding="utf-8",
                errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"اجرای wmctrl ناموفق بود: {exc}") from exc
    if completed.returncode != 0:
        return "پنجره‌ای پیدا نشد (wmctrl خطا برگرداند)."
    lines = completed.stdout.strip().splitlines()
    titles = []
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) >= 4:
            title = parts[3]
            if not needle or needle in title.lower():
                titles.append(title)
    if not titles:
        return f"پنجره‌ای با فیلتر {filter!r} پیدا نشد."
    return "\n".join(f"  • {t}" for t in titles[:80]) + (f"\n  ... ({len(titles)-80} بیشتر)" if len(titles) > 80 else "")


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
    if not isinstance(title, str) or not title.strip():
        raise AssistantError("عنوان پنجره نباید خالی باشد")
    if len(title) > 500:
        raise AssistantError("عنوان پنجره خیلی طولانی است")
    # Validate coordinates reasonable
    for v, name in [(x, "x"), (y, "y"), (width, "width"), (height, "height")]:
        if not isinstance(v, int) and not isinstance(v, float):
            raise AssistantError(f"{name} باید عدد باشد")
        if v < -1 or v > 10000:
            raise AssistantError(f"{name} خارج از محدوده مجاز است (-1 تا 10000)")

    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"پنجره‌ای با عنوان {title!r} پیدا نشد")
        try:
            move_resize_window(matched, int(x), int(y), int(width), int(height))
        except (AssistantError, OSError) as exc:
            raise AssistantError(f"جابجایی پنجره ناموفق بود: {exc}") from exc
        return f"✅ پنجره {matched!r} به ({x},{y},{width},{height}) منتقل شد"

    # Linux: use wmctrl - safe list args (no shell)
    if not shutil.which("wmctrl"):
        raise DependencyMissing(
            "برای جابجایی پنجره روی لینوکس، ابزار wmctrl لازم است. "
            "نصب کنید: sudo apt install wmctrl",
            install_hint="sudo apt install wmctrl",
        )
    gravity = 0
    args = ["wmctrl", "-r", title, "-e", f"{gravity},{int(x)},{int(y)},{int(width)},{int(height)}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                encoding="utf-8",
                errors="replace", timeout=5, check=False)
        if result.returncode != 0:
            logger.debug("wmctrl move returned %s: %s", result.returncode, result.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"جابجایی با wmctrl ناموفق بود: {exc}") from exc
    return f"درخواست جابجایی پنجره {title!r} به ({x},{y},{width},{height}) ارسال شد"


@risk(Risk.SAFE)
def minimize_window_action(*, title: str, context: ActionContext) -> str:
    if not isinstance(title, str) or not title.strip():
        raise AssistantError("عنوان پنجره نباید خالی باشد")
    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"پنجره‌ای با عنوان {title!r} پیدا نشد")
        try:
            minimize_window(matched)
        except Exception as exc:
            raise AssistantError(f"کمینه‌سازی ناموفق بود: {exc}") from exc
        return f"پنجره {matched!r} کمینه شد"

    # Linux: use xdotool
    if not shutil.which("xdotool"):
        raise DependencyMissing(
            "برای کمینه‌سازی پنجره روی لینوکس، ابزار xdotool لازم است. "
            "نصب کنید: sudo apt install xdotool",
            install_hint="sudo apt install xdotool",
        )
    try:
        completed = subprocess.run(
            ["xdotool", "search", "--name", title],
            capture_output=True,
            text=True,
                encoding="utf-8",
                errors="replace",
            timeout=5,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            wid = completed.stdout.strip().splitlines()[0]
            subprocess.run(["xdotool", "windowminimize", wid], capture_output=True, timeout=5, check=False)
            return f"پنجره {title!r} کمینه شد"
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"کمینه‌سازی با xdotool ناموفق بود: {exc}") from exc
    raise AssistantError(f"پنجره‌ای با عنوان {title!r} برای کمینه‌سازی پیدا نشد")


@risk(Risk.SAFE)
def maximize_window_action(*, title: str, context: ActionContext) -> str:
    if not isinstance(title, str) or not title.strip():
        raise AssistantError("عنوان پنجره نباید خالی باشد")
    if is_windows():
        matched = _focus_by_title(title)
        if not matched:
            raise AssistantError(f"پنجره‌ای با عنوان {title!r} پیدا نشد")
        try:
            maximize_window(matched)
        except Exception as exc:
            raise AssistantError(f"بیشینه‌سازی ناموفق بود: {exc}") from exc
        return f"پنجره {matched!r} بیشینه شد"

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
            text=True,
                encoding="utf-8",
                errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"بیشینه‌سازی با wmctrl ناموفق بود: {exc}") from exc
    return f"درخواست بیشینه‌سازی پنجره {title!r} ارسال شد"


# ---------------------------------------------------------------------------
# Helpers shared with app_control
# ---------------------------------------------------------------------------


def _focus_by_title(title: str) -> str | None:
    """Find a window matching ``title`` and bring it to the foreground."""
    needle = title.strip().lower()
    if not needle:
        return None

    if is_windows():
        try:
            for actual in iter_windows_windows():
                if needle in actual.lower():
                    _set_foreground(actual)
                    return actual
        except Exception:
            return None
        return None

    # Linux: use wmctrl to activate - safe, no shell
    if shutil.which("wmctrl"):
        try:
            subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            completed = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
            try:
                for title in iter_windows_windows():
                    if needle in title.lower():
                        _set_foreground(title)
                        return title
            except Exception:
                pass
        else:
            if shutil.which("wmctrl"):
                try:
                    completed = subprocess.run(
                        ["wmctrl", "-l"],
                        capture_output=True,
                        text=True,
                encoding="utf-8",
                errors="replace",
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
