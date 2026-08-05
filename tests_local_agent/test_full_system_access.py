"""Tests for the «دسترسی کامل سیستم» (Admin/Root mode) toggle.

Covers:
* sandbox ON  — paths outside work_dir are refused;
* sandbox OFF (full_system_access=True) — any path is reachable, but
  sensitive files stay blocked in both modes;
* stateful shell ``cd`` (working_dir is remembered across commands);
* settings round-trip through ``POST /api/settings`` (P3 persistence).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_agent.actions import build_default_registry, run_action
from local_agent.actions.registry import ActionContext, ConfirmationGate
from local_agent.core.config import AssistantSettings, load_settings
from local_agent.core.context import RuntimeContext
from local_agent.core.errors import AssistantError


def _ctx(
    tmp_path: Path, *, full_access: bool = False, restrict_shell: bool = True
) -> tuple[AssistantSettings, ActionContext]:
    settings = AssistantSettings(
        data_dir=tmp_path / "data",
        work_dir=tmp_path / "ws",
    )
    safety_dict = dict(settings.safety.__dict__)
    if full_access:
        safety_dict["full_system_access"] = True
    if restrict_shell:
        safety_dict["restrict_shell_to_workdir"] = True
    settings = settings.with_overrides(
        safety=type(settings.safety)(**safety_dict)
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "ws").mkdir(exist_ok=True)
    gate = ConfirmationGate(settings.safety)
    gate.auto_approve()
    context = ActionContext(
        runtime=RuntimeContext(settings),
        confirmation_gate=gate,
        work_dir=settings.work_dir,
    )
    return settings, context


@pytest.fixture
def restricted(tmp_path: Path) -> tuple[AssistantSettings, ActionContext]:
    """Normal mode: workspace sandbox + shell restricted to work_dir."""
    return _ctx(tmp_path, full_access=False)


@pytest.fixture
def full_access(tmp_path: Path) -> tuple[AssistantSettings, ActionContext]:
    """Admin/Root mode: sandbox lifted, shell free to move anywhere."""
    return _ctx(tmp_path, full_access=True)


# ---------------------------------------------------------------------------
# file tools
# ---------------------------------------------------------------------------


def test_restricted_mode_blocks_paths_outside_workdir(restricted) -> None:
    settings, ctx = restricted
    registry = build_default_registry(ctx)
    outside = settings.data_dir / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(AssistantError) as excinfo:
        run_action(registry, "read_file", {"path": str(outside)}, ctx)
    assert "خارج از فضای کاری" in str(excinfo.value)


def test_full_access_allows_paths_outside_workdir(full_access) -> None:
    settings, ctx = full_access
    registry = build_default_registry(ctx)
    outside = settings.data_dir / "notes.txt"
    outside.write_text("hello", encoding="utf-8")
    result = run_action(registry, "read_file", {"path": str(outside)}, ctx)
    assert "hello" in result


def test_full_access_still_blocks_sensitive_files(full_access) -> None:
    settings, ctx = full_access
    registry = build_default_registry(ctx)
    # A .env inside the *work dir* is already blocked; try one outside too.
    dotenv = settings.data_dir / ".env"
    dotenv.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(AssistantError):
        run_action(registry, "read_file", {"path": str(dotenv)}, ctx)
    ssh_dir = settings.data_dir / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    key = ssh_dir / "id_rsa"
    key.write_text("PRIVATE", encoding="utf-8")
    with pytest.raises(AssistantError):
        run_action(registry, "read_file", {"path": str(key)}, ctx)


def test_restricted_mode_also_blocks_sensitive_files(restricted) -> None:
    settings, ctx = restricted
    registry = build_default_registry(ctx)
    dotenv = settings.work_dir / ".env"
    dotenv.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(AssistantError):
        run_action(registry, "read_file", {"path": str(dotenv)}, ctx)


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def test_shell_cd_is_stateful(full_access) -> None:
    settings, ctx = full_access
    registry = build_default_registry(ctx)
    # cd into a data-dir folder (outside the workspace — only allowed in
    # full-access mode), then run pwd: the cwd must persist.
    sub = settings.data_dir / "subdir"
    sub.mkdir(exist_ok=True)
    run_action(registry, "run_shell", {"command": "cd .", "working_dir": str(sub)}, ctx)
    assert ctx.extra.get("shell_cwd") == str(sub.resolve())
    result = run_action(registry, "run_shell", {"command": "pwd"}, ctx)
    assert str(sub.resolve()) in result


def test_shell_restricted_refuses_outside_working_dir(restricted) -> None:
    settings, ctx = restricted
    registry = build_default_registry(ctx)
    outside = settings.data_dir
    outside.mkdir(exist_ok=True)
    with pytest.raises(AssistantError):
        run_action(registry, "run_shell", {"command": "pwd", "working_dir": str(outside)}, ctx)


def test_shell_restricted_falls_back_on_stale_cwd(restricted) -> None:
    """A cwd remembered during a full-access session must not escape the sandbox."""
    settings, ctx = restricted
    registry = build_default_registry(ctx)
    outside = settings.data_dir
    outside.mkdir(exist_ok=True)
    ctx.extra["shell_cwd"] = str(outside.resolve())
    result = run_action(registry, "run_shell", {"command": "pwd"}, ctx)
    assert str(settings.work_dir.resolve()) in result


# ---------------------------------------------------------------------------
# settings persistence (P3)
# ---------------------------------------------------------------------------


def test_settings_round_trip_through_web_api(web_server) -> None:
    import requests

    base = f"http://127.0.0.1:{web_server.port}"
    payload = {
        "provider": "openai_compatible",
        "model": "gpt-4o-mini",
        "openai_base_url": "https://api.avalai.ir/v1",
        "openai_api_key": "sk-roundtrip-secret",
        "confirm_mode": "always",
        "full_system_access": True,
        "work_dir": str(web_server.settings.data_dir / "ws2"),
        "telegram": {
            "enabled": True,
            "api_id": 999,
            "api_hash": "h" * 32,
            "phone": "+989120000000",
        },
        "gmail": {"enabled": True, "app_password": "abcd efgh ijkl mnop"},
    }
    r = requests.post(base + "/api/settings", json=payload, timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"]["full_system_access"] is True
    assert body["saved"]["telegram_enabled"] is True
    assert body["saved"]["gmail_enabled"] is True
    assert "sk-roundtrip-secret" not in r.text, "کلید API هرگز در پاسخ نباید بیاید"

    # Reload from disk exactly like a restart would.
    config_path = web_server.settings.data_dir / "config.json"
    reloaded = load_settings(config_path=config_path, data_dir=web_server.settings.data_dir)
    assert reloaded.llm.provider == "openai_compatible"
    assert reloaded.llm.openai_model == "gpt-4o-mini"
    assert reloaded.llm.openai_api_key == "sk-roundtrip-secret"
    assert reloaded.safety.confirm_mode == "always"
    assert reloaded.safety.full_system_access is True
    assert reloaded.work_dir == (web_server.settings.data_dir / "ws2")
    assert reloaded.telegram.api_id == 999
    assert reloaded.telegram.enabled is True
    assert reloaded.gmail.enabled is True
    assert reloaded.gmail.app_password == "abcd efgh ijkl mnop"


def test_web_api_never_returns_raw_api_key(web_server) -> None:
    import requests

    base = f"http://127.0.0.1:{web_server.port}"
    requests.post(
        base + "/api/settings",
        json={"provider": "openai_compatible", "openai_api_key": "sk-top-secret-42"},
        timeout=10,
    )
    status = requests.get(base + "/api/status", timeout=5).json()
    text = json.dumps(status, ensure_ascii=False)
    assert "sk-top-secret-42" not in text
    s = status["settings"]["settings"]
    assert s["openai_api_key_set"] is True


def test_atomic_config_write_leaves_no_tmp_file(web_server) -> None:
    """P3: the atomic write must not leave a .tmp next to config.json."""
    import requests

    base = f"http://127.0.0.1:{web_server.port}"
    requests.post(base + "/api/settings", json={"provider": "ollama"}, timeout=10)
    data_dir = web_server.settings.data_dir
    leftovers = [p for p in data_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
