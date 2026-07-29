"""Tests for the action registry and runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.actions import build_default_registry
from local_agent.actions.registry import (
    Action,
    ActionContext,
    ActionRegistry,
    ConfirmationGate,
    Risk,
    run_action,
)
from local_agent.core.config import AssistantSettings, SafetySettings
from local_agent.core.context import RuntimeContext
from local_agent.core.errors import ActionRefused, AssistantError


def _ctx(tmp_path: Path) -> ActionContext:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    runtime = RuntimeContext(settings)
    gate = ConfirmationGate(settings.safety)
    return ActionContext(runtime=runtime, confirmation_gate=gate, work_dir=tmp_path)


def test_decorator_registers_and_runs() -> None:
    registry = ActionRegistry()

    @registry.decorator(
        name="echo",
        description="returns the message",
        parameters={"message": {"type": "string"}},
        required=("message",),
    )
    def echo(*, message: str, context: ActionContext) -> str:
        return message

    assert registry.get("echo").name == "echo"
    assert "echo" in registry.names()


def test_duplicate_registration_raises() -> None:
    registry = ActionRegistry()
    registry.register(
        Action(
            name="dup",
            description="x",
            function=lambda *, context: "",
            parameters={},
            required=(),
        )
    )
    with pytest.raises(ValueError):
        registry.register(
            Action(
                name="dup",
                description="x",
                function=lambda *, context: "",
                parameters={},
                required=(),
            )
        )


def test_run_action_executes_safe_action(tmp_path: Path) -> None:
    registry = ActionRegistry()

    def hello(*, name: str = "world", context: ActionContext) -> str:
        return f"hello {name}"

    registry.register(
        Action(
            name="hello",
            description="hi",
            function=hello,
            parameters={"name": {"type": "string"}},
            required=(),
            risk_level=Risk.SAFE,
        )
    )
    ctx = _ctx(tmp_path)
    result = run_action(registry, "hello", {"name": "test"}, ctx)
    assert result == "hello test"


def test_destructive_action_requires_approval(tmp_path: Path) -> None:
    registry = ActionRegistry()

    def destructive(*, context: ActionContext) -> str:
        return "deleted everything"

    registry.register(
        Action(
            name="kill_everything",
            description="very bad",
            function=destructive,
            parameters={},
            required=(),
            risk_level=Risk.DESTRUCTIVE,
        )
    )
    ctx = _ctx(tmp_path)
    # Without a prompt configured, run_action should raise
    with pytest.raises(NotImplementedError):
        run_action(registry, "kill_everything", {}, ctx)

    # After installing a prompt that declines, the action is refused
    ctx.confirmation_gate.set_callback(lambda *_: (False, "no"))
    with pytest.raises(ActionRefused):
        run_action(registry, "kill_everything", {}, ctx)

    # An approval lets it run
    ctx.confirmation_gate.set_callback(lambda *_: (True, "ok"))
    assert run_action(registry, "kill_everything", {}, ctx) == "deleted everything"


def test_safe_action_runs_without_approval(tmp_path: Path) -> None:
    registry = ActionRegistry()
    registry.register(
        Action(
            name="ping",
            description="safe",
            function=lambda *, context: "pong",
            parameters={},
            required=(),
            risk_level=Risk.SAFE,
        )
    )
    ctx = _ctx(tmp_path)
    assert run_action(registry, "ping", {}, ctx) == "pong"


def test_unknown_action_raises(tmp_path: Path) -> None:
    registry = ActionRegistry()
    ctx = _ctx(tmp_path)
    with pytest.raises(AssistantError):
        run_action(registry, "missing", {}, ctx)


def test_missing_required_argument_raises(tmp_path: Path) -> None:
    registry = ActionRegistry()
    registry.register(
        Action(
            name="needs_x",
            description="x",
            function=lambda *, x, context: str(x),
            parameters={"x": {"type": "integer"}},
            required=("x",),
            risk_level=Risk.SAFE,
        )
    )
    ctx = _ctx(tmp_path)
    with pytest.raises(AssistantError):
        run_action(registry, "needs_x", {}, ctx)
    assert run_action(registry, "needs_x", {"x": 42}, ctx) == "42"


def test_action_failure_is_wrapped(tmp_path: Path) -> None:
    registry = ActionRegistry()
    registry.register(
        Action(
            name="boom",
            description="explodes",
            function=lambda *, context: 1 / 0,  # type: ignore[arg-type]
            parameters={},
            required=(),
            risk_level=Risk.SAFE,
        )
    )
    ctx = _ctx(tmp_path)
    with pytest.raises(AssistantError, match="action boom failed"):
        run_action(registry, "boom", {}, ctx)


def test_default_registry_has_many_actions(tmp_path: Path) -> None:
    registry = build_default_registry(_ctx(tmp_path))
    names = set(registry.names())
    expected = {
        "open_application",
        "close_application",
        "list_applications",
        "locate_application",
        "focus_window",
        "list_windows",
        "move_window",
        "minimize_window",
        "maximize_window",
        "list_processes",
        "kill_process",
        "open_task_manager",
        "clipboard_read",
        "clipboard_write",
        "read_file",
        "write_file",
        "list_directory",
        "make_directory",
        "move_path",
        "delete_path",
        "search_files",
        "web_search",
        "web_fetch",
        "run_shell",
        "system_info",
        "open_path",
        "shutdown_computer",
        "cancel_shutdown",
    }
    missing = expected - names
    assert not missing, f"missing default actions: {sorted(missing)}"


def test_risk_decorator_attaches_level() -> None:
    from local_agent.actions.registry import risk

    @risk(Risk.SYSTEM)
    def my_action(*, context):
        return ""

    assert my_action.__action_risk__ is Risk.SYSTEM  # type: ignore[attr-defined]
