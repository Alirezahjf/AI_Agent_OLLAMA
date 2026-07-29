"""Convenience wrappers used by the CLI."""

from __future__ import annotations

from .registry import Action, ActionContext, ActionRegistry, run_action as _run

# Re-export the most common names so the CLI can import them with one line.
__all__ = [
    "Action",
    "ActionContext",
    "ActionRegistry",
    "run_action",
    "list_actions",
    "describe_action",
]


def run_action(
    registry: ActionRegistry,
    name: str,
    arguments: dict,
    context: ActionContext,
) -> str:
    return _run(registry, name, arguments, context)


def list_actions(registry: ActionRegistry) -> list[Action]:
    return registry.all()


def describe_action(action: Action) -> str:
    risk = action.risk_level.value
    params = ", ".join(sorted(action.parameters.keys())) or "-"
    return f"{action.name}  [risk={risk}]  args=({params})  {action.description}"
