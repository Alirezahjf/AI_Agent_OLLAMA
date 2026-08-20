"""Protected Web/Desktop GitHub route tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.bridge import BridgeClient
from local_agent.core.config import AssistantSettings
from local_agent.core.errors import AssistantError
from local_agent.web.app import create_app


@pytest.fixture
def github_web(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    settings = AssistantSettings(data_dir=tmp_path / "data", work_dir=tmp_path)
    client = BridgeClient.start_in_process(settings)
    service = client._backend._server.handlers.context.extra["github"]  # type: ignore[attr-defined]
    app = create_app(client, settings)
    with TestClient(app, base_url="http://testserver") as browser:
        yield browser, client, service
    client._backend.stop()  # type: ignore[attr-defined]


def security(browser):
    browser.get("/")
    response = browser.post(
        "/api/github/security", headers={"Origin": "http://testserver"}, json={}
    )
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert token
    return {"Origin": "http://testserver", "X-CSRF-Token": token}


def test_remote_web_requires_https_bearer_bootstrap_and_cookie(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    settings = AssistantSettings(data_dir=tmp_path / "data", work_dir=tmp_path)
    client = BridgeClient.start_in_process(settings)
    token = "remote-" + "x" * 40
    app = create_app(client, settings, remote_access_token=token)
    try:
        with TestClient(app, base_url="https://assistant.example") as browser:
            login = browser.get("/")
            assert login.status_code == 200
            assert "توکن Bridge" in login.text
            assert "index.html" not in login.text
            assert login.headers["cache-control"] == "no-store"
            assert browser.get("/api/status").status_code == 401
            assert (
                browser.post(
                    "/api/auth/bootstrap",
                    headers={"Authorization": "Bearer " + "z" * 40},
                ).status_code
                == 401
            )
            accepted = browser.post(
                "/api/auth/bootstrap", headers={"Authorization": f"Bearer {token}"}
            )
            assert accepted.status_code == 204
            cookie = accepted.headers["set-cookie"]
            assert "pla_remote_auth=" in cookie
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
            assert browser.get("/api/status").status_code == 200

        with TestClient(app, base_url="http://assistant.example") as plaintext:
            rejected = plaintext.get("/api/status", headers={"Authorization": f"Bearer {token}"})
            assert rejected.status_code == 426
    finally:
        client._backend.stop()  # type: ignore[attr-defined]


def test_remote_web_rejects_weak_access_token(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path / "data", work_dir=tmp_path)
    client = BridgeClient.start_in_process(settings)
    try:
        with pytest.raises(AssistantError, match="۳۲"):
            create_app(client, settings, remote_access_token="weak")
    finally:
        client._backend.stop()  # type: ignore[attr-defined]


def test_websocket_rejects_cross_origin_browser(github_web) -> None:
    from starlette.websockets import WebSocketDisconnect

    browser, _client, _service = github_web
    with (
        pytest.raises(WebSocketDisconnect) as raised,
        browser.websocket_connect("/ws", headers={"Origin": "https://evil.example"}),
    ):
        pass
    assert raised.value.code == 1008


def test_github_routes_require_exact_origin_and_csrf(github_web) -> None:
    browser, _client, service = github_web
    assert browser.post("/api/github/security", json={}).status_code == 403
    assert (
        browser.post(
            "/api/github/security",
            headers={"Origin": "https://evil.example"},
            json={},
        ).status_code
        == 403
    )
    headers = security(browser)

    # Status is a bootstrap/read endpoint: signed session + Origin are enough.
    status = browser.post("/api/github/status", headers={"Origin": "http://testserver"})
    assert status.status_code == 200
    assert "configuration" in status.json()
    assert "access_token" not in status.text

    service.read = lambda operation, params: {"operation": operation, "params": params}
    payload = {"operation": "repositories", "params": {"limit": 5}}
    assert (
        browser.post(
            "/api/github/read",
            headers={"Origin": "http://testserver"},
            json=payload,
        ).status_code
        == 403
    )
    assert (
        browser.post(
            "/api/github/read",
            headers={**headers, "X-CSRF-Token": "wrong"},
            json=payload,
        ).status_code
        == 403
    )
    accepted = browser.post("/api/github/read", headers=headers, json=payload)
    assert accepted.status_code == 200
    assert accepted.json()["operation"] == "repositories"


def test_forwarded_host_cannot_redefine_github_origin(github_web) -> None:
    browser, _client, _service = github_web
    browser.get("/")
    spoofed = {"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example"}
    assert browser.post("/api/github/security", headers=spoofed, json={}).status_code == 403

    accepted = browser.post(
        "/api/github/security",
        headers={"Origin": "http://testserver", "X-Forwarded-Host": "evil.example"},
        json={},
    )
    assert accepted.status_code == 200
    # Untrusted forwarded protocol headers must not rewrite callback/cookie security.
    assert (
        browser.post(
            "/api/github/security",
            headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
            json={},
        ).status_code
        == 403
    )


def test_github_settings_are_csrf_protected_and_allowlisted(github_web) -> None:
    browser, _client, service = github_web
    service.vault = SimpleNamespace(load=lambda: None)
    headers = security(browser)
    payload = {
        "github": {
            "enabled": False,
            "client_id": "Iv1.public",
            "broker_url": "https://broker.example",
            "unexpected_secret": "must-not-be-accepted",
        }
    }
    rejected = browser.post("/api/settings", headers=headers, json=payload)
    assert rejected.status_code == 400
    assert "ناشناخته" in rejected.text
    assert "must-not-be-accepted" not in rejected.text

    payload["github"].pop("unexpected_secret")
    saved = browser.post("/api/settings", headers=headers, json=payload)
    assert saved.status_code == 200
    assert saved.json()["saved"]["github_enabled"] is False
    assert "client_secret" not in saved.text


def test_github_write_requires_explicit_confirmation(github_web) -> None:
    browser, _client, service = github_web
    headers = security(browser)
    calls: list[tuple[str, dict]] = []
    service.write = lambda operation, params: calls.append((operation, params)) or {"ok": True}
    payload = {
        "operation": "issue_create",
        "params": {"owner": "owner", "repo": "repo", "title": "title"},
    }
    response = browser.post("/api/github/write", headers=headers, json=payload)
    assert response.status_code == 409
    assert calls == []
    response = browser.post(
        "/api/github/write",
        headers=headers,
        json={**payload, "confirm": True},
    )
    assert response.status_code == 200
    assert calls == [("issue_create", payload["params"])]


def test_oauth_start_binds_browser_session_and_origin(github_web) -> None:
    browser, _client, service = github_web
    headers = security(browser)
    captured: dict = {}

    def start_oauth(**kwargs):
        captured.update(kwargs)
        return "https://github.com/login/oauth/authorize?state=opaque"

    service.oauth.start = start_oauth
    response = browser.post("/api/github/oauth/start", headers=headers, json={})
    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://github.com/")
    assert captured["browser_session"]
    assert captured["origin"] == "http://testserver"
    assert captured["redirect_uri"] == "http://testserver/api/github/oauth/callback"
    assert captured["browser_session"] not in response.text


def test_callback_targets_stored_origin_and_has_strict_csp(github_web) -> None:
    browser, _client, service = github_web
    browser.get("/")
    service.complete_oauth = lambda **_kwargs: "http://testserver"
    response = browser.get("/api/github/oauth/callback?state=state&code=code")
    assert response.status_code == 200
    assert "postMessage" in response.text
    assert '"http://testserver"' in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["cache-control"] == "no-store"


def test_purge_attempts_github_revocation_before_cleanup(
    github_web,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, _client, service = github_web
    headers = security(browser)
    order: list[str] = []
    service.vault = SimpleNamespace(available=True, delete=lambda: None)
    service.disconnect = lambda revoke=True: order.append(f"disconnect:{revoke}")

    def purge_all(_settings, *, include_repo_caches=True, close_logging=False):
        order.append(f"cleanup:{include_repo_caches}:{close_logging}")
        return {"ok": True, "removed": []}

    monkeypatch.setattr("local_agent.core.cleanup.purge_all", purge_all)
    response = browser.post(
        "/api/purge",
        headers=headers,
        json={"confirm": True, "include_repo_caches": True, "shutdown": False},
    )
    assert response.status_code == 200
    assert order == ["disconnect:True", "cleanup:True:True"]


def test_purge_continues_if_remote_revocation_fails(
    github_web,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, _client, service = github_web
    headers = security(browser)
    service.vault = SimpleNamespace(available=True, delete=lambda: None)
    service.disconnect = lambda revoke=True: (_ for _ in ()).throw(AssistantError("offline"))
    monkeypatch.setattr(
        "local_agent.core.cleanup.purge_all",
        lambda *_args, **_kwargs: {"ok": True, "removed": []},
    )
    response = browser.post(
        "/api/purge",
        headers=headers,
        json={"confirm": True, "shutdown": False},
    )
    assert response.status_code == 200
    assert response.json()["revocation_warning"]


def test_download_is_no_store_and_sanitizes_filename(github_web) -> None:
    browser, _client, service = github_web
    headers = security(browser)
    service.download = lambda operation, params: (b"zip", "../../token.zip", "application/zip")
    response = browser.post(
        "/api/github/download",
        headers=headers,
        json={"operation": "artifact", "params": {"artifact_id": 1}},
    )
    assert response.status_code == 200
    assert response.content == b"zip"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert 'filename="_.._token.zip"' in disposition
    assert "/" not in disposition
