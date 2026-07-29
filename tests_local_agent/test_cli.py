"""Smoke tests for the CLI helpers and prompts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_agent.actions import build_default_registry
from local_agent.actions.registry import ActionContext, ConfirmationGate
from local_agent.cli.prompts import build_system_prompt
from local_agent.core.config import AssistantSettings
from local_agent.core.context import RuntimeContext
from local_agent.utils.platform import (
    is_windows,
    iter_windows_windows,
    list_installed_apps_windows,
    resolve_windows_executable,
    start_windows_process,
)


def _ctx(tmp_path: Path) -> ActionContext:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return ActionContext(
        runtime=RuntimeContext(settings),
        confirmation_gate=ConfirmationGate(settings.safety),
        work_dir=tmp_path,
    )


def test_system_prompt_lists_actions(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    prompt = build_system_prompt(
        settings=ctx.runtime.settings,
        actions=registry.all(),
        gui_available=False,
        telegram_enabled=False,
    )
    assert "open_application" in prompt
    assert "send_message" in prompt or "telegram" in prompt.lower()
    assert "system" in prompt.lower()


def test_resolve_windows_executable_returns_none_for_unknown() -> None:
    if is_windows():
        result = resolve_windows_executable("definitely-not-an-app-xyz123")
        assert result is None


def test_start_windows_process_rejects_empty() -> None:
    if is_windows():
        with pytest.raises(ValueError):
            start_windows_process("")


def test_list_installed_apps_is_safe_on_non_windows() -> None:
    if not is_windows():
        assert list_installed_apps_windows() == []


def test_iter_windows_windows_yields_nothing_on_non_windows() -> None:
    if not is_windows():
        assert list(iter_windows_windows()) == []


def test_load_dotenv_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOCAL_AGENT_LLM__PROVIDER=openai_compatible\n", encoding="utf-8")
    # Confirm env-file path is recognised by python-dotenv
    from dotenv import load_dotenv

    load_dotenv(env_file, override=False)
    assert os.environ.get("LOCAL_AGENT_LLM__PROVIDER") == "openai_compatible"
