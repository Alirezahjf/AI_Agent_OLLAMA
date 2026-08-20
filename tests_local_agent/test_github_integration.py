"""Focused security and behavior tests for the GitHub integration."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from local_agent.actions.github_actions import register_github
from local_agent.actions.registry import ActionContext, ActionRegistry, Risk, _summarize_arguments
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings, GitHubSettings, _apply_env_overrides
from local_agent.core.errors import AssistantError, ConfigError
from local_agent.github.client import GitHubAPIError, GitHubClient
from local_agent.github.credentials import TokenBundle, credential_binding
from local_agent.github.git import LocalGit, _redact_url, _write_askpass
from local_agent.github.oauth import GitHubOAuth
from local_agent.github.service import GitHubService


class MemoryVault:
    available = True
    error = ""

    def __init__(self) -> None:
        self.token: TokenBundle | None = None
        self.deleted = False

    def require(self):
        return self

    def load(self) -> TokenBundle | None:
        return self.token

    def save(self, token: TokenBundle) -> None:
        self.token = token

    def delete(self) -> None:
        self.token = None
        self.deleted = True


def github_settings(**changes) -> GitHubSettings:
    values = {
        "enabled": True,
        "client_id": "Iv1.test",
        "broker_url": "https://broker.example",
        "callback_url": "https://app.example/api/github/oauth/callback",
        "selected_repositories": ("owner/repo",),
    }
    values.update(changes)
    return GitHubSettings(**values)


def test_github_config_roundtrip_and_environment_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_GITHUB__ENABLED", "true")
    monkeypatch.setenv("LOCAL_AGENT_GITHUB__CLIENT_ID", "Iv1.env")
    monkeypatch.setenv("LOCAL_AGENT_GITHUB__BROKER_URL", "https://broker.example")
    monkeypatch.setenv("LOCAL_AGENT_GITHUB__SELECTED_REPOSITORIES", '["Owner/Repo"]')
    monkeypatch.setenv("LOCAL_AGENT_GITHUB__ALLOWED_ORIGINS", '["https://app.example"]')
    payload = _apply_env_overrides({})
    settings = AssistantSettings.from_dict(payload)
    assert settings.github.enabled is True
    assert settings.github.selected_repositories == ("Owner/Repo",)
    assert settings.github.allowed_origins == ("https://app.example",)
    assert AssistantSettings.from_dict(settings.to_dict()).github == settings.github


@pytest.mark.parametrize(
    "changes",
    [
        {"client_id": ""},
        {"client_id": " Iv1.client "},
        {"client_id": "Iv1.client\nvalue"},
        {"client_id": "x" * 256},
        {"broker_url": "http://broker.example"},
        {"callback_url": "http://app.example/api/github/oauth/callback"},
        {"api_url": "https://user:password@api.github.com"},
        {"web_url": "https://github.com/#fragment"},
        {"broker_url": "https://broker.example/exchange?redirect=https://evil.example"},
        {"graphql_url": "https://api.github.com:invalid/graphql"},
        {"allowed_origins": ("http://app.example",)},
        {"allowed_origins": ("https://app.example/path",)},
        {"allowed_origins": ("https://APP.example",)},
        {"allowed_origins": ("https://app.example:443",)},
        {"selected_repositories": ("not-a-full-name",)},
        {"selected_repositories": ("../repo",)},
        {"selected_repositories": ("owner/..",)},
        {"selected_repositories": ("owner/repo%2Fissues",)},
        {"selected_repositories": ("owner/repo", "OWNER/REPO")},
        {"selected_repositories": tuple(f"owner/repo-{index}" for index in range(1_001))},
        {"allowed_origins": ("https://app.example", "https://app.example")},
        {"enabled": "false"},
        {"local_clone_root": "bad\x00path"},
        {"api_url": "file:///tmp/github"},
        {"api_url": "https://api.example/" + "x" * 2048},
    ],
)
def test_github_config_rejects_unsafe_values(changes: dict) -> None:
    with pytest.raises(ConfigError):
        github_settings(**changes).validate()


def test_github_config_allows_canonical_loopback_development_urls() -> None:
    github_settings(
        broker_url="http://127.0.0.1:8787",
        callback_url="http://localhost:8765/api/github/oauth/callback",
        allowed_origins=("http://localhost:8765",),
    ).validate()


def test_token_bundle_serialization_expiry_and_repr_redaction() -> None:
    token = TokenBundle.from_oauth(
        {
            "access_token": "ghu_access_must_never_appear",
            "expires_in": 30,
            "refresh_token": "ghr_refresh_must_never_appear",
        },
        client_id="client",
    )
    assert TokenBundle.from_json(token.to_json()) == token
    assert token.expires_within(90)
    future = TokenBundle(
        access_token="token",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    assert not future.expires_within(90)
    rendered = repr(token)
    assert "ghu_access_must_never_appear" not in rendered
    assert "ghr_refresh_must_never_appear" not in rendered
    assert "client" in rendered
    assert TokenBundle(access_token="x", expires_at="malformed").expires_within(90)
    for invalid in ("", " leading", "trailing ", "line\nbreak", "x" * 16_385):
        with pytest.raises((AssistantError, ValueError)):
            TokenBundle.from_oauth({"access_token": invalid}, client_id="client")
        with pytest.raises((TypeError, ValueError)):
            TokenBundle.from_json(json.dumps({"access_token": invalid}))
    for invalid_expiry in ("not-a-number", -1, 10**20):
        with pytest.raises(AssistantError, match="انقضا"):
            TokenBundle.from_oauth(
                {"access_token": "valid", "expires_in": invalid_expiry}, client_id="client"
            )


def test_oauth_pkce_is_session_bound_and_one_time(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = MemoryVault()
    oauth = GitHubOAuth(github_settings(), vault)  # type: ignore[arg-type]
    captured: dict[str, str] = {}

    def broker(operation: str, body: dict[str, str]) -> dict[str, str]:
        assert operation == "exchange"
        captured.update(body)
        return {"access_token": "access", "refresh_token": "refresh", "expires_in": "3600"}

    monkeypatch.setattr(oauth, "_broker", broker)
    authorization_url = oauth.start(
        redirect_uri="https://app.example/api/github/oauth/callback",
        browser_session="browser-session",
        origin="https://app.example",
    )
    query = parse_qs(urlparse(authorization_url).query)
    state = query["state"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert "code_verifier" not in query

    with pytest.raises(AssistantError, match="نشست مرورگر"):
        oauth.complete(state=state, code="code", browser_session="other-session")
    # The state is consumed before any exchange, even for a session mismatch.
    with pytest.raises(AssistantError, match="منقضی"):
        oauth.complete(state=state, code="code", browser_session="browser-session")

    second_url = oauth.start(
        redirect_uri="https://app.example/api/github/oauth/callback",
        browser_session="browser-session",
        origin="https://app.example",
    )
    second_state = parse_qs(urlparse(second_url).query)["state"][0]
    token, origin = oauth.complete(
        state=second_state,
        code="code",
        browser_session="browser-session",
    )
    assert token.access_token == "access"
    assert vault.token == token
    assert origin == "https://app.example"
    assert 43 <= len(captured["code_verifier"]) <= 128


def test_oauth_broker_refuses_redirects_with_sensitive_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth = GitHubOAuth(github_settings(), MemoryVault())  # type: ignore[arg-type]
    captured: dict = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=307, json=dict)

    monkeypatch.setattr("local_agent.github.oauth.requests.post", post)
    with pytest.raises(AssistantError, match="رد کرد"):
        oauth._broker("refresh", {"refresh_token": "sensitive-refresh"})
    assert captured["allow_redirects"] is False


def test_oauth_revoke_deletes_vault_even_when_broker_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = MemoryVault()
    settings = github_settings()
    token = TokenBundle(
        access_token="access",
        client_id=settings.client_id,
        binding=credential_binding(
            client_id=settings.client_id,
            broker_url=settings.broker_url,
            api_url=settings.api_url,
            web_url=settings.web_url,
            graphql_url=settings.graphql_url,
        ),
    )
    vault.save(token)
    oauth = GitHubOAuth(settings, vault)  # type: ignore[arg-type]

    def fail(*_args, **_kwargs):
        raise AssistantError("offline")

    monkeypatch.setattr(oauth, "_broker", fail)
    with pytest.raises(AssistantError, match="offline"):
        oauth.revoke(token)
    assert vault.deleted
    assert vault.token is None


class FakeResponse:
    def __init__(self, payload, *, status: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.reason = "error" if status >= 400 else "ok"
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return self._payload

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.content

    def close(self):
        return None


class PagingSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        page = kwargs["params"]["page"]
        items = list(range(100)) if page == 1 else [100, 101]
        return FakeResponse(
            items,
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Reset": "123",
                "X-RateLimit-Resource": "core",
            },
        )


def test_client_pagination_is_bounded_and_tracks_rate_limit() -> None:
    client = GitHubClient(github_settings(), lambda: "never-log-this-token")
    session = PagingSession()
    client._session = session  # type: ignore[assignment]
    result = client.paginate("/user/repos", max_items=150)
    assert len(result["items"]) == 102
    assert result["pagination"]["pages_fetched"] == 2
    assert result["rate_limit"]["remaining"] == 4999
    assert all("never-log-this-token" not in call["url"] for call in session.calls)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer never-log-this-token"
    with pytest.raises(AssistantError):
        client.paginate("/user/repos", max_items=2001)


def test_client_refuses_api_redirects_but_allows_get_only_raw_download_redirects() -> None:
    client = GitHubClient(github_settings(), lambda: "token")
    calls: list[dict] = []

    def request(_method, _url, **kwargs):
        calls.append(kwargs)
        if kwargs["allow_redirects"]:
            return FakeResponse({"download": True}, status=200)
        return FakeResponse({}, status=302, headers={"Location": "https://evil.example"})

    client._session = SimpleNamespace(request=request)
    with pytest.raises(GitHubAPIError) as error:
        client.request("POST", "/repos/o/r/issues", json_body={"title": "sensitive"})
    assert error.value.status == 302
    assert calls[0]["allow_redirects"] is False

    # Raw operations are allow-listed GET downloads. GitHub uses redirects to
    # expiring object-storage URLs and requests strips Authorization whenever
    # the redirect changes host.
    client.request("GET", "/repos/o/r/actions/artifacts/1/zip", raw=True)
    assert calls[1]["allow_redirects"] is True
    with pytest.raises(ValueError, match="must use GET"):
        client.request("POST", "/repos/o/r/archive", raw=True)
    with pytest.raises(ValueError, match="cannot be overridden"):
        client.request("GET", "/user", headers={"Authorization": "Bearer attacker"})

    client._session = SimpleNamespace(
        request=lambda *_args, **_kwargs: FakeResponse(
            {}, status=200, headers={"Content-Length": str(256 * 1024 * 1024 + 1)}
        )
    )
    with pytest.raises(AssistantError, match="۲۵۶"):
        client.request("GET", "/repos/o/r/actions/artifacts/1/zip", raw=True)


def test_client_sends_the_exact_preflighted_utf8_json_bytes() -> None:
    client = GitHubClient(github_settings(), lambda: "token")
    rest_calls: list[dict] = []
    graphql_calls: list[dict] = []

    def request(_method, _url, **kwargs):
        rest_calls.append(kwargs)
        return FakeResponse({"ok": True})

    def post(_url, **kwargs):
        graphql_calls.append(kwargs)
        return FakeResponse({"data": {"viewer": {"login": "octocat"}}})

    client._session = SimpleNamespace(request=request, post=post)
    client.request("POST", "/user/repos", json_body={"description": "سلام"})
    client.graphql("query{viewer{login}}", {"label": "فارسی"})

    assert json.loads(rest_calls[0]["data"].decode("utf-8")) == {"description": "سلام"}
    assert "json" not in rest_calls[0]
    assert rest_calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(graphql_calls[0]["data"].decode("utf-8"))["variables"] == {
        "label": "فارسی"
    }
    assert "json" not in graphql_calls[0]


def test_client_rejects_invalid_or_oversized_json_before_loading_credentials() -> None:
    token_reads = 0

    def token_provider() -> str:
        nonlocal token_reads
        token_reads += 1
        return "must-not-be-loaded"

    client = GitHubClient(github_settings(), token_provider)
    client._session = SimpleNamespace(  # type: ignore[assignment]
        request=lambda *_args, **_kwargs: pytest.fail("network request attempted")
    )
    with pytest.raises(AssistantError, match="JSON معتبر"):
        client.request("POST", "/user/repos", json_body={"bad": {1, 2}})
    with pytest.raises(AssistantError, match="JSON معتبر"):
        client.graphql("query($n:Float!){rateLimit{limit}}", {"n": float("nan")})
    with pytest.raises(AssistantError, match="۲ مگابایت"):
        client.request("POST", "/user/repos", json_body={"body": "x" * (2 * 1024 * 1024)})
    assert token_reads == 0


def test_client_permission_error_is_typed() -> None:
    client = GitHubClient(github_settings(), lambda: "token")
    client._session = SimpleNamespace(
        request=lambda *_args, **_kwargs: FakeResponse(
            {"message": "Resource not accessible"},
            status=403,
            headers={"X-RateLimit-Remaining": "12", "X-GitHub-Request-Id": "RID"},
        )
    )
    with pytest.raises(GitHubAPIError) as error:
        client.request("GET", "/repos/o/r/issues", required_permission="Issues: read")
    assert error.value.status == 403
    assert error.value.required_permission == "Issues: read"
    assert error.value.request_id == "RID"


def test_service_enforces_repository_selection_case_insensitively(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    assert service._repo_path({"owner": "OWNER", "repo": "REPO"}) == "/repos/OWNER/REPO"
    with pytest.raises(AssistantError, match="انتخاب نشده"):
        service._repo_path({"owner": "other", "repo": "repo"})

    service.update_settings(
        github_settings(selected_repositories=()),
        default_clone_root=tmp_path,
    )
    with pytest.raises(AssistantError, match="هیچ مخزنی"):
        service._repo_path({"owner": "owner", "repo": "repo"})


def test_service_rejects_credential_identity_changes_until_disconnect(tmp_path: Path) -> None:
    original = github_settings()
    service = GitHubService(original, default_clone_root=tmp_path)
    vault = MemoryVault()
    vault.token = TokenBundle(access_token="access", client_id=original.client_id)
    service.vault = vault  # type: ignore[assignment]

    changed = github_settings(broker_url="https://new-broker.example")
    with pytest.raises(AssistantError, match="قطع کنید"):
        service.update_settings(changed, default_clone_root=tmp_path)
    assert service.settings == original

    vault.delete()
    service.update_settings(changed, default_clone_root=tmp_path)
    assert service.settings == changed


def test_service_has_no_arbitrary_operation_escape(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    with pytest.raises(AssistantError, match="ناشناخته"):
        service.read("raw_request", {"url": "https://evil.example"})
    with pytest.raises(AssistantError, match="ناشناخته"):
        service.write("graphql", {"query": "mutation { anything }"})
    with pytest.raises(AssistantError, match="ناشناخته"):
        service.download("url", {"url": "https://evil.example"})


def test_issue_listing_excludes_pull_request_entries_transparently(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    captured: dict = {}

    def paginate(path, **kwargs):
        captured.update({"path": path, **kwargs})
        return {
            "items": [
                {"id": 1, "number": 1, "title": "issue"},
                {"id": 2, "number": 2, "title": "pull", "pull_request": {"url": "x"}},
            ],
            "pagination": {"pages_fetched": 1},
        }

    service.client.paginate = paginate  # type: ignore[method-assign]
    result = service.read(
        "issues",
        {"owner": "owner", "repo": "repo", "state": "all", "labels": "bug", "limit": 20},
    )
    assert [item["number"] for item in result["items"]] == [1]
    assert result["excluded_pull_requests"] == 1
    assert captured["path"] == "/repos/owner/repo/issues"
    assert captured["params"]["state"] == "all"


def test_service_rejects_non_string_optional_repository_inputs(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    service.client.request = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "network request attempted"
    )
    with pytest.raises(AssistantError, match="ref.*رشته"):
        service.read(
            "repository_tree",
            {"owner": "owner", "repo": "repo", "ref": 0},
        )
    with pytest.raises(AssistantError, match="org.*رشته"):
        service.write("repository_create", {"name": "new-repo", "org": False})


def test_update_operations_reject_noop_payloads(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    service.client.request = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "network request attempted"
    )
    service.client.graphql = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "GraphQL request attempted"
    )
    operations = (
        ("repository_update", {}),
        ("issue_update", {"number": 1}),
        ("pull_update", {"number": 1}),
        ("release_update", {"release_id": 1}),
        ("codespace_update", {"codespace_name": "safe-name"}),
        ("project_update", {"project_id": "PVT_kwDOA"}),
    )
    for operation, extra in operations:
        with pytest.raises(AssistantError, match="حداقل یک فیلد"):
            service.write(
                operation,
                {"owner": "owner", "repo": "repo", **extra},
            )


def test_disabled_service_blocks_remote_and_local_operations(tmp_path: Path) -> None:
    service = GitHubService(github_settings(enabled=False), default_clone_root=tmp_path)
    service.client.request = lambda *_args, **_kwargs: pytest.fail("network request attempted")  # type: ignore[method-assign]

    calls = (
        lambda: service.read("account"),
        lambda: service.write("issue_create", {}),
        lambda: service.download("workflow_logs", {}),
        lambda: service.local_read("local_status", {"path": "."}),
        service.access_token,
    )
    for call in calls:
        with pytest.raises(AssistantError, match="غیرفعال"):
            call()


def test_service_local_commit_derives_isolated_github_identity(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    captured: dict = {}

    def commit(path, message, **kwargs):
        captured.update({"path": path, "message": message, **kwargs})
        return {"ok": True, "sha": "a" * 40}

    service.account = lambda **_kwargs: {  # type: ignore[method-assign]
        "id": 7,
        "login": "octocat",
        "name": "The Octocat",
    }
    service.git = SimpleNamespace(commit=commit)  # type: ignore[assignment]
    result = service.write(
        "local_commit",
        {"path": "repo", "message": "safe identity", "all_tracked": True},
    )
    assert result["sha"] == "a" * 40
    assert captured == {
        "path": "repo",
        "message": "safe identity",
        "paths": None,
        "all_tracked": True,
        "author_name": "The Octocat",
        "author_email": "7+octocat@users.noreply.github.com",
    }


def test_service_rejects_path_traversal_and_encodes_content_paths(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    captured: dict = {}

    def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"ok": True}

    service.client.request = request  # type: ignore[method-assign]
    for malicious in ("../issues", "dir/../../issues", "dir//file", "dir/\x00file"):
        with pytest.raises(AssistantError, match="path نامعتبر"):
            service.read("contents", {"owner": "owner", "repo": "repo", "path": malicious})
    with pytest.raises(AssistantError, match="org نامعتبر"):
        service.read("organization_repositories", {"org": ".."})

    service.read("contents", {"owner": "owner", "repo": "repo", "path": "dir/a b.txt"})
    assert captured["path"] == "/repos/owner/repo/contents/dir/a%20b.txt"


def test_created_repository_is_selected_and_immediately_usable(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    selected: list[str] = []
    service.set_repository_created_callback(selected.append)
    service.client.request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "id": 7,
        "name": "new-repo",
        "full_name": "owner/new-repo",
    }
    result = service.write("repository_create", {"name": "new-repo", "auto_init": True})
    assert result["full_name"] == "owner/new-repo"
    assert selected == ["owner/new-repo"]
    assert service._repo_path({"owner": "owner", "repo": "new-repo"}) == (
        "/repos/owner/new-repo"
    )
    assert "owner/new-repo" in service.git.allowed_repositories


def test_service_reads_bounded_text_files_and_complete_tree(tmp_path: Path) -> None:
    service = GitHubService(github_settings(), default_clone_root=tmp_path)
    calls: list[tuple[str, dict]] = []

    def request(_method, path, **kwargs):
        calls.append((path, kwargs))
        if "/contents/" in path:
            return {
                "type": "file",
                "path": "src/main.py",
                "name": "main.py",
                "sha": "a" * 40,
                "size": 12,
                "encoding": "base64",
                "content": base64.b64encode("سلام\n".encode()).decode(),
            }
        if "/git/trees/" in path:
            return {
                "sha": "b" * 40,
                "tree": [
                    {"path": "README.md", "type": "blob", "sha": "c" * 40},
                    {"path": "src", "type": "tree", "sha": "d" * 40},
                ],
                "truncated": False,
            }
        raise AssertionError(path)

    service.client.request = request  # type: ignore[method-assign]
    text = service.read(
        "file_text", {"owner": "owner", "repo": "repo", "path": "src/main.py"}
    )
    assert text["text"] == "سلام\n"
    tree = service.read(
        "repository_tree", {"owner": "owner", "repo": "repo", "ref": "main", "limit": 1}
    )
    assert tree["count"] == 1
    assert tree["truncated"] is True
    assert calls[-1][0].endswith("/git/trees/main")
    assert calls[-1][1]["params"] == {"recursive": "1"}

    with pytest.raises(AssistantError, match="max_bytes"):
        service.read(
            "file_text",
            {"owner": "owner", "repo": "repo", "path": "src/main.py", "max_bytes": 0},
        )
    for invalid in ("../main", "-option", "branch@{upstream}"):
        with pytest.raises(AssistantError, match="ref"):
            service.read(
                "repository_tree",
                {"owner": "owner", "repo": "repo", "ref": invalid},
            )


def test_local_git_diff_and_clone_inventory_are_confined(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    tracked.write_text("after\n", encoding="utf-8")
    local = LocalGit(
        root,
        lambda: "unused",
        web_url="https://github.com",
        allowed_repositories=("o/r",),
    )
    assert "-before" in local.diff("repo")["diff"]
    committed = local.commit(
        "repo",
        "update",
        all_tracked=True,
        author_name="GitHub Test",
        author_email="test@users.noreply.github.com",
    )
    assert len(committed["sha"]) == 40
    assert local.status("repo")["clean"] is True
    with pytest.raises(AssistantError, match="نویسنده"):
        local.commit(
            "repo",
            "invalid identity",
            all_tracked=True,
            author_name="Injected\nName",
            author_email="invalid",
        )
    inventory = local.repositories()
    assert len(inventory) == 1
    assert inventory[0]["path"] == str(repo.resolve())


def test_local_git_confines_paths_and_redacts_credentials(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    local = LocalGit(root, lambda: "token", web_url="https://github.com")
    status = local.status("repo")
    assert status["path"] == str(repo.resolve())
    with pytest.raises(AssistantError, match="داخل ریشه"):
        local.status("../outside")
    assert _redact_url("https://user:secret@github.com/o/r.git") == "https://github.com/o/r.git"


def test_local_git_rejects_git_directories_outside_clone_root(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    linked = root / "linked"
    external = tmp_path / "external.git"
    linked.mkdir(parents=True)
    external.mkdir()
    (linked / ".git").write_text(f"gitdir: {external}\n", encoding="utf-8")
    local = LocalGit(root, lambda: "token", web_url="https://github.com")
    with pytest.raises(AssistantError, match="دایرکتوری داخلی Git"):
        local.status("linked")

    repo = root / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / ".git" / "commondir").write_text(str(external), encoding="utf-8")
    with pytest.raises(AssistantError, match="دایرکتوری داخلی Git"):
        local.status("repo")


def test_local_git_rejects_foreign_and_credential_bearing_authenticated_remotes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://evil.example/o/r.git"],
        check=True,
    )
    token_calls = 0

    def token() -> str:
        nonlocal token_calls
        token_calls += 1
        return "must-not-be-sent"

    local = LocalGit(root, token, web_url="https://github.com")
    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        local.pull("repo")
    assert token_calls == 0

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "origin",
            "https://user:password@github.com/o/r.git",
        ],
        check=True,
    )
    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        local.push("repo")
    assert token_calls == 0


def test_local_git_allows_only_configured_github_origin_for_authentication(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    local = LocalGit(root, lambda: "token", web_url="https://github.com")
    assert local._authenticated_remote(repo, "origin", push=False) == "origin"
    assert local._authenticated_remote(repo, "origin", push=True) == "origin"

    # Every configured push URL is checked; one valid URL cannot hide a
    # second credential-exfiltration destination.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            "https://github.com/o/r.git",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            "https://evil.example/o/r.git",
        ],
        check=True,
    )
    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        local._authenticated_remote(repo, "origin", push=True)


def test_local_git_enforces_selected_repository_for_reads_and_authentication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    token_calls = 0

    def token() -> str:
        nonlocal token_calls
        token_calls += 1
        return "must-not-be-sent"

    local = LocalGit(
        root,
        token,
        web_url="https://github.com",
        allowed_repositories=("o/r",),
    )
    assert local.status("repo")["path"] == str(repo.resolve())

    # A distinct push URL is part of the origin identity even for local reads.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            "https://github.com/o/not-selected.git",
        ],
        check=True,
    )
    with pytest.raises(AssistantError, match="fetch و push"):
        local.status("repo")
    subprocess.run(
        ["git", "-C", str(repo), "config", "--unset-all", "remote.origin.pushurl"],
        check=True,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "origin",
            "https://github.com/o/not-selected.git",
        ],
        check=True,
    )
    with pytest.raises(AssistantError, match="انتخاب‌شده"):
        local.status("repo")
    with pytest.raises(AssistantError, match="انتخاب‌شده"):
        local.pull("repo")
    assert token_calls == 0


def test_authenticated_git_disables_helpers_and_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "clones"
    root.mkdir()
    captured: dict = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["askpass"] = Path(kwargs["env"]["GIT_ASKPASS"])
        assert captured["askpass"].exists()
        assert (captured["askpass"].parent / "credential").exists()
        kwargs["stdout"].write(b"ok sensitive-token")
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious-helper")
    monkeypatch.setenv("GITHUB_TOKEN", "inherited-token")
    monkeypatch.setattr(subprocess, "run", run)
    local = LocalGit(root, lambda: "sensitive-token", web_url="https://github.com")
    assert local._run(["fetch"], root, authenticated=True) == "ok [REDACTED]"
    command = captured["command"]
    assert "credential.helper=" in command
    assert "http.followRedirects=false" in command
    assert "protocol.allow=never" in command
    assert any(str(part).startswith("core.hooksPath=") for part in command)
    assert "sensitive-token" not in " ".join(command)
    assert captured["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]
    assert "GIT_CONFIG_COUNT" not in captured["env"]
    assert not captured["askpass"].parent.exists()


def test_askpass_files_are_private_literal_and_platform_specific(tmp_path: Path) -> None:
    posix_dir = tmp_path / "posix"
    posix_dir.mkdir()
    token = "literal!$% value"
    askpass = _write_askpass(posix_dir, token, windows=False)
    credential = posix_dir / "credential"
    assert credential.stat().st_mode & 0o777 == 0o600
    assert askpass.stat().st_mode & 0o777 == 0o700
    assert subprocess.check_output([str(askpass), "Username for GitHub"], text=True).strip() == (
        "x-access-token"
    )
    assert (
        subprocess.check_output([str(askpass), "Password for GitHub"], text=True).strip() == token
    )

    windows_dir = tmp_path / "windows"
    windows_dir.mkdir()
    windows_askpass = _write_askpass(windows_dir, token, windows=True)
    script = windows_askpass.read_text(encoding="utf-8")
    assert windows_askpass.suffix == ".cmd"
    assert "call" not in script.casefold()
    assert 'type "%~dp0credential"' in script
    assert "PLA_TOKEN" not in script
    assert ":username" in script
    assert token not in script


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("url.https://evil.example/.insteadof", "https://github.com/"),
        ("merge.exfil.driver", "malicious-command"),
        ("include.path", "/tmp/evil-config"),
        ("http.proxy", "http://evil.example"),
        ("remote.origin.proxy", "malicious-command"),
        ("remote.origin.uploadpack", "malicious-command"),
        ("remote.origin.vcs", "exfil"),
        ("submodule.exfil.url", "https://evil.example/repo"),
        ("core.gitproxy", "malicious-command"),
        ("core.askpass", "malicious-command"),
        ("core.worktree", "/tmp/outside"),
        ("alias.exfil", "!malicious-command"),
        ("diff.exfil.textconv", "malicious-command"),
        ("interactive.diffFilter", "malicious-command"),
        ("gpg.program", "malicious-command"),
        ("core.pager", "malicious-command"),
        ("protocol.ext.allow", "always"),
    ],
)
def test_authenticated_git_rejects_unsafe_local_config_before_token(
    tmp_path: Path, key: str, value: str
) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    token_calls = 0

    def token() -> str:
        nonlocal token_calls
        token_calls += 1
        return "must-not-be-exposed"

    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        LocalGit(root, token, web_url="https://github.com").pull("repo")
    assert token_calls == 0


def test_authenticated_git_allows_benign_config_and_checks_worktrees(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    worktree = root / "worktree"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Benign User"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "user@example.test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    executable = shutil.which("git")
    assert executable is not None
    probe_env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1"}
    LocalGit._assert_safe_local_config(executable, repo, probe_env)

    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "worktree-test", str(worktree)],
        check=True,
        capture_output=True,
    )
    assert (worktree / ".git").is_file()
    subprocess.run(
        ["git", "-C", str(worktree), "config", "merge.exfil.driver", "malicious-command"],
        check=True,
    )
    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        LocalGit(root, lambda: "must-not-be-exposed", web_url="https://github.com")._run(
            ["fetch"], worktree, authenticated=True
        )


def test_authenticated_git_rejects_command_spawning_repository_config(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "filter.exfiltrate.smudge", "malicious-command"],
        check=True,
    )
    calls = 0

    def token() -> str:
        nonlocal calls
        calls += 1
        return "must-not-be-exposed-to-filter"

    local = LocalGit(root, token, web_url="https://github.com")
    with pytest.raises(AssistantError, match="توکن ارسال نشد"):
        local.pull("repo")
    assert calls == 0
    with pytest.raises(AssistantError, match="پیکربندی محلی"):
        local.status("repo")
    assert calls == 0


def test_shared_ui_places_complete_github_card_with_protected_lifecycle_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "local_agent/web/templates/index.html").read_text(encoding="utf-8")
    javascript = (root / "local_agent/web/static/app.js").read_text(encoding="utf-8")

    # The shared Web/Desktop settings modal places GitHub between the other
    # first-class integrations and uses the official 16x16 mark path.
    github_at = html.index("integration-card--github")
    assert html.rfind("Telegram", 0, github_at) >= 0
    assert html.index("📧 جیمیل", github_at) > github_at
    assert 'viewBox="0 0 16 16"' in html[github_at:]
    assert "M8 0C3.58 0 0 3.64" in html[github_at:]

    for persian_state in (
        "اتصال به GitHub",
        "در حال اتصال…",
        "قطع اتصال",
        "متصل",
        "مخزن‌های در دسترس",
        "نصب‌های GitHub App",
        "Actions Secrets و Variables",
        "ساخت مخزن جدید",
        "مدیریت و بررسی مخزن‌ها",
        "درخت کامل",
        "Clone، Pull و Push محلی",
        "Commit با تأیید",
        "ساخت Pull Request با تأیید",
        "کشف Cloneها",
        "ویرایش مستقیم فایل",
        "ساخت Issue با تأیید",
        "اجرای Workflow با تأیید",
        "ساخت Release با تأیید",
        "Projects v2 — بررسی جمعی، جزئی و ساخت",
        "ساخت Project با تأیید",
        "زبان‌ها",
        "Workflowها",
    ):
        assert persian_state in html
    assert 'autocomplete="new-password"' in html
    assert "submitGitHubActionsEntry(false)" in html
    assert "submitGitHubActionsEntry(true)" in html

    # All lifecycle/read/write calls flow through the CSRF wrapper; popup is
    # opened synchronously and same-window navigation remains the fallback.
    assert '"X-CSRF-Token": csrf' in javascript
    assert 'window.open("about:blank"' in javascript
    assert "location.assign(data.authorization_url)" in javascript
    assert "event.origin !== location.origin" in javascript
    for endpoint in (
        "/api/github/oauth/start",
        "/api/github/disconnect",
        "/api/github/status",
        "/api/github/read",
        "/api/github/write",
    ):
        assert endpoint in javascript
    assert javascript.count('entry.value = ""') >= 2
    for operation in (
        "repository_create",
        "repository_tree",
        "file_text",
        "local_clone",
        "local_pull",
        "local_push",
        "local_repositories",
        "local_status",
        "local_branches",
        "local_log",
        "local_diff",
        "local_commit",
        "local_branch_create",
        "local_branch_switch",
        "pull_create",
        "file_upsert",
        "file_delete",
        "issue_create",
        "workflow_dispatch",
        "release_create",
        "projects",
        "project_create",
    ):
        assert operation in javascript


def test_github_actions_are_typed_and_all_mutations_force_human(tmp_path: Path) -> None:
    registry = ActionRegistry()
    context = ActionContext(runtime=None, confirmation_gate=None, work_dir=tmp_path)  # type: ignore[arg-type]
    register_github(registry, context)
    writes = [
        action
        for action in registry.all()
        if action.name.endswith("_manage")
        or action.name in {"github.file_write", "github.local_write"}
    ]
    assert writes
    assert all(action.risk_level is Risk.DESTRUCTIVE for action in writes)
    assert all(action.force_human_confirmation for action in writes)
    actions_manage = registry.get("github.actions_manage")
    assert "actions_secret_set" not in actions_manage.parameters["operation"]["enum"]
    assert "value" not in json.dumps(actions_manage.parameters)


def test_public_auto_confirm_cannot_run_github_mutation(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    response = handlers.handle(
        {
            "id": "github-write",
            "type": "invoke_action",
            "payload": {
                "name": "github.issue_manage",
                "arguments": {
                    "operation": "issue_create",
                    "params": {"owner": "owner", "repo": "repo", "title": "title"},
                },
                "auto_confirm": True,
            },
        }
    )
    assert response["ok"] is True
    assert response["result"]["success"] is False
    assert response["result"]["refused"] is True
    assert "auto_confirm" in response["result"]["text"]


def test_argument_logging_redacts_nested_credentials() -> None:
    summary = _summarize_arguments(
        {
            "params": {
                "owner": "owner",
                "access_token": "ghu-secret",
                "value": "secret-value",
                "nested": [{"client_secret": "broker-secret"}],
            }
        }
    )
    assert "ghu-secret" not in summary
    assert "secret-value" not in summary
    assert "broker-secret" not in summary
    assert "[REDACTED]" in summary
