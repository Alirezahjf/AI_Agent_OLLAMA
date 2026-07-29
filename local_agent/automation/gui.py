"""Mouse / keyboard automation via pyautogui.

The helpers in this module are exposed to the LLM through a separate
``register_gui`` entry point because they are only useful on a real
desktop session.  pyautogui itself is optional; the module degrades
gracefully when it is not installed.
"""

from __future__ import annotations

import time
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..actions.registry import ActionContext, ActionRegistry, risk, Risk


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
            "read it back. Always safe."
        ),
        parameters={
            "filename": {"type": "string", "description": "Output filename (default screen.png)."},
        },
    )(screen_capture)

    if not is_gui_available():
        return

    registry.decorator(
        name="mouse_move",
        description="Move the mouse cursor to absolute screen coordinates (x, y).",
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
            "clicks: how many times (default 1). Use to interact with native UIs."
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
        description="Double-click at (x, y).",
        parameters={"x": {"type": "integer"}, "y": {"type": "integer"}},
        required=("x", "y"),
    )(mouse_double_click)

    registry.decorator(
        name="type_text",
        description=(
            "Type a string into the currently focused window via the keyboard. "
            "Use ``interval`` between keystrokes (helps some apps catch up)."
        ),
        parameters={
            "text": {"type": "string"},
            "interval": {"type": "number", "description": "Seconds between keys (default 0)."},
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
            "Press a chord of keys. Example: ['ctrl', 'shift', 'esc']."
        ),
        parameters={"keys": {"type": "array", "items": {"type": "string"}}},
        required=("keys",),
    )(hotkey)

    registry.decorator(
        name="scroll",
        description="Scroll the mouse wheel by ``amount`` clicks at (x, y).",
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
            "Useful for drag-and-drop in Photoshop, file managers, etc."
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
def screen_capture(*, filename: str = "screen.png", context: ActionContext) -> str:
    from .screenshot import take_screenshot

    image = take_screenshot()
    target = context.runtime.settings.data_dir / "screenshots"
    target.mkdir(parents=True, exist_ok=True)
    final = target / filename
    image.save(final, "PNG")
    return f"saved screenshot to {final}"


@risk(Risk.SAFE)
def mouse_move(
    *, x: int, y: int, duration: float = 0.0, context: ActionContext
) -> str:
    pg = _pyautogui()
    pg.moveTo(int(x), int(y), duration=max(0.0, float(duration or 0.0)))
    return f"mouse moved to ({x},{y})"


@risk(Risk.SAFE)
def mouse_click(
    *,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    context: ActionContext,
) -> str:
    pg = _pyautogui()
    pg.click(int(x), int(y), button=button or "left", clicks=max(1, int(clicks or 1)))
    return f"clicked {button} {clicks}x at ({x},{y})"


@risk(Risk.SAFE)
def mouse_double_click(
    *, x: int, y: int, context: ActionContext
) -> str:
    pg = _pyautogui()
    pg.doubleClick(int(x), int(y))
    return f"double-clicked at ({x},{y})"


@risk(Risk.SAFE)
def type_text(
    *, text: str, interval: float = 0.0, context: ActionContext
) -> str:
    if not isinstance(text, str):
        raise AssistantError("text must be a string")
    pg = _pyautogui()
    safe_interval = max(0.0, float(interval or 0.0))
    if safe_interval > 0:
        pg.typewrite(text, interval=safe_interval)  # ASCII only when interval>0
    else:
        # write supports unicode via pyperclip-style paste; pyautogui.write is
        # already unicode-aware on modern versions.
        pg.write(text, interval=0.0)
    return f"typed {len(text)} characters"


@risk(Risk.SAFE)
def key_press(*, key: str, context: ActionContext) -> str:
    pg = _pyautogui()
    pg.press(key)
    return f"pressed {key}"


@risk(Risk.SAFE)
def hotkey(*, keys: list[str], context: ActionContext) -> str:
    if not isinstance(keys, list) or not keys:
        raise AssistantError("keys must be a non-empty list")
    pg = _pyautogui()
    pg.hotkey(*keys)
    return f"pressed {'+'.join(keys)}"


@risk(Risk.SAFE)
def scroll(
    *, x: int, y: int, amount: int, context: ActionContext
) -> str:
    pg = _pyautogui()
    pg.moveTo(int(x), int(y))
    pg.scroll(int(amount))
    return f"scrolled {amount} at ({x},{y})"


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
    pg = _pyautogui()
    pg.moveTo(int(from_x), int(from_y))
    pg.dragTo(
        int(to_x), int(to_y),
        duration=max(0.05, float(duration or 0.4)),
        button="left",
    )
    return f"dragged ({from_x},{from_y}) -> ({to_x},{to_y})"


@risk(Risk.SAFE)
def get_mouse_position(*, context: ActionContext) -> str:
    pg = _pyautogui()
    pos = pg.position()
    return f"x={pos.x}, y={pos.y}"


@risk(Risk.SAFE)
def get_screen_size(*, context: ActionContext) -> str:
    pg = _pyautogui()
    size = pg.size()
    return f"width={size.width}, height={size.height}"
