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
    """Compose the full registry of actions for the CLI."""
    registry = ActionRegistry()
    register_app_control(registry, context)
    register_window_control(registry, context)
    register_process_control(registry, context)
    register_clipboard(registry, context)
    register_file_ops(registry, context)
    register_web(registry, context)
    register_system(registry, context)
    register_gui_advanced(registry, context)
    return registry
