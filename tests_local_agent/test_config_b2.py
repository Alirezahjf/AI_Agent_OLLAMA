"""B2 — Settings must survive a restart even with a custom ``data_dir``.

The bug: ``load_settings`` read from the fixed settings file (``LOCAL_AGENT_CONFIG``
or ``~/.local_assistant/config.json``) but ``_persist_settings`` wrote to
``<data_dir>/config.json`` via the old ``config_path`` property.  When a config
carried a ``data_dir`` pointing at an old project folder, saved settings were
written to a file that was never read again — they appeared to "reset" on every
restart.

These tests are fully offline and exercise the real web server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests


def _build_server(settings):
    """Start a real web server around an already-loaded ``settings`` object."""
    import socket as _socket

    from local_agent.bridge.api.client import BridgeClient, _InProcessBackend, _welcome_to_info
    from local_agent.bridge.server.server import BridgeServer
    from local_agent.web.app import WebServer

    def _free_port() -> int:
        sock = _socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    bridge = BridgeServer(settings)
    bridge.start_in_process()
    backend = _InProcessBackend(bridge)
    backend._started = True
    client = BridgeClient(backend, _welcome_to_info(bridge.welcome()))
    server = WebServer(settings, client, host="127.0.0.1", port=_free_port())
    server.start_in_thread()
    if not server.wait_until_ready():
        server.stop()
        pytest.fail("web server did not start")
    return server


@pytest.fixture
def custom_data_dir_config(tmp_path: Path) -> Path:
    """Write a config whose ``data_dir`` points somewhere other than the
    settings file's own folder (the B2 user scenario)."""
    elsewhere = tmp_path / "old_project_folder"
    elsewhere.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_dir": str(elsewhere),
        "llm": {
            "provider": "openai_compatible",
            "openai_model": "gpt-4o-mini",
            "openai_base_url": "https://api.avalai.ir/v1",
            "openai_api_key": "sk-b2-secret",
        },
        "safety": {"confirm_mode": "always"},
        "work_dir": str(tmp_path),
    }), encoding="utf-8")
    return config


def test_settings_roundtrip_with_custom_data_dir(custom_data_dir_config, tmp_path) -> None:
    """POST /api/settings persists, then a fresh load_settings() keeps every value.

    Critically the write must land in the *fixed* settings file (the one that
    was read), NOT in ``<data_dir>/config.json``.
    """
    from local_agent.core.config import load_settings

    settings = load_settings(custom_data_dir_config)
    assert settings.data_dir == tmp_path / "old_project_folder"
    assert settings.effective_config_path() == custom_data_dir_config

    server = _build_server(settings)
    base = f"http://127.0.0.1:{server.port}"

    # Simulate the user changing a setting from the UI.
    r = requests.post(
        base + "/api/settings",
        json={
            "provider": "openai_compatible",
            "model": "claude-sonnet-5",
            "confirm_mode": "destructive",
            "full_system_access": True,
            "telegram": {"enabled": True, "api_id": 123, "api_hash": "h" * 32, "phone": "+989120000000"},
            "gmail": {"enabled": True, "confirm_send": False},
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text

    # Nothing may be written to the old data-dir's config.
    old = tmp_path / "old_project_folder" / "config.json"
    assert not old.exists(), "must not write to <data_dir>/config.json anymore"

    # Now simulate a restart: reload from the fixed settings file.
    reloaded = load_settings(custom_data_dir_config)
    assert reloaded.llm.provider == "openai_compatible"
    assert reloaded.llm.openai_model == "claude-sonnet-5"
    assert reloaded.safety.confirm_mode == "destructive"
    assert reloaded.safety.full_system_access is True
    assert reloaded.telegram.enabled is True
    assert reloaded.telegram.api_id == 123
    assert reloaded.telegram.phone == "+989120000000"
    assert reloaded.gmail.enabled is True
    assert reloaded.gmail.confirm_send is False
    server.stop()


def test_api_key_never_returned_in_get(custom_data_dir_config) -> None:
    """The raw API key must never appear in GET responses."""
    from local_agent.core.config import load_settings

    settings = load_settings(custom_data_dir_config)
    server = _build_server(settings)
    base = f"http://127.0.0.1:{server.port}"
    try:
        body = requests.get(base + "/api/status", timeout=5).json()
        blob = json.dumps(body)
        assert "sk-b2-secret" not in blob
        assert body["settings"]["settings"]["openai_api_key_set"] is True
    finally:
        server.stop()


def test_migration_from_legacy_data_dir_config(tmp_path: Path, monkeypatch) -> None:
    """Old non-default settings sitting in ``<data_dir>/config.json`` are folded
    into the fixed settings file on first load."""
    from local_agent.core.config import load_settings

    data_dir = tmp_path / "olddata"
    data_dir.mkdir(parents=True)
    legacy = data_dir / "config.json"
    legacy.write_text(json.dumps({
        "llm": {"provider": "openai_compatible", "openai_model": "gpt-4o-mini",
                "openai_api_key": "sk-migrated", "openai_base_url": "https://api.avalai.ir/v1"},
        "telegram": {"enabled": True, "api_id": 99, "api_hash": "h" * 32, "phone": "+1"},
    }), encoding="utf-8")

    config = tmp_path / "config.json"
    # Main config exists but only carries the data_dir pointer (no settings).
    config.write_text(json.dumps({"data_dir": str(data_dir)}), encoding="utf-8")

    settings = load_settings(config)
    assert settings.effective_config_path() == config
    # The legacy settings must now be readable from the fixed file.
    persisted = json.loads(config.read_text(encoding="utf-8"))
    assert persisted["llm"]["provider"] == "openai_compatible"
    assert persisted["llm"]["openai_api_key"] == "sk-migrated"
    assert persisted["telegram"]["enabled"] is True


def test_migration_does_not_override_existing_main_settings(tmp_path: Path) -> None:
    """If the fixed settings file already has a value, the legacy file must not
    override it."""
    from local_agent.core.config import load_settings

    data_dir = tmp_path / "olddata"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({
        "llm": {"provider": "ollama", "ollama_model": "qwen2.5:7b"},
    }), encoding="utf-8")

    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "data_dir": str(data_dir),
        "llm": {"provider": "openai_compatible", "openai_model": "claude-sonnet-5"},
    }), encoding="utf-8")

    settings = load_settings(config)
    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.openai_model == "claude-sonnet-5"
