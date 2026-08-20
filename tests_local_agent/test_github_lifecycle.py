"""GitHub token refresh and revocation lifecycle tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from local_agent.core.config import GitHubSettings
from local_agent.core.errors import AssistantError
from local_agent.github.credentials import TokenBundle, credential_binding
from local_agent.github.oauth import GitHubOAuth
from local_agent.github.service import GitHubService


class Vault:
    available = True
    error = ""

    def __init__(self, token: TokenBundle | None) -> None:
        self.token = token
        self.saved: list[TokenBundle] = []
        self.deleted = 0

    def load(self):
        return self.token

    def save(self, token):
        self.token = token
        self.saved.append(token)

    def delete(self):
        self.token = None
        self.deleted += 1


def service(tmp_path: Path, token: TokenBundle | None):
    settings = GitHubSettings(
        enabled=True,
        client_id="Iv1.client",
        broker_url="https://broker.example",
        selected_repositories=("owner/repo",),
    )
    if token is not None:
        token = replace(
            token,
            binding=credential_binding(
                client_id=settings.client_id,
                broker_url=settings.broker_url,
                api_url=settings.api_url,
                web_url=settings.web_url,
                graphql_url=settings.graphql_url,
            ),
        )
    vault = Vault(token)
    oauth = GitHubOAuth(settings, vault)
    result = GitHubService(settings, default_clone_root=tmp_path / "clones")
    result.vault = vault
    result.oauth = oauth
    return result, vault, oauth


def test_access_token_refreshes_and_persists_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = TokenBundle(
        access_token="expired",
        refresh_token="refresh",
        expires_at="2000-01-01T00:00:00+00:00",
        client_id="Iv1.client",
    )
    github, vault, oauth = service(tmp_path, old)
    calls: list[tuple[str, dict[str, str]]] = []

    def broker(operation: str, body: dict[str, str]):
        calls.append((operation, body))
        return {"access_token": "new-access", "token_type": "bearer", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_broker", broker)
    assert github.access_token() == "new-access"
    assert calls == [("refresh", {"refresh_token": "refresh"})]
    assert vault.saved[0].access_token == "new-access"
    assert vault.saved[0].refresh_token == "refresh"


def test_expired_token_without_refresh_is_deleted(tmp_path: Path) -> None:
    token = TokenBundle(
        access_token="expired",
        expires_at="2000-01-01T00:00:00+00:00",
        client_id="Iv1.client",
    )
    github, vault, _oauth = service(tmp_path, token)
    with pytest.raises(AssistantError, match="منقضی"):
        github.access_token()
    assert vault.deleted == 1
    assert vault.token is None


def test_failed_post_oauth_account_verification_deletes_new_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, vault, oauth = service(tmp_path, None)
    exchanged = TokenBundle(
        access_token="invalid",
        client_id="Iv1.client",
        binding=github._credential_binding(),
    )

    def complete(**_kwargs):
        vault.save(exchanged)
        return exchanged, "https://app.example"

    calls: list[tuple[str, dict[str, str]]] = []

    def broker(operation: str, body: dict[str, str]):
        calls.append((operation, body))
        return {"ok": True}

    monkeypatch.setattr(oauth, "complete", complete)
    monkeypatch.setattr(oauth, "_broker", broker)
    monkeypatch.setattr(
        github,
        "account",
        lambda **_kwargs: (_ for _ in ()).throw(AssistantError("invalid credential")),
    )
    with pytest.raises(AssistantError, match="invalid credential"):
        github.complete_oauth(state="state", code="code", browser_session="session")
    assert calls == [("revoke", {"access_token": "invalid"})]
    assert vault.deleted == 1
    assert vault.token is None


def test_disconnect_revokes_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TokenBundle(access_token="access", refresh_token="refresh", client_id="Iv1.client")
    github, vault, oauth = service(tmp_path, token)
    calls: list[tuple[str, dict[str, str], bool]] = []

    def broker(operation: str, body: dict[str, str]):
        calls.append((operation, body, vault.token is not None))
        return {"ok": True}

    monkeypatch.setattr(oauth, "_broker", broker)
    github.disconnect()
    assert calls == [("revoke", {"access_token": "access"}, True)]
    assert vault.deleted == 1
    assert vault.token is None


def test_disconnect_deletes_local_token_even_if_revoke_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TokenBundle(access_token="access", client_id="Iv1.client")
    github, vault, oauth = service(tmp_path, token)

    def fail(*_args, **_kwargs):
        raise AssistantError("broker unavailable")

    monkeypatch.setattr(oauth, "_broker", fail)
    with pytest.raises(AssistantError, match="broker unavailable"):
        github.disconnect()
    assert vault.deleted == 1
    assert vault.token is None
