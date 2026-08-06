"""Offline tests — گ ۸: تنظیمات باید بعد از ری‌استارت باقی بمانند.

سناریوی کاربر: config.json واقعی در پوشهٔ پروژه است (data_dir=پروژه) ولی
خواندن/نوشتن به مسیر ثابت ~/.local_assistant/config.json می‌رفت و همه‌چیز
«پَریده» به نظر می‌رسید. حالا:

1) وقتی فایل پیش‌فرض وجود ندارد، یک تابع واحد دنبال config.json واقعی در
   پوشهٔ جاری/پروژه می‌گردد و همان را منبع حقیقت می‌کند.
2) مهاجرت قوی: اگر config واقعی در جای دیگر باشد و مسیر اصلی خالی باشد،
   مقادیر یک‌بار به مسیر اصلی منتقل می‌شوند.
3) همهٔ نوشتن‌ها (POST /api/settings) به همان مسیر خوانده‌شده می‌روند.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import local_agent.core.config as cfg


def _make_project_config(project: Path, **overrides) -> Path:
    payload = {
        "data_dir": str(project),
        "work_dir": str(project),
        "llm": {
            "provider": "openai_compatible",
            "openai_model": "gpt-4o-mini",
            "openai_base_url": "https://api.avalai.ir/v1",
            "openai_api_key": "sk-project-real",
        },
        "safety": {"confirm_mode": "always", "full_system_access": True},
        "telegram": {"enabled": True, "api_id": 77, "api_hash": "h" * 32, "phone": "+989120000000"},
        "gmail": {"enabled": True, "username": "user@gmail.com", "confirm_send": False},
    }
    payload.update(overrides)
    path = project / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """فایل پیش‌فرض ~/.local_assistant/config.json را به جایی خالی هدایت کن."""
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(cfg, "_DEFAULT_DATA_DIR", fake_home / ".local_assistant")
    # «پوشهٔ جاری» هم برای سناریوهای جست‌وجو قابل کنترل باشد
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
    return fake_home


def test_load_settings_finds_project_config_when_default_missing(
    tmp_path: Path, isolated_home
) -> None:
    _make_project_config(tmp_path)
    settings = cfg.load_settings()
    assert settings.effective_config_path() == tmp_path / "config.json"
    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.openai_api_key == "sk-project-real"
    assert settings.gmail.username == "user@gmail.com"
    # چیزی در خانهٔ پیش‌فرض ساخته نشده باشد
    assert not (isolated_home / ".local_assistant" / "config.json").exists()


def test_settings_roundtrip_after_restart_via_web(
    tmp_path: Path, isolated_home
) -> None:
    """config واقعی در پوشهٔ پروژه → POST /api/settings → لود مجدد (شبیه‌سازی
    ری‌استارت) → همهٔ مقادیر باقی می‌مانند."""
    import socket as _socket

    from local_agent.bridge.api.client import BridgeClient, _InProcessBackend, _welcome_to_info
    from local_agent.bridge.server.server import BridgeServer
    from local_agent.web.app import WebServer

    project = tmp_path / "project"
    project.mkdir()
    _make_project_config(project)

    # پوشهٔ جاری را به project هدایت کن تا جست‌وجو همان را پیدا کند
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))
    try:
        settings = cfg.load_settings()
        assert settings.effective_config_path() == project / "config.json"

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
        assert server.wait_until_ready()
        base = f"http://127.0.0.1:{server.port}"

        r = requests.post(base + "/api/settings", json={
            "provider": "openai_compatible",
            "model": "claude-sonnet-5",
            "confirm_mode": "destructive",
            "work_dir": str(project / "ws"),
            "full_system_access": False,
            "telegram": {"enabled": True, "confirm_send": True},
            "gmail": {"enabled": True, "username": "newuser@gmail.com"},
        }, timeout=10)
        assert r.status_code == 200, r.text
        server.stop()

        # شبیه‌سازی ری‌استارت: دوباره از همان مسیر بخوان
        reloaded = cfg.load_settings()
        assert reloaded.effective_config_path() == project / "config.json"
        assert reloaded.llm.openai_model == "claude-sonnet-5"
        assert reloaded.llm.openai_api_key == "sk-project-real"  # کلید قبلی دست‌نخورده
        assert reloaded.safety.confirm_mode == "destructive"
        assert reloaded.safety.full_system_access is False
        assert reloaded.work_dir == project / "ws"
        assert reloaded.telegram.enabled is True
        assert reloaded.telegram.api_id == 77
        assert reloaded.gmail.enabled is True
        assert reloaded.gmail.username == "newuser@gmail.com"
    finally:
        monkeypatch.undo()


def test_migration_from_project_config_to_empty_default(
    tmp_path: Path, isolated_home
) -> None:
    """فایل پیش‌فرض وجود دارد ولی خالی/قالب است؛ config واقعی در پوشهٔ جاری →
    مقادیرش به مسیر اصلی منتقل می‌شود (مهاجرت قوی)."""
    default_dir = isolated_home / ".local_assistant"
    default_dir.mkdir(parents=True)
    default_cfg = default_dir / "config.json"
    default_cfg.write_text(json.dumps({"data_dir": str(default_dir)}), encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    _make_project_config(project)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))
    try:
        settings = cfg.load_settings()
        assert settings.effective_config_path() == default_cfg
        # مقادیر واقعی از پوشهٔ پروژه به فایل اصلی منتقل شده‌اند
        assert settings.llm.provider == "openai_compatible"
        assert settings.llm.openai_api_key == "sk-project-real"
        assert settings.gmail.username == "user@gmail.com"
        persisted = json.loads(default_cfg.read_text(encoding="utf-8"))
        assert persisted["llm"]["openai_api_key"] == "sk-project-real"
    finally:
        monkeypatch.undo()


def test_foreign_config_json_is_ignored(tmp_path: Path, isolated_home) -> None:
    """config.json متعلق به یک پروژهٔ دیگر نباید به‌عنوان تنظیمات ما انتخاب شود."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.json").write_text(
        json.dumps({"database": {"host": "db.internal"}, "port": 5432}),
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))
    try:
        settings = cfg.load_settings()
        # به پیش‌فرض برگشته و قالب ساخته است؛ نه فایل خارجی
        assert settings.effective_config_path() == isolated_home / ".local_assistant" / "config.json"
    finally:
        monkeypatch.undo()


def test_doctor_warns_on_stray_project_config(tmp_path: Path, isolated_home) -> None:
    from local_agent.diagnostics import WARN, check_config_consistency

    default_dir = isolated_home / ".local_assistant"
    default_dir.mkdir(parents=True)
    (default_dir / "config.json").write_text(json.dumps({"data_dir": str(default_dir)}), encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    _make_project_config(project)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))
    try:
        settings = cfg.load_settings()
        result = check_config_consistency(settings)
        assert result.status == WARN
        assert "سرگردان" in result.detail
    finally:
        monkeypatch.undo()
