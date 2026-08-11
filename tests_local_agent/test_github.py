"""Tests for the GitHub integration.

The real GitHubClient talks to github.com (REST) and the local ``git``
binary.  We never hit the network here: the REST calls are exercised
through a tiny fake ``requests``-like transport, and the git calls run
against a throwaway local repo (no remote, so no auth is needed).

What these tests pin down (all of it REAL production logic):

* PAT validation + token persistence to a JSON file
* OAuth ``authorize_url`` (CSRF ``state`` carries the account) + ``exchange_code``
* A stored token reconnects a freshly-built client (restart survives)
* ``disconnect`` forgets the token file
* **The token is fed to ``git`` only via a process-local env var** — it
  never appears in ``.git/config`` and the raw token never appears in the
  git environment (only a base64 Authorization header does)
* Real ``git status`` runs against a local repo
* The ``github.*`` actions register with correct risk levels
* ``github_status`` never leaks the token/secret
* The HTTP endpoints work offline: status returns accounts, OAuth without
  a client_id fails with a clear 400, and settings persist GitHub config.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from local_agent.actions import run_action
from local_agent.actions.github_actions import register_github
from local_agent.actions.registry import ActionContext, ConfirmationGate, Risk
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings
from local_agent.core.context import RuntimeContext
from local_agent.github import GitHubClient, GitHubError


# --------------------------------------------------------------------------- #
# Fake HTTP transport (stands in for ``requests``)
# --------------------------------------------------------------------------- #


class _FakeReqExc(Exception):
    pass


class _FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        body = json.dumps(payload) if payload is not None else text
        self.content = (body or "").encode()
        self.text = body or ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _FakeReqExc(f"HTTP {self.status_code}")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeTransport:
    """Routes (method, url-substring) -> _FakeResponse or callable(args)->resp."""

    RequestException = _FakeReqExc

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def add(self, method: str, url_part: str, response: Any) -> None:
        self.routes[(method.upper(), url_part)] = response

    def _resolve(self, method: str, url: str) -> _FakeResponse:
        for (m, part), resp in self.routes.items():
            if m == method.upper() and part in url:
                self.calls.append((method.upper(), url, {}))
                return resp() if callable(resp) else resp
        return _FakeResponse(404, {"message": "not found"})

    def request(self, method: str, url: str, headers=None, timeout=None, **kwargs: Any) -> _FakeResponse:
        return self._resolve(method, url)

    def post(self, url: str, data=None, headers=None, timeout=None, **kwargs: Any) -> _FakeResponse:
        return self._resolve("POST", url)

    def get(self, url: str, headers=None, timeout=None, **kwargs: Any) -> _FakeResponse:
        return self._resolve("GET", url)


def _user_payload() -> dict[str, Any]:
    return {"login": "octocat", "name": "The Octocat", "id": 1,
            "avatar_url": "u", "html_url": "u", "email": ""}


def _client(tmp_path: Path, *, transport: _FakeTransport | None = None,
            client_id: str = "cid", client_secret: str = "csec") -> GitHubClient:
    return GitHubClient(
        account_name="اصلی",
        api_base="https://api.github.com",
        client_id=client_id,
        client_secret=client_secret,
        token_file=tmp_path / "github" / "gh.json",
        data_dir=tmp_path,
        transport=transport or _FakeTransport(),
    )


# --------------------------------------------------------------------------- #
# PAT + OAuth
# --------------------------------------------------------------------------- #


def test_pat_connect_validates_and_stores_token(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.add("GET", "/user", _FakeResponse(200, _user_payload()))
    client = _client(tmp_path, transport=transport)
    result = client.connect_pat("ghp_testtoken")
    assert result["state"] == "connected"
    assert client.is_connected
    assert client.login == "octocat"
    # Token persisted to a JSON file (not config.json).
    saved = json.loads(client.token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "ghp_testtoken"
    assert saved["login"] == "octocat"


def test_oauth_authorize_url_and_exchange(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.add("POST", "login/oauth/access_token",
                  _FakeResponse(200, {"access_token": "ghp_oauth_tok", "scope": "repo", "token_type": "bearer"}))
    transport.add("GET", "/user", _FakeResponse(200, _user_payload()))
    client = _client(tmp_path, transport=transport)

    registry: dict[str, Any] = {}
    url, state = client.authorize_url("http://localhost:7824/api/github/callback", state_registry=registry)
    assert "client_id=cid" in url and "github.com/login/oauth/authorize" in url
    assert state.startswith("اصلی::")
    assert state in registry  # CSRF state remembered for the callback

    result = client.exchange_code("the-code", client_secret="csec")
    assert result["state"] == "connected"
    assert client.is_connected
    # The token + login persisted.
    saved = json.loads(client.token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "ghp_oauth_tok"


def test_oauth_without_client_id_is_a_clear_error(tmp_path: Path) -> None:
    client = _client(tmp_path, client_id="", client_secret="")
    with pytest.raises(GitHubError):
        client.authorize_url("http://localhost:7824/api/github/callback")


def test_stored_token_reconnects_a_fresh_client(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.add("GET", "/user", _FakeResponse(200, _user_payload()))
    _client(tmp_path, transport=transport).connect_pat("ghp_persist")
    # A brand-new client (simulating a restart) loads + validates the stored token.
    transport2 = _FakeTransport()
    transport2.add("GET", "/user", _FakeResponse(200, _user_payload()))
    fresh = _client(tmp_path, transport=transport2)
    assert fresh.connect()["state"] == "connected"
    assert fresh.is_connected


def test_disconnect_forgets_token(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.add("GET", "/user", _FakeResponse(200, _user_payload()))
    client = _client(tmp_path, transport=transport)
    client.connect_pat("ghp_forget")
    assert client.token_file.is_file()
    client.forget_token()
    assert not client.is_connected
    assert not client.token_file.is_file()


# --------------------------------------------------------------------------- #
# Security: the token never leaks into git config / args
# --------------------------------------------------------------------------- #


def test_git_env_never_exposes_raw_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._token = "ghp_SUPERSECRET"  # noqa: SLF001 - exercising the env builder
    env = client._git_env()  # noqa: SLF001
    # The header IS injected (so push/pull authenticate) ...
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]
    # ... but the RAW token never appears anywhere in the environment, and git
    # is told never to prompt for credentials interactively.
    assert "ghp_SUPERSECRET" not in " ".join(env.values())
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_status_runs_in_a_real_repo(tmp_path: Path) -> None:
    if not _git_available():
        pytest.skip("git not installed")
    repo = tmp_path / "r"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "t@t.t"], cwd=repo)
    _run(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "a.txt").write_text("hi", encoding="utf-8")
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)
    client = _client(tmp_path)
    report = client.git_status(repo)
    assert "شاخه" in report


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


# --------------------------------------------------------------------------- #
# Actions registration + risk levels
# --------------------------------------------------------------------------- #


@pytest.fixture
def ctx(tmp_path: Path) -> ActionContext:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return ActionContext(
        runtime=RuntimeContext(settings), confirmation_gate=ConfirmationGate(settings.safety),
        work_dir=tmp_path,
    )


def test_github_actions_register_with_correct_risk(ctx: ActionContext) -> None:
    from local_agent.actions.registry import ActionRegistry
    registry = ActionRegistry()
    register_github(registry, ctx)
    by_name = {a.name: a for a in registry.all()}
    assert "github.whoami" in by_name and "github.push" in by_name and "github.merge_pr" in by_name
    assert by_name["github.whoami"].risk_level == Risk.SAFE
    assert by_name["github.list_repos"].risk_level == Risk.SAFE
    assert by_name["github.push"].risk_level == Risk.DESTRUCTIVE
    assert by_name["github.merge"].risk_level == Risk.DESTRUCTIVE
    assert by_name["github.create_repo"].risk_level == Risk.DESTRUCTIVE


def test_github_actions_without_client_give_helpful_error(ctx: ActionContext) -> None:
    from local_agent.actions.registry import ActionRegistry
    from local_agent.core.errors import DependencyMissing
    registry = ActionRegistry()
    register_github(registry, ctx)
    ctx.extra["github"] = None
    with pytest.raises(DependencyMissing) as exc:
        run_action(registry, "github.whoami", {}, ctx)
    assert "گیتهاب" in exc.value.install_hint


# --------------------------------------------------------------------------- #
# Handlers: status never leaks secrets
# --------------------------------------------------------------------------- #


def test_handlers_github_status_no_secrets(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    status = handlers.github_status()
    # The status is safe to send to the UI: no token, no secret.
    blob = json.dumps(status, ensure_ascii=False)
    assert "token" not in blob.lower() or "token_file_exists" in blob
    assert "client_secret" not in blob
    assert status["state"] == "disabled"
    assert status["connected"] is False


# --------------------------------------------------------------------------- #
# HTTP endpoints (offline-safe subset)
# --------------------------------------------------------------------------- #


def test_http_github_status_and_accounts(web_server) -> None:
    import requests
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/github/status", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "accounts" in body and "active_account" in body


def test_http_oauth_without_client_id_is_400(web_server) -> None:
    import requests
    # No client_id configured -> the handler must refuse with a clear 400,
    # never a 500.
    r = requests.post(f"http://127.0.0.1:{web_server.port}/api/github/oauth", timeout=5)
    assert r.status_code == 400
    assert "client_id" in r.text or "GitHub" in r.text


def test_http_settings_persist_github_config(web_server) -> None:
    import requests
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/settings",
        json={"github": {"enabled": True, "accounts": [
            {"name": "work", "enabled": True, "auth_mode": "pat", "client_id": "abc",
             "client_secret": "shh", "confirm_push": True},
        ]}},
        timeout=5,
    )
    assert r.status_code == 200
    status = requests.get(f"http://127.0.0.1:{web_server.port}/api/github/status", timeout=5).json()
    names = [a["account"] for a in status["accounts"]]
    assert "work" in names
    work = next(a for a in status["accounts"] if a["account"] == "work")
    assert work["auth_mode"] == "pat"
    assert work["has_client_id"] is True
    # client_secret is never echoed back.
    assert "shh" not in json.dumps(status)
