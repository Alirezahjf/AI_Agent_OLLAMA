"""Tests for the Web UI app."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from local_agent.bridge.server.server import BridgeServer
from local_agent.core.config import AssistantSettings
from local_agent.web.app import WebServer, create_app


def test_root_endpoint_returns_html(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/", timeout=3)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_api_status(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/status", timeout=3)
    assert r.status_code == 200
    body = r.json()
    assert "bridge" in body
    assert "settings" in body


def test_api_actions(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/actions", timeout=3)
    assert r.status_code == 200
    actions = r.json()
    assert any(d.startswith("open_application") for d in actions)


def test_api_invoke(web_server: WebServer, tmp_path: Path) -> None:
    (tmp_path / "web-test.txt").write_text("from web", encoding="utf-8")
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/invoke",
        json={"name": "read_file", "arguments": {"path": "web-test.txt"}, "auto_confirm": True},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert "from web" in body["text"]


def test_api_clear(web_server: WebServer) -> None:
    r = requests.post(f"http://127.0.0.1:{web_server.port}/api/clear", timeout=3)
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] is True
    # History should be empty now
    r2 = requests.get(f"http://127.0.0.1:{web_server.port}/api/history", timeout=3)
    assert r2.json() == []


def test_static_files_served(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/static/app.js", timeout=3)
    # Static may or may not exist depending on installation; we just verify
    # that the route doesn't 500
    assert r.status_code in {200, 404}


# ---------------------------------------------------------------------------
# Redesigned UI: markup, assets, and the new endpoints
# ---------------------------------------------------------------------------


WEB_DIR = Path(__file__).resolve().parents[1] / "local_agent" / "web"


def test_index_is_rtl_persian_and_dark_by_default() -> None:
    html = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'lang="fa"' in html
    assert 'dir="rtl"' in html
    assert 'data-theme="dark"' in html
    assert 'name="viewport"' in html  # responsive


def test_index_wires_the_alpine_component() -> None:
    html = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'x-data="assistantApp()"' in html
    assert "vendor/alpine.min.js" in html
    assert "vendor/marked.min.js" in html
    assert "vendor/highlight.min.js" in html


def test_index_contains_every_required_ui_region() -> None:
    html = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    for marker in (
        'class="topbar"',      # header with connection status
        'class="sidebar"',     # conversation history
        'class="chat"',        # message list
        'class="composer"',    # input box
        'class="panel"',       # actions / status side panel
        'class="empty"',       # empty state
        'class="dropzone"',    # drag & drop
        'class="toasts"',      # notifications
        "settingsOpen",        # settings modal
        "exportOpen",          # export modal
        "shortcutsOpen",       # keyboard shortcuts modal
        "approval",            # approval dialog
        "tool__status",        # tool execution cards
    ):
        assert marker in html, f"missing UI region: {marker}"


def test_vendored_assets_are_present_so_the_ui_works_offline() -> None:
    vendor = WEB_DIR / "static" / "vendor"
    for name in (
        "alpine.min.js",
        "marked.min.js",
        "highlight.min.js",
        "hljs-dark.min.css",
        "hljs-light.min.css",
        "fonts/Vazirmatn-Regular.woff2",
    ):
        asset = vendor / name
        assert asset.is_file(), f"missing vendored asset: {name}"
        assert asset.stat().st_size > 0


def test_stylesheet_defines_both_themes() -> None:
    css = (WEB_DIR / "static" / "style.css").read_text(encoding="utf-8")
    assert 'html[data-theme="dark"]' in css
    assert 'html[data-theme="light"]' in css
    assert "@media (max-width: 860px)" in css  # mobile breakpoint
    assert "prefers-reduced-motion" in css     # accessibility


def test_app_js_exposes_the_component_factory() -> None:
    js = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.assistantApp = assistantApp" in js
    for feature in (
        "renderMarkdown", "respondApproval", "exportConversation",
        "toggleVoice", "onDrop", "toggleTheme", "onGlobalKey", "beep",
    ):
        assert feature in js, f"missing behaviour: {feature}"


def test_ui_assets_are_served(web_server: WebServer) -> None:
    base = f"http://127.0.0.1:{web_server.port}"
    for path in ("/static/app.js", "/static/style.css", "/static/vendor/alpine.min.js"):
        response = requests.get(base + path, timeout=3)
        assert response.status_code == 200, path
        assert response.content


def test_healthz(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/healthz", timeout=3)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_api_actions_detail_is_structured(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/actions/detail", timeout=5)
    assert r.status_code == 200
    actions = r.json()
    assert actions and isinstance(actions, list)
    entry = next(a for a in actions if a["name"] == "open_application")
    assert entry["risk"] in {"safe", "destructive", "system"}
    assert isinstance(entry["args"], list)
    assert entry["description"]


def test_parse_action_line_handles_garbage() -> None:
    from local_agent.web.app import parse_action_line

    good = parse_action_line("read_file  [risk=safe]  args=(path)  Read a text file")
    assert good == {
        "name": "read_file",
        "risk": "safe",
        "args": ["path"],
        "description": "Read a text file",
    }
    degraded = parse_action_line("something unexpected")
    assert degraded["name"] == "something"
    assert degraded["risk"] == "safe"


def test_upload_writes_into_the_workspace(web_server: WebServer, tmp_path: Path) -> None:
    import base64

    payload = base64.b64encode("سلام".encode("utf-8")).decode("ascii")
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/upload",
        json={"name": "uploaded.txt", "content_base64": payload},
        timeout=5,
    )
    assert r.status_code == 200
    saved = Path(r.json()["saved"])
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == "سلام"


def test_upload_strips_directory_components(web_server: WebServer) -> None:
    import base64

    payload = base64.b64encode(b"x").decode("ascii")
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/upload",
        json={"name": "../escape.txt", "content_base64": payload},
        timeout=5,
    )
    assert r.status_code == 200
    assert Path(r.json()["saved"]).name == "escape.txt"


def test_file_endpoint_serves_and_guards(web_server: WebServer, tmp_path: Path) -> None:
    (tmp_path / "artifact.md").write_text("# hi", encoding="utf-8")
    base = f"http://127.0.0.1:{web_server.port}"
    ok = requests.get(base + "/api/file", params={"path": "artifact.md"}, timeout=3)
    assert ok.status_code == 200
    assert "# hi" in ok.text

    for escape in ("../../etc/passwd", "/etc/passwd"):
        blocked = requests.get(base + "/api/file", params={"path": escape}, timeout=3)
        assert blocked.status_code in {403, 404}

    missing = requests.get(base + "/api/file", params={"path": "nope.txt"}, timeout=3)
    assert missing.status_code == 404


def test_artifact_endpoint_serves_screenshots_and_workspace(tmp_path: Path, web_server: WebServer) -> None:
    (tmp_path / "screenshot.png").write_bytes(b"png")
    (tmp_path / "screenshots").mkdir(exist_ok=True)
    (tmp_path / "screenshots" / "desktop.png").write_bytes(b"png")
    (tmp_path / "report.md").write_text("# سلام", encoding="utf-8")
    base = f"http://127.0.0.1:{web_server.port}"

    ok = requests.get(base + "/api/artifact", params={"path": "report.md"}, timeout=3)
    assert ok.status_code == 200
    assert "# سلام" in ok.text

    ok2 = requests.get(base + "/api/artifact", params={"path": "screenshots/desktop.png"}, timeout=3)
    assert ok2.status_code == 200
    assert ok2.content == b"png"

    for escape in ("../../etc/passwd", "/etc/passwd"):
        blocked = requests.get(base + "/api/artifact", params={"path": escape}, timeout=3)
        assert blocked.status_code in {403, 404}

    missing = requests.get(base + "/api/artifact", params={"path": "nope.png"}, timeout=3)
    assert missing.status_code == 404


def test_provider_detect_endpoint_detects_and_lists_models(web_server: WebServer, monkeypatch) -> None:
    import local_agent.llm.client as llm_client

    class _FakeClient:
        def list_models(self):
            return ["gpt-4o-mini", "claude-sonnet-4"]

    monkeypatch.setattr(llm_client, "create_client", lambda settings: _FakeClient())
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(
        base + "/api/provider/detect",
        json={"base_url": "https://api.avalai.ir/v1", "api_key": "sk-test"},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "avalai"
    assert body["valid"] is True
    assert "gpt-4o-mini" in body["models"]


def test_provider_detect_endpoint_handles_missing_credentials(web_server: WebServer) -> None:
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(
        base + "/api/provider/detect",
        json={"base_url": "", "api_key": ""},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["models"] == []


def test_billing_endpoint_returns_not_available_without_cloud_key(web_server: WebServer) -> None:
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.get(base + "/api/billing", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False


def test_settings_endpoint_updates_the_model(web_server: WebServer) -> None:
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/settings",
        json={"provider": "ollama", "model": "llama3.1:8b", "confirm_mode": "always"},
        timeout=10,
    )
    assert r.status_code == 200
    assert r.json()["model"] == "llama3.1:8b"


# ---------------------------------------------------------------------------
# Health check endpoint + settings persistence
# ---------------------------------------------------------------------------


def test_api_doctor_returns_a_report(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/doctor?offline=true", timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "warn", "fail"}
    assert body["results"], "expected at least one check"
    for result in body["results"]:
        assert result["title"], "every check needs a Persian title"
        assert result["status"] in {"ok", "warn", "fail"}


def test_api_status_exposes_persian_warnings(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/status", timeout=3)
    warnings = r.json()["settings"]["warnings"]
    assert isinstance(warnings, list)
    # The default config points at a local Ollama that is not running here.
    assert any("Ollama" in w for w in warnings)


def test_api_settings_persists_to_disk(web_server: WebServer, tmp_path: Path) -> None:
    import json

    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/settings",
        json={
            "provider": "openai_compatible",
            "model": "gpt-4o-mini",
            "openai_base_url": "https://api.avalai.ir/v1",
            "openai_api_key": "sk-persisted",
        },
        timeout=5,
    )
    assert r.status_code == 200
    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["llm"]["openai_base_url"] == "https://api.avalai.ir/v1"
    assert payload["llm"]["openai_api_key"] == "sk-persisted"


# ---------------------------------------------------------------------------
# Full purge endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def purge_server(tmp_path: Path) -> WebServer:
    """A web server whose data dir lives fully inside tmp_path."""
    from tests_local_agent.conftest import _free_port, _wait_for_server

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "assistant.log").write_text("old", encoding="utf-8")
    (data_dir / "bridge.token").write_text("tok", encoding="utf-8")
    settings = AssistantSettings(data_dir=data_dir, work_dir=tmp_path)
    bridge = BridgeServer(settings)
    bridge.start_in_process()
    from local_agent.bridge.api.client import BridgeClient, _InProcessBackend, _welcome_to_info

    backend = _InProcessBackend(bridge)
    backend._started = True
    client = BridgeClient(backend, _welcome_to_info(bridge.welcome()))
    server = WebServer(settings, client, host="127.0.0.1", port=_free_port())
    server.start_in_thread()
    if not _wait_for_server(server):
        server.stop()
        pytest.fail("web server did not start")
    yield server
    server.stop()


def test_api_purge_requires_explicit_confirm(purge_server: WebServer) -> None:
    r = requests.post(f"http://127.0.0.1:{purge_server.port}/api/purge", json={}, timeout=5)
    assert r.status_code == 400
    assert "تأیید" in r.json()["detail"]
    # confirmed=false must not delete anything
    r2 = requests.post(
        f"http://127.0.0.1:{purge_server.port}/api/purge",
        json={"confirm": False, "shutdown": False},
        timeout=5,
    )
    assert r2.status_code == 400
    assert purge_server.settings.data_dir.exists()


def test_api_purge_wipes_data_dir(purge_server: WebServer, tmp_path: Path) -> None:
    data_dir = purge_server.settings.data_dir
    keep = tmp_path / "outside.txt"
    keep.write_text("نباید پاک شود", encoding="utf-8")
    r = requests.post(
        f"http://127.0.0.1:{purge_server.port}/api/purge",
        json={"confirm": True, "shutdown": False, "include_repo_caches": False},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "shutdown_scheduled" not in body  # خاموش‌سازی درخواست نشده بود
    assert "پاک‌سازی کامل انجام شد" in body["message"]
    assert not data_dir.exists(), "کل پوشهٔ داده باید پاک شود"
    assert keep.exists(), "هیچ مسیر بیرونی نباید پاک شود"


# ---------------------------------------------------------------------------
# Artifact endpoint — Windows-style paths + cwd-shadowing regression
# ---------------------------------------------------------------------------


def test_artifact_endpoint_windows_backslash_relative_path(
    tmp_path: Path, web_server: WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production 404: artifacts like ``screenshots\\screen.png``.

    On the desktop app the process cwd *is* the workspace, so the old
    resolver matched the (non-existent) cwd-relative candidate whose
    containment check passed and never reached the real file in the data
    dir — answering 404 for a screenshot that existed.  The bridge also
    used to emit backslashed relative paths on Windows.
    """
    # chdir *into the workspace* to reproduce production (cwd == work_dir).
    monkeypatch.chdir(tmp_path)
    shot = tmp_path / "screenshots" / "screen.png"
    shot.parent.mkdir(exist_ok=True)
    shot.write_bytes(b"REAL-PNG")
    base = f"http://127.0.0.1:{web_server.port}"

    r = requests.get(base + "/api/artifact", params={"path": "screenshots\\screen.png"}, timeout=3)
    assert r.status_code == 200, r.text
    assert r.content == b"REAL-PNG"

    # forward slash form keeps working too
    r2 = requests.get(base + "/api/artifact", params={"path": "screenshots/screen.png"}, timeout=3)
    assert r2.status_code == 200
    assert r2.content == b"REAL-PNG"


def test_artifact_endpoint_prefers_existing_file_over_cwd_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_artifact_path must not stop at the first in-scope target."""
    from local_agent.web.app import resolve_artifact_path

    work = tmp_path / "work"
    data = tmp_path / "data"
    (work / "screenshots").mkdir(parents=True)
    (data / "screenshots").mkdir(parents=True)
    real = data / "screenshots" / "screen.png"
    real.write_bytes(b"real")
    monkeypatch.chdir(work)
    # (work/screenshots exists as a DIRECTORY only — no file shadowing ours)
    resolved = resolve_artifact_path(work, data, "screenshots\\screen.png")
    assert resolved == real
    resolved2 = resolve_artifact_path(work, data, "screenshots/screen.png")
    assert resolved2 == real


def test_artifact_endpoint_missing_file_is_404_not_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from local_agent.web.app import resolve_artifact_path

    work = tmp_path / "work"
    data = tmp_path / "data"
    work.mkdir()
    data.mkdir()
    monkeypatch.chdir(work)
    # In-scope but missing → path returned (endpoint answers 404), not 403.
    resolved = resolve_artifact_path(work, data, "screenshots\\nope.png")
    assert not resolved.exists()
    # Out of scope stays forbidden.
    with pytest.raises(HTTPException) as excinfo:
        resolve_artifact_path(work, data, "..\\..\\etc\\passwd")
    assert excinfo.value.status_code == 403
    with pytest.raises(HTTPException) as excinfo2:
        resolve_artifact_path(work, data, "screenshots/../../../etc/passwd")
    assert excinfo2.value.status_code == 403


# ---------------------------------------------------------------------------
# P0 — global exception handler: no more HTML "Internal Server Error"
# ---------------------------------------------------------------------------


def test_unhandled_exception_returns_clean_persian_json() -> None:
    """Every unhandled exception must become JSON, never an HTML 500 page."""
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from local_agent.web.app import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret-detail-sk-abc should never reach the client")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/boom")
        assert r.status_code == 500
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert isinstance(body.get("detail"), str)
        assert body["detail"], "پیام فارسی باید غیرخالی باشد"
        # The raw exception text, paths and secret-looking values never leak.
        assert "secret-detail-sk-abc" not in r.text
        assert "Traceback" not in r.text
        assert "RuntimeError" not in r.text


def test_validation_error_returns_persian_json() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from local_agent.web.app import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(name: str) -> dict:
        return {"name": name}

    with TestClient(app) as client:
        r = client.post("/echo", json={})
        assert r.status_code == 422
        body = r.json()
        assert "نامعتبر" in body.get("detail", "")


def test_artifact_windows_path_with_cwd_equal_workdir_regression(
    tmp_path: Path, web_server: WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 regression: ``screenshots\\screen.png`` served from data_dir.

    The production layout runs the process with cwd == work_dir.  The
    file exists ONLY under data_dir/screenshots; the old resolver used to
    answer 404 (or 500) for the backslashed path.  ``..`` escapes must
    stay forbidden.
    """
    monkeypatch.chdir(tmp_path)  # cwd == work_dir, like the Windows build
    shot = tmp_path / "screenshots" / "screen.png"
    shot.parent.mkdir(exist_ok=True)
    shot.write_bytes(b"P0-REAL")
    base = f"http://127.0.0.1:{web_server.port}"

    ok = requests.get(base + "/api/artifact", params={"path": "screenshots\\screen.png"}, timeout=3)
    assert ok.status_code == 200, ok.text
    assert ok.content == b"P0-REAL"

    for escape in ("..\\..\\etc\\passwd", "screenshots/../../../etc/passwd"):
        blocked = requests.get(base + "/api/artifact", params={"path": escape}, timeout=3)
        assert blocked.status_code in {403, 404}, f"{escape} باید مسدود شود"


# ---------------------------------------------------------------------------
# P1 — personal Telegram connect flow over HTTP
# ---------------------------------------------------------------------------


def test_telegram_connect_without_credentials_returns_guidance(
    web_server: WebServer,
) -> None:
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(base + "/api/telegram/connect", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "my.telegram.org" in body.get("detail", "")
    assert "config" in body.get("detail", "")


def test_telegram_verify_without_login_flow_returns_400(web_server: WebServer) -> None:
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(base + "/api/telegram/verify", json={"code": "12345"}, timeout=5)
    assert r.status_code == 400


def test_telegram_full_login_flow_over_http(
    web_server: WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """await_code -> await_2fa -> connected, driven purely over HTTP."""
    import sys

    import telethon

    # Persist credentials through the settings endpoint first.
    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(
        base + "/api/settings",
        json={
            "telegram": {
                "enabled": True,
                "api_id": 12345,
                "api_hash": "h" * 32,
                "phone": "+10000000000",
            }
        },
        timeout=5,
    )
    assert r.status_code == 200

    # --- fake telethon client with 2FA -------------------------------
    class _FakeUser:
        id = 1
        first_name = "Test"
        username = "tester"
        last_name = ""
        phone = "+10000000000"

    class _FakeTele:
        def __init__(self, *args, **kwargs) -> None:
            self.authorized = False
            self.password_prompted = False

        async def connect(self) -> None:
            pass

        async def is_user_authorized(self) -> bool:
            return self.authorized

        async def send_code_request(self, phone: str) -> Any:
            return type("Sent", (), {"phone_code_hash": "hash"})()

        async def sign_in(self, *args, **kwargs) -> _FakeUser:
            if not kwargs.get("password"):
                from telethon.errors import SessionPasswordNeededError

                self.password_prompted = True
                raise SessionPasswordNeededError(request=None)
            self.authorized = True
            return _FakeUser()

        async def get_me(self) -> _FakeUser:
            return _FakeUser()

        async def disconnect(self) -> None:
            pass

    telethon_module = sys.modules.get("telethon") or telethon
    monkeypatch.setattr(telethon_module, "TelegramClient", _FakeTele)

    # --- state machine ------------------------------------------------
    r = requests.post(base + "/api/telegram/connect", timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "await_code"

    r = requests.post(base + "/api/telegram/verify", json={"code": "12345"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "await_2fa"

    r = requests.post(base + "/api/telegram/verify", json={"password": "p4ss"}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "connected"
    assert body["connected"] is True

    # Status now reports the connected client.
    status = requests.get(base + "/api/status", timeout=5).json()
    s = status["settings"]["settings"]
    assert s["telegram_connected"] is True
    assert s["telegram_state"] == "connected"

    # Disconnect resets it.
    r = requests.post(base + "/api/telegram/disconnect", timeout=5)
    assert r.status_code == 200
    assert r.json()["connected"] is False
