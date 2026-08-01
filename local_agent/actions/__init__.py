"""Action layer: high-level desktop operations exposed as tools to the LLM.

Every action is a function that takes a small dict of arguments and
returns a string result. The LLM never sees a Python object; the agent
loop just passes the JSON it received as ``tool_call.arguments``.

Actions are intentionally *coarse-grained* and *side-effect-typed*:

  * SAFE — open an app, focus a window, take a screenshot, search web.
    These never need confirmation.
  * DESTRUCTIVE — close apps, send messages, move files, kill processes.
    These require user approval when ``safety.require_confirm_for_destructive``
    is True.
"""

from .registry import Action, ActionContext, ActionRegistry, risk
from .app_control import register_app_control
from .window_control import register_window_control
from .process_control import register_process_control
from .clipboard import register_clipboard
from .file_ops import register_file_ops
from .web import register_web
from .system import register_system
from .gui_advanced_actions import register_gui_advanced
from .runner import run_action, list_actions, describe_action

__all__ = [
    "Action",
    "ActionContext",
    "ActionRegistry",
    "risk",
    "run_action",
    "list_actions",
    "describe_action",
]


def build_default_registry(context: ActionContext) -> ActionRegistry:
    """Compose the full registry of actions for the CLI.

    Actions that cannot work in the current environment are still
    registered but marked with an ``unavailable`` attribute so the
    Bridge can refuse early with a helpful message.
    """
    from ..utils.platform import capabilities

    caps = capabilities()
    registry = ActionRegistry()
    register_app_control(registry, context)
    register_window_control(registry, context)
    register_process_control(registry, context)
    register_clipboard(registry, context)
    register_file_ops(registry, context)
    register_web(registry, context)
    register_system(registry, context)
    register_gui_advanced(registry, context)

    # Mark actions that cannot work in this environment
    if not caps.get("gui"):
        for name in ("list_windows", "move_window", "minimize_window",
                     "maximize_window", "focus_window", "take_screenshot",
                     "click_at", "type_text", "press_key", "drag_mouse",
                     "scroll_at"):
            action = registry._actions.get(name)
            if action is not None:
                action.unavailable = True  # type: ignore[attr-defined]
                action.unavailable_reason = "این ابزار فقط در محیط گرافیکی قابل استفاده است."  # type: ignore[attr-defined]

    if not caps.get("clipboard"):
        for name in ("clipboard_read", "clipboard_write"):
            action = registry._actions.get(name)
            if action is not None:
                action.unavailable = True  # type: ignore[attr-defined]
                action.unavailable_reason = "کلیپ‌بورد در دسترس نیست. xclip/xsel را نصب کنید."  # type: ignore[attr-defined]

    return registry
