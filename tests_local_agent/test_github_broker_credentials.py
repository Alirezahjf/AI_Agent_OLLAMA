"""OAuth broker and OS-vault isolation tests."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from local_agent.core.errors import AssistantError
from local_agent.github.broker import _RateLimiter, create_broker_app
from local_agent.github.credentials import CredentialVault, TokenBundle


def test_vault_has_no_plaintext_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = ModuleType("keyring")
    keyring.get_keyring = lambda: SimpleNamespace(priority=0)
    errors = ModuleType("keyring.errors")

    class KeyringError(Exception):
        pass

    errors.KeyringError = KeyringError
    errors.PasswordDeleteError = type("PasswordDeleteError", (Exception,), {})
    keyring.errors = errors
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    vault = CredentialVault()
    assert vault.available is False
    with pytest.raises(AssistantError):
        vault.save(TokenBundle(access_token="must-not-be-written"))


def test_vault_roundtrip_uses_keyring_only(monkeypatch: pytest.MonkeyPatch) -> None:
    values: dict[tuple[str, str], str] = {}
    keyring = ModuleType("keyring")
    keyring.get_keyring = lambda: SimpleNamespace(priority=10)
    keyring.get_password = lambda service, account: values.get((service, account))
    keyring.set_password = lambda service, account, value: values.__setitem__(
        (service, account), value
    )
    keyring.delete_password = lambda service, account: values.pop((service, account), None)
    errors = ModuleType("keyring.errors")
    errors.KeyringError = type("KeyringError", (Exception,), {})
    errors.PasswordDeleteError = type("PasswordDeleteError", (Exception,), {})
    keyring.errors = errors
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)

    vault = CredentialVault()
    token = TokenBundle(access_token="access", refresh_token="refresh", client_id="client")
    vault.save(token)
    assert values
    assert vault.load() == token
    credential_key = next(iter(values))
    for malformed in ("[]", '{"access_token":"line\\nbreak"}', '{"access_token":""}'):
        values[credential_key] = malformed
        assert vault.load() is None
        assert credential_key not in values
    vault.delete()
    assert vault.load() is None


def test_vault_delete_ignores_only_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = ModuleType("keyring")
    keyring.get_keyring = lambda: SimpleNamespace(priority=10)
    errors = ModuleType("keyring.errors")
    errors.KeyringError = type("KeyringError", (Exception,), {})
    missing_error = type("PasswordDeleteError", (Exception,), {})
    errors.PasswordDeleteError = missing_error
    keyring.errors = errors
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)

    keyring.delete_password = lambda *_args: (_ for _ in ()).throw(missing_error("missing"))
    CredentialVault().delete()

    backend_error = type("BackendUnavailable", (Exception,), {})
    keyring.delete_password = lambda *_args: (_ for _ in ()).throw(backend_error("failed"))
    with pytest.raises(AssistantError, match="BackendUnavailable"):
        CredentialVault().delete()


def broker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "broker-secret")
    monkeypatch.setenv(
        "GITHUB_CALLBACK_URLS",
        "https://app.example/api/github/oauth/callback,http://localhost/callback",
    )


def test_broker_requires_secret_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_CALLBACK_URLS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        create_broker_app()


def test_broker_rate_limiter_bounds_peer_table_and_fails_closed() -> None:
    limiter = _RateLimiter(limit=2, window=60, max_peers=2)
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-b") is True
    assert len(limiter._hits) == 2
    assert limiter.allow("peer-c") is False
    assert len(limiter._hits) == 2
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-a") is False


def test_broker_rejects_actual_oversized_body_without_declared_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    broker_env(monkeypatch)
    with TestClient(create_broker_app()) as browser:
        response = browser.post(
            "/exchange",
            content=(chunk for chunk in (b"{" + b"x" * 20_000, b"y" * 20_000 + b"}")),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_broker_rejects_insecure_or_malformed_callback_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "broker-secret")
    for callback in (
        "http://public.example/callback",
        "https://user:password@app.example/callback",
        "https://app.example:bad/callback",
        "https://app.example/callback;parameter",
        "https://app.example/callback?next=https://evil.example",
        "https://app.example/callback#fragment",
        "javascript:alert(1)",
    ):
        monkeypatch.setenv("GITHUB_CALLBACK_URLS", callback)
        with pytest.raises(RuntimeError):
            create_broker_app()

    monkeypatch.setenv("GITHUB_CALLBACK_URLS", "https://app.example/callback")
    for variable in ("GITHUB_WEB_URL", "GITHUB_API_URL"):
        monkeypatch.setenv(variable, "http://public.example")
        with pytest.raises(RuntimeError):
            create_broker_app()
        monkeypatch.delenv(variable)


def test_broker_enforces_callback_pkce_and_does_not_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    broker_env(monkeypatch)
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.enterprise.example")
    calls: list[dict] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("local_agent.github.broker.requests.post", post)
    with TestClient(create_broker_app()) as browser:
        invalid = browser.post(
            "/exchange",
            json={
                "client_id": "Iv1.client",
                "code": "code",
                "code_verifier": "v" * 43,
                "redirect_uri": "https://evil.example/callback",
            },
        )
        assert invalid.status_code == 400
        assert calls == []
        short = browser.post(
            "/exchange",
            json={
                "client_id": "Iv1.client",
                "code": "code",
                "code_verifier": "short",
                "redirect_uri": "https://app.example/api/github/oauth/callback",
            },
        )
        assert short.status_code == 400

        accepted = browser.post(
            "/exchange",
            json={
                "client_id": "Iv1.client",
                "code": "code",
                "code_verifier": "v" * 64,
                "redirect_uri": "https://app.example/api/github/oauth/callback",
            },
        )
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json()["access_token"] == "access"
        assert calls[0]["url"] == "https://github.enterprise.example/login/oauth/access_token"
        assert calls[0]["data"]["client_secret"] == "broker-secret"
        assert calls[0]["data"]["code_verifier"] == "v" * 64
        assert calls[0]["allow_redirects"] is False
        assert "broker-secret" not in accepted.text


def test_broker_revoke_uses_github_application_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    broker_env(monkeypatch)
    monkeypatch.setenv("GITHUB_API_URL", "https://github.enterprise.example/api/v3")
    calls: list[dict] = []

    def delete(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=204)

    monkeypatch.setattr("local_agent.github.broker.requests.delete", delete)
    with TestClient(create_broker_app()) as browser:
        response = browser.post(
            "/revoke",
            json={
                "client_id": "Iv1.client",
                "access_token": "access",
            },
        )
    assert response.status_code == 200
    assert calls[0]["url"] == (
        "https://github.enterprise.example/api/v3/applications/Iv1.client/token"
    )
    assert calls[0]["auth"] == ("Iv1.client", "broker-secret")
    assert calls[0]["json"] == {"access_token": "access"}
    assert calls[0]["allow_redirects"] is False
    assert "access" not in json.dumps(response.json())
