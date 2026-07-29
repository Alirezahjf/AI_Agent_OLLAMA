"""Tests for AssistantSettings and load_settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_agent.core.config import (
    AssistantSettings,
    LLMSettings,
    SafetySettings,
    TelegramSettings,
    _apply_env_overrides,
    _coerce_env_value,
    load_settings,
)
from local_agent.core.errors import ConfigError


def test_settings_is_frozen() -> None:
    settings = AssistantSettings()
    with pytest.raises(Exception):
        settings.data_dir = Path("/tmp")  # type: ignore[misc]


def test_settings_to_dict_roundtrip(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    payload = settings.to_dict()
    again = AssistantSettings.from_dict(payload)
    assert again.data_dir == settings.data_dir
    assert again.work_dir == settings.work_dir


def test_from_dict_rejects_bad_payload() -> None:
    with pytest.raises(ConfigError):
        AssistantSettings.from_dict({"llm": {"provider": "not-a-real-provider-xyz"}})


def test_coerce_env_value_handles_types() -> None:
    assert _coerce_env_value("42") == 42
    assert _coerce_env_value("3.14") == 3.14
    assert _coerce_env_value("true") is True
    assert _coerce_env_value("false") is False
    assert _coerce_env_value("hello") == "hello"


def test_apply_env_overrides_uses_double_underscore_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCAL_AGENT_LLM__PROVIDER", "openai_compatible")
    monkeypatch.setenv("LOCAL_AGENT_LLM__OPENAI_API_KEY", "test-key")
    out = _apply_env_overrides({"llm": {"provider": "ollama"}})
    assert out["llm"]["provider"] == "openai_compatible"
    assert out["llm"]["openai_api_key"] == "test-key"


def test_load_settings_creates_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_DATA_DIR", str(tmp_path))
    settings = load_settings()
    assert (tmp_path / "config.json").is_file()
    assert settings.data_dir == tmp_path
    assert settings.log_dir.is_dir()


def test_load_settings_uses_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"data_dir": str(tmp_path), "llm": {"provider": "ollama"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_AGENT_CONFIG", str(config_path))
    monkeypatch.setenv("LOCAL_AGENT_LLM__PROVIDER", "openai_compatible")
    settings = load_settings()
    assert settings.llm.provider == "openai_compatible"


def test_load_settings_invalid_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG", str(config_path))
    with pytest.raises(ConfigError):
        load_settings()


def test_settings_with_overrides_keeps_other_fields(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    new = settings.with_overrides(work_dir=tmp_path / "subdir")
    assert new.work_dir == tmp_path / "subdir"
    assert new.data_dir == tmp_path
    assert new.llm == settings.llm
