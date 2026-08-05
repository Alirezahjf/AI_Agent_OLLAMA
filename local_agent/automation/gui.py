"""Mouse / keyboard automation via pyautogui.

High-level improvements:
- Coordinate validation against screen size (prevents off-screen clicks)
- Failsafe handling for pyautogui
- Unicode/Persian typing via clipboard fallback when direct typing fails
- Safe filename sanitization for screenshots
- Detailed Persian error messages
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from ..actions.registry import ActionContext, ActionRegistry, Risk, risk
from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("automation.gui")


def is_gui_available() -> bool:
    """Return True if pyautogui is importable AND a display is attached."""
    try:
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = True
    except Exception:  # noqa: BLE001
        return False
    try:
        size = pyautogui.size()
    except Exception:  # noqa: BLE001
        return False
    return size.width > 0 and size.height > 0


def _get_screen_size_safe() -> tuple[int, int]:
    try:
        import pyautogui

        s = pyautogui.size()
        return int(s.width), int(s.height)
    except Exception:  # noqa: BLE001 - headless fallback
        return 1920, 1080


def _validate_coords(x: int, y: int) -> None:
    w, h = _get_screen_size_safe()
    # Allow small overflow (multi-monitor) but warn if way off
    if x < -10000 or y < -10000 or x > w + 10000 or y > h + 10000:
        raise AssistantError(f"مختصات خارج از محدوده است: ({x},{y}) در برابر صفحه {w}x{h}")
    if x < 0 or y < 0 or x > w or y > h:
        logger.warning("coordinate (%s,%s) outside primary screen %sx%s", x, y, w, h)


def _sanitize_filename(name: str) -> str:
    # Keep only safe chars
    safe = "".join(c for c in name if c.isalnum() or c in "._-")
    safe = safe[:128] or "screen.png"
    if not safe.lower().endswith(".png"):
        # Force png extension for screenshots
        if "." in safe:
            safe = safe.rsplit(".", 1)[0] + ".png"
        else:
            safe += ".png"
    return safe


def _unique_screenshot_name(target_dir: Path, requested: str) -> Path:
    """Pick a filename that never overwrites an existing screenshot.

    Default names are ``screen-<YYYYmmdd-HHMMSS>-<6hex>.png`` (time +
    random suffix), so two back-to-back captures always differ and old
    chat messages keep pointing at *their own* image.  A user-supplied
    name is sanitised, and if a file with that name already exists a
    numeric counter is appended (``name-1.png``, ``name-2.png``, ...).
    """
    if requested and requested.strip():
        safe = _sanitize_filename(requested)
        candidate = target_dir / safe
        if not candidate.exists():
            return candidate
        stem, suffix = safe.rsplit(".", 1)
        for index in range(1, 1000):
            candidate = target_dir / f"{stem}-{index}.{suffix}"
            if not candidate.exists():
                return candidate
        return candidate  # pragma: no cover - 999 collisions is impossible in practice
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    token = secrets.token_hex(3)
    return target_dir / f"screen-{stamp}-{token}.png"


def register_gui(registry: ActionRegistry, context: ActionContext) -> None:
    """Register mouse / keyboard / screenshot tools.

    Always registers the ``screen_capture`` tool (works even without
    pyautogui using our PIL fallback).  Mouse / keyboard tools are
    only registered when pyautogui is available.
    """
    if not is_gui_available():
        logger.warning("pyautogui not available; mouse/keyboard tools disabled")

    registry.decorator(
        name="screen_capture",
        description=(
            "Take a PNG screenshot of the full primary screen and return the path. "
            "The image is saved into the assistant's data directory so the LLM can "
            "read it back. Always safe. The filename is sanitized and forced to .png; "
            "the default name is unique per capture (screen-<timestamp>-<random>.png) "
            "and an existing custom name gets a numeric suffix instead of being "
            "overwritten."
        ),
        parameters={
            "filename": {
                "type": "string",
                "description": "اختیاری — نام خروجی؛ در صورت وجود، پسوند عددی می‌گیرد.",
            },
        },
    )(screen_capture)

    if not is_gui_available():
        return

    registry.decorator(
        name="mouse_move",
        description="Move the mouse cursor to absolute screen coordinates (x, y). Validates bounds.",
        parameters={
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "duration": {"type": "number", "description": "Seconds to spend moving."},
        },
        required=("x", "y"),
    )(mouse_move)

    registry.decorator(
        name="mouse_click",
        description=(
            "Click the mouse at (x, y). button: 'left' (default), 'right', 'middle'. "
            "clicks: how many times (default 1). Validates coordinates."
        ),
        parameters={
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "clicks": {"type": "integer"},
        },
        required=("x", "y"),
    )(mouse_click)

    registry.decorator(
        name="mouse_double_click",
        description="Double-click at (x, y). Validates coordinates.",
        parameters={"x": {"type": "integer"}, "y": {"type": "integer"}},
        required=("x", "y"),
    )(mouse_double_click)

    registry.decorator(
        name="type_text",
        description=(
            "Type a string into the currently focused window via the keyboard. "
            "Supports Persian/Unicode via clipboard fallback. "
            "Use interval between keystrokes (helps some apps catch up)."
        ),
        parameters={
            "text": {"type": "string"},
            "interval": {"type": "number", "description": "Seconds between keys (default 0)."},
            "use_clipboard": {"type": "boolean", "description": "Force clipboard paste for Unicode (default auto)."},
        },
        required=("text",),
    )(type_text)

    registry.decorator(
        name="key_press",
        description=(
            "Press a single key or chord. Example: 'enter', 'tab', 'ctrl+c', "
            "'alt+F4', 'super', 'escape'. Use pyautogui key names."
        ),
        parameters={"key": {"type": "string"}},
        required=("key",),
    )(key_press)

    registry.decorator(
        name="hotkey",
        description=(
            "Press a chord of keys. Example: ['ctrl', 'shift', 'esc']. Validates non-empty."
        ),
        parameters={"keys": {"type": "array", "items": {"type": "string"}}},
        required=("keys",),
    )(hotkey)

    registry.decorator(
        name="scroll",
        description="Scroll the mouse wheel by ``amount`` clicks at (x, y). Validates coords.",
        parameters={
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "amount": {"type": "integer"},
        },
        required=("x", "y", "amount"),
    )(scroll)

    registry.decorator(
        name="drag_to",
        description=(
            "Press the mouse at (from_x, from_y), move to (to_x, to_y), and release. "
            "Useful for drag-and-drop in Photoshop, file managers, etc. Validates bounds."
        ),
        parameters={
            "from_x": {"type": "integer"},
            "from_y": {"type": "integer"},
            "to_x": {"type": "integer"},
            "to_y": {"type": "integer"},
            "duration": {"type": "number"},
        },
        required=("from_x", "from_y", "to_x", "to_y"),
    )(drag_to)

    registry.decorator(
        name="get_mouse_position",
        description="Return the current mouse position as x,y.",
        parameters={},
    )(get_mouse_position)

    registry.decorator(
        name="get_screen_size",
        description="Return the primary screen size as width,height.",
        parameters={},
    )(get_screen_size)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _pyautogui():
    import pyautogui  # type: ignore

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


@risk(Risk.SAFE)
def screen_capture(*, filename: str = "", context: ActionContext) -> str:
    from .screenshot import take_screenshot

    image = take_screenshot()
    target = context.runtime.settings.data_dir / "screenshots"
    target.mkdir(parents=True, exist_ok=True)
    # Unique name: never overwrite an existing screenshot, so every chat
    # message keeps pointing at its own image (P4).  An empty filename
    # yields screen-<timestamp>-<random>.png; a custom name that exists
    # gets a numeric suffix.
    final = _unique_screenshot_name(target, filename)
    # Use flexible save that accepts format
    try:
        image.save(final)
    except Exception as exc:
        raise AssistantError(f"ذخیره اسکرین‌شات ممکن نشد: {exc}") from exc
    return f"✅ اسکرین‌شات ذخیره شد: {final} ({image.width}x{image.height}, {image.backend})"


@risk(Risk.SAFE)
def mouse_move(
    *, x: int, y: int, duration: float = 0.0, context: ActionContext
) -> str:
    _validate_coords(int(x), int(y))
    pg = _pyautogui()
    try:
        pg.moveTo(int(x), int(y), duration=max(0.0, float(duration or 0.0)))
    except Exception as exc:
        # pyautogui raises FailSafeException when mouse in corner
        if "fail-safe" in str(exc).lower():
            raise AssistantError("حرکت ماوس به‌دلیل فعال شدن FailSafe متوقف شد (ماوس گوشه صفحه)") from exc
        raise
    return f"ماوس به ({x},{y}) منتقل شد"


@risk(Risk.SAFE)
def mouse_click(
    *,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    context: ActionContext,
) -> str:
    _validate_coords(int(x), int(y))
    pg = _pyautogui()
    btn = button or "left"
    if btn not in {"left", "right", "middle"}:
        raise AssistantError(f"دکمه نامعتبر: {btn}")
    try:
        pg.click(int(x), int(y), button=btn, clicks=max(1, int(clicks or 1)))
    except Exception as exc:
        if "fail-safe" in str(exc).lower():
            raise AssistantError("کلیک به‌دلیل FailSafe متوقف شد") from exc
        raise AssistantError(f"کلیک ناموفق بود: {exc}") from exc
    return f"کلیک {btn} {clicks}× در ({x},{y}) انجام شد"


@risk(Risk.SAFE)
def mouse_double_click(
    *, x: int, y: int, context: ActionContext
) -> str:
    _validate_coords(int(x), int(y))
    pg = _pyautogui()
    try:
        pg.doubleClick(int(x), int(y))
    except Exception as exc:
        raise AssistantError(f"دابل‌کلیک ناموفق بود: {exc}") from exc
    return f"دابل‌کلیک در ({x},{y})"


@risk(Risk.SAFE)
def type_text(
    *, text: str, interval: float = 0.0, context: ActionContext, use_clipboard: bool = False
) -> str:
    if not isinstance(text, str):
        raise AssistantError("text must be a string")
    if len(text) > 5000:
        raise AssistantError("متن خیلی طولانی است (max 5000)")
    pg = _pyautogui()
    safe_interval = max(0.0, float(interval or 0.0))

    # Heuristic: if text contains non-ASCII (Persian, etc.) or user forces clipboard, use clipboard paste
    needs_unicode = any(ord(c) > 127 for c in text)
    if use_clipboard or (needs_unicode and len(text) > 2):
        try:
            # Try clipboard method
            import pyperclip

            pyperclip.copy(text)
            # Paste via ctrl+v / cmd+v
            import platform as plat

            if plat.system() == "Darwin":
                pg.hotkey("command", "v")
            else:
                pg.hotkey("ctrl", "v")
            time.sleep(0.15)
            return f"متن {len(text)} کاراکتری از طریق کلیپ‌بورد تایپ شد (Unicode)"
        except Exception as exc:  # noqa: BLE001 - fall back to direct typing
            logger.debug("clipboard typing failed, falling back to direct: %s", exc)
            # Fall back to direct

    try:
        if safe_interval > 0:
            pg.typewrite(text, interval=safe_interval)
        else:
            pg.write(text, interval=0.0)
    except Exception as exc:
        raise AssistantError(f"تایپ ناموفق بود: {exc}") from exc
    return f"تایپ شد: {len(text)} کاراکتر"


@risk(Risk.SAFE)
def key_press(*, key: str, context: ActionContext) -> str:
    if not isinstance(key, str) or not key.strip():
        raise AssistantError("key must be a non-empty string")
    pg = _pyautogui()
    # Normalize
    k = key.strip().lower()
    # Allow chords like ctrl+c in key_press too
    if "+" in k or "-" in k:
        # treat as hotkey
        parts = [p.strip() for p in k.replace("-", "+").split("+") if p.strip()]
        if len(parts) > 1:
            pg.hotkey(*parts)
            return f"کلید ترکیبی {key} فشرده شد"
    try:
        pg.press(k)
    except Exception as exc:
        raise AssistantError(f"فشردن کلید {key} ناموفق بود: {exc}") from exc
    return f"کلید {key} فشرده شد"


@risk(Risk.SAFE)
def hotkey(*, keys: list[str], context: ActionContext) -> str:
    if not isinstance(keys, list) or not keys:
        raise AssistantError("keys must be a non-empty list")
    if len(keys) > 6:
        raise AssistantError("تعداد کلیدهای ترکیبی بیش از حد است (max 6)")
    cleaned = [str(k).strip().lower() for k in keys if str(k).strip()]
    if not cleaned:
        raise AssistantError("لیست کلیدها خالی است")
    pg = _pyautogui()
    try:
        pg.hotkey(*cleaned)
    except Exception as exc:
        raise AssistantError(f"hotkey {'+'.join(cleaned)} ناموفق بود: {exc}") from exc
    return f"کلیدهای {'+'.join(cleaned)} فشرده شد"


@risk(Risk.SAFE)
def scroll(
    *, x: int, y: int, amount: int, context: ActionContext
) -> str:
    _validate_coords(int(x), int(y))
    if abs(int(amount)) > 100:
        raise AssistantError("مقدار اسکرول بیش از حد است (max 100)")
    pg = _pyautogui()
    try:
        pg.moveTo(int(x), int(y))
        pg.scroll(int(amount))
    except Exception as exc:
        raise AssistantError(f"اسکرول ناموفق بود: {exc}") from exc
    return f"اسکرول {amount} در ({x},{y})"


@risk(Risk.SAFE)
def drag_to(
    *,
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration: float = 0.4,
    context: ActionContext,
) -> str:
    _validate_coords(int(from_x), int(from_y))
    _validate_coords(int(to_x), int(to_y))
    pg = _pyautogui()
    dur = max(0.05, min(float(duration or 0.4), 5.0))
    try:
        pg.moveTo(int(from_x), int(from_y))
        pg.dragTo(
            int(to_x),
            int(to_y),
            duration=dur,
            button="left",
        )
    except Exception as exc:
        raise AssistantError(f"drag ناموفق بود: {exc}") from exc
    return f"درگ از ({from_x},{from_y}) به ({to_x},{to_y}) در {dur} ثانیه"


@risk(Risk.SAFE)
def get_mouse_position(*, context: ActionContext) -> str:
    pg = _pyautogui()
    try:
        pos = pg.position()
    except Exception as exc:
        raise AssistantError(f"خواندن موقعیت ماوس ناموفق بود: {exc}") from exc
    return f"x={pos.x}, y={pos.y}"


@risk(Risk.SAFE)
def get_screen_size(*, context: ActionContext) -> str:
    pg = _pyautogui()
    try:
        size = pg.size()
    except Exception as exc:
        raise AssistantError(f"خواندن اندازه صفحه ناموفق بود: {exc}") from exc
    return f"width={size.width}, height={size.height} (primary)"
