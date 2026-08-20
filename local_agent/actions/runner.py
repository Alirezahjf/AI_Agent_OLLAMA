"""Convenience wrappers used by the CLI."""

from __future__ import annotations

from .registry import Action, ActionContext, ActionRegistry
from .registry import run_action as _run

# Re-export the most common names so the CLI can import them with one line.
__all__ = [
    "Action",
    "ActionContext",
    "ActionRegistry",
    "describe_action",
    "list_actions",
    "run_action",
]


def run_action(
    registry: ActionRegistry,
    name: str,
    arguments: dict,
    context: ActionContext,
    *,
    human_confirmed: bool = False,
) -> str:
    return _run(
        registry,
        name,
        arguments,
        context,
        human_confirmed=human_confirmed,
    )


def list_actions(registry: ActionRegistry) -> list[Action]:
    return registry.all()


def describe_action(action: Action) -> str:
    risk = action.risk_level.value
    params = ", ".join(sorted(action.parameters.keys())) or "-"
    return f"{action.name}  [risk={risk}]  args=({params})  {action.description}"
