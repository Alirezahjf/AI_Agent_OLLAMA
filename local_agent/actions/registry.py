"""Action registry: the bridge between tools (LLM-facing) and side effects."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.context import RuntimeContext
from .groups import infer_group, group_by_id
from ..core.errors import ActionRefused, AssistantError, DependencyMissing
from ..core.logging_setup import get_logger

logger = get_logger("actions")


class Risk(Enum):
    """How dangerous an action is.

    SAFE — never asks for confirmation (e.g. take a screenshot).
    DESTRUCTIVE — asks for confirmation (e.g. send a Telegram message).
    SYSTEM — touches the OS in ways that cannot easily be undone
             (e.g. shutdown, restart). Always requires confirmation.
    """

    SAFE = "safe"
    DESTRUCTIVE = "destructive"
    SYSTEM = "system"


def risk(level: Risk) -> Callable[[Callable], Callable]:
    """Decorator to attach a risk level to an action function."""

    def decorate(func: Callable) -> Callable:
        func.__action_risk__ = level  # type: ignore[attr-defined]
        return func

    return decorate


@dataclass
class ActionContext:
    """Shared resources passed to every action.

    The context is created once at startup and reused. The runtime is
    shared so actions can read/write the conversation (e.g. to record
    that a file was deleted). The settings come from the loaded config.
    """

    runtime: RuntimeContext
    confirmation_gate: ConfirmationGate
    work_dir: Any  # pathlib.Path
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """Describes a single LLM-callable action.

    The ``function`` attribute is the actual Python callable; ``schema``
    is the JSON-Schema description the model sees. ``risk_level`` lets
    the CLI decide whether to ask for confirmation.
    """

    name: str
    description: str
    function: Callable[..., str]
    parameters: dict[str, Any]
    required: tuple[str, ...] = ()
    risk_level: Risk = Risk.SAFE
    unavailable: bool = False
    unavailable_reason: str = ""
    group: str = ""

    def __post_init__(self) -> None:
        if not self.group or group_by_id(self.group) is None:
            self.group = infer_group(self.name)

    # Optional runtime override: when set and returns True, the action
    # always asks for confirmation regardless of confirm_mode/risk
    # (used e.g. by ``telegram.confirm_send``).  Signature:
    # (safety, arguments) -> bool.
    confirm_override: Callable[[Any, Any], bool] | None = None
    # Optional runtime skip: when set and returns True, the action NEVER asks
    # for confirmation, even for a destructive/always policy (used to honour
    # ``confirm_send=False`` per account).  Signature: (safety, arguments).
    confirm_skip: Callable[[Any, Any], bool] | None = None

    def to_tool_definition(self):
        from ..llm.client import ToolDefinition

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            required=self.required,
        )

    def needs_confirmation(self, safety, arguments: dict[str, Any] | None = None) -> bool:
        if self.confirm_skip is not None and self.confirm_skip(safety, arguments):
            return False
        if self.confirm_override is not None and self.confirm_override(safety, arguments):
            return True
        if self.risk_level == Risk.SAFE:
            return False
        if safety.confirm_mode == "never":
            return False
        if safety.confirm_mode == "always":
            return True
        # 'destructive' (default)
        if self.risk_level == Risk.SYSTEM:
            return True
        return safety.require_confirm_for_destructive


class ActionRegistry:
    """A typed registry mapping names to :class:`Action` instances."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}
        self._lock = threading.Lock()

    def register(self, action: Action) -> None:
        with self._lock:
            if action.name in self._actions:
                raise ValueError(f"action already registered: {action.name}")
            self._actions[action.name] = action

    def decorator(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        required: tuple[str, ...] = (),
        risk_level: Risk = Risk.SAFE,
        confirm_override: Callable[[Any, Any], bool] | None = None,
        confirm_skip: Callable[[Any, Any], bool] | None = None,
    ) -> Callable[[Callable], Callable]:
        def wrap(func: Callable) -> Callable:
            actual_risk = getattr(func, "__action_risk__", risk_level)
            self.register(
                Action(
                    name=name,
                    description=description,
                    function=func,
                    parameters=parameters,
                    required=required,
                    risk_level=actual_risk,
                    confirm_override=confirm_override,
                    confirm_skip=confirm_skip,
                )
            )
            return func

        return wrap

    def get(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError as exc:
            raise AssistantError(f"unknown action: {name}") from exc

    def all(self) -> list[Action]:
        with self._lock:
            return list(self._actions.values())

    def names(self) -> list[str]:
        return sorted(self._actions.keys())


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------


class ConfirmationGate:
    """Decides whether an action may execute.

    A real gate would call back into the CLI (or, when no human is in
    the loop, an auto-approve policy). For headless/test use we expose
    ``auto_approve`` and ``auto_deny`` helpers.
    """

    def __init__(self, safety) -> None:
        self._safety = safety
        self._lock = threading.Lock()
        self._auto_approve_all = False
        self._auto_deny_all = False

    def ask(self, action: Action, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(approved, reason)``.

        The CLI replaces this with a real prompt. Tests use the
        programmatic helpers below.
        """
        if self._auto_approve_all:
            return True, "auto-approve-all is on"
        if self._auto_deny_all:
            return False, "auto-deny-all is on"
        raise NotImplementedError(
            "ConfirmationGate.ask must be replaced by a UI prompt before use"
        )

    def auto_approve(self) -> None:
        with self._lock:
            self._auto_approve_all = True
            self._auto_deny_all = False

    def auto_deny(self) -> None:
        with self._lock:
            self._auto_approve_all = False
            self._auto_deny_all = True

    def reset(self) -> None:
        with self._lock:
            self._auto_approve_all = False
            self._auto_deny_all = False

    def set_callback(self, callback: Callable[[Action, dict[str, Any]], tuple[bool, str]]) -> None:
        """Install the real prompt. The CLI does this at startup."""
        self.ask = callback  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Action runner
# ---------------------------------------------------------------------------


def run_action(
    registry: ActionRegistry,
    name: str,
    arguments: dict[str, Any],
    context: ActionContext,
) -> str:
    """Validate, confirm if needed, and execute an action."""
    action = registry.get(name)
    if action.unavailable:
        raise AssistantError(
            action.unavailable_reason or f"action {name!r} is not available in this environment"
        )
    arguments = _coerce_arguments(action, arguments)
    _validate_arguments(action, arguments)

    if action.needs_confirmation(context.runtime.settings.safety, arguments):
        approved, reason = context.confirmation_gate.ask(action, arguments)
        if not approved:
            raise ActionRefused(f"action {name!r} was not approved: {reason}")
        logger.info("action %s approved (%s)", name, reason)

    logger.info("running action %s with %s", name, _summarize_arguments(arguments))
    try:
        result = action.function(**arguments, context=context)
    except TypeError as exc:
        raise AssistantError(f"action {name} got bad arguments: {exc}") from exc
    except DependencyMissing:
        raise
    except ActionRefused:
        raise
    except AssistantError:
        raise
    except Exception as exc:
        logger.exception("action %s crashed", name)
        raise AssistantError(f"action {name} failed: {exc}") from exc
    return str(result)


def list_actions(registry: ActionRegistry) -> list[Action]:
    return registry.all()


def describe_action(action: Action) -> str:
    risk = action.risk_level.value
    params = ", ".join(sorted(action.parameters.keys())) or "-"
    suffix = " [UNAVAILABLE]" if action.unavailable else ""
    return f"{action.name}  [risk={risk}]  args=({params}){suffix}  {action.description}"


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def _coerce_arguments(action: Action, arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AssistantError(f"arguments for {action.name} must be an object")
    return {key: _coerce_value(value) for key, value in arguments.items()}


def _coerce_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_coerce_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _coerce_value(v) for k, v in value.items()}
    return str(value)


def _validate_arguments(action: Action, arguments: dict[str, Any]) -> None:
    signature = inspect.signature(action.function)
    has_context_kw = any(
        param.name == "context" and param.kind in (param.KEYWORD_ONLY, param.POSITIONAL_OR_KEYWORD)
        for param in signature.parameters.values()
    )
    if not has_context_kw:
        raise AssistantError(
            f"action {action.name} does not accept a 'context' kwarg; "
            "every action must take context= to access runtime resources"
        )
    for required in action.required:
        if required not in arguments:
            raise AssistantError(f"action {action.name} missing required arg: {required}")


def _summarize_arguments(arguments: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in arguments.items():
        rendered = str(value)
        if len(rendered) > 80:
            rendered = rendered[:77] + "..."
        parts.append(f"{key}={rendered!r}")
    return " ".join(parts) if parts else "<no args>"
