"""High-level, allow-listed GitHub operations and lifecycle ownership."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from ..core.config import GitHubSettings
from ..core.errors import AssistantError
from .client import GitHubClient
from .credentials import CredentialVault, TokenBundle, credential_binding
from .git import LocalGit
from .oauth import GitHubOAuth

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SEARCH_TYPES = {"repositories", "code", "issues", "commits", "users", "topics"}


class GitHubService:
    """Single integration object shared by Web, Desktop and agent actions.

    Every operation is explicitly mapped.  There is no caller-controlled URL,
    HTTP method, REST path, shell command, or GraphQL document.
    """

    def __init__(self, settings: GitHubSettings, *, default_clone_root: Path) -> None:
        self.settings = settings
        self.vault = CredentialVault()
        self.oauth = GitHubOAuth(settings, self.vault)
        self.client = GitHubClient(settings, self.access_token)
        root = (
            Path(settings.local_clone_root).expanduser()
            if settings.local_clone_root
            else default_clone_root
        )
        self.git = LocalGit(
            root,
            self.access_token,
            web_url=settings.web_url,
            allowed_repositories=settings.selected_repositories,
        )
        self._token_lock = threading.RLock()
        self._account_cache: tuple[float, dict[str, Any]] | None = None
        self._repository_created_callback: Callable[[str], None] | None = None
        self._session_repositories: set[str] = set()

    def set_repository_created_callback(self, callback: Callable[[str], None]) -> None:
        """Persist newly-created repositories through the owning application."""
        self._repository_created_callback = callback

    def validate_settings_update(self, settings: GitHubSettings) -> None:
        credential_identity = (
            "client_id",
            "broker_url",
            "api_url",
            "web_url",
            "graphql_url",
        )
        if any(
            getattr(settings, field) != getattr(self.settings, field)
            for field in credential_identity
        ):
            token = self.vault.load()
            if token is not None:
                raise AssistantError(
                    "پیش از تغییر Client ID یا نشانی‌های GitHub، اتصال حساب را قطع کنید"
                )

    def update_settings(self, settings: GitHubSettings, *, default_clone_root: Path) -> None:
        self.validate_settings_update(settings)
        self.settings = settings
        self.oauth.update_settings(settings)
        self.client.update_settings(settings)
        root = (
            Path(settings.local_clone_root).expanduser()
            if settings.local_clone_root
            else default_clone_root
        )
        self.git.update(
            root,
            web_url=settings.web_url,
            allowed_repositories=settings.selected_repositories,
        )
        self._session_repositories.clear()
        self._account_cache = None

    # ---------------------------------------------------------------- auth

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise AssistantError("اتصال GitHub در تنظیمات غیرفعال است")

    def _credential_binding(self) -> str:
        return credential_binding(
            client_id=self.settings.client_id,
            broker_url=self.settings.broker_url,
            api_url=self.settings.api_url,
            web_url=self.settings.web_url,
            graphql_url=self.settings.graphql_url,
        )

    def _token_matches_settings(self, token: TokenBundle) -> bool:
        return bool(
            token.client_id == self.settings.client_id
            and token.binding
            and token.binding == self._credential_binding()
        )

    def access_token(self) -> str:
        self._require_enabled()
        with self._token_lock:
            token = self.vault.load()
            if token is None or not self._token_matches_settings(token):
                raise AssistantError("حساب GitHub متصل نیست یا اعتبار ذخیره‌شده به این پیکربندی تعلق ندارد")
            if token.expires_within(90):
                token = self.oauth.refresh(token)
            return token.access_token

    def complete_oauth(self, *, state: str, code: str, browser_session: str) -> str:
        self._account_cache = None
        token, origin = self.oauth.complete(
            state=state,
            code=code,
            browser_session=browser_session,
        )
        # Verify immediately. Invalid tokens do not silently become connected
        # or remain in the operating-system vault after a failed callback.
        try:
            self.account(force=True)
        except Exception:
            # A token that cannot verify must not remain live remotely or in
            # the local vault. Preserve the original verification error.
            try:
                # revoke() deletes from the vault in its own finally block.
                self.oauth.revoke(token)
            except AssistantError:
                pass
            raise
        return origin

    def disconnect(self) -> None:
        try:
            with self._token_lock:
                token = self.vault.load()
                if token is not None:
                    if self._token_matches_settings(token):
                        self.oauth.revoke(token)
                    else:
                        # Never disclose a credential to a broker from a
                        # different configuration. Remove the unusable local
                        # credential; remote revocation must be done at its
                        # original GitHub host by the user.
                        self.vault.delete()
        finally:
            self._account_cache = None

    def status(self, *, verify: bool = False) -> dict[str, Any]:
        base: dict[str, Any] = {
            "enabled": self.settings.enabled,
            "configured": bool(self.settings.client_id and self.settings.broker_url),
            "connected": False,
            "vault_available": self.vault.available,
            "vault_error": self.vault.error,
            "account": None,
            "rate_limit": self.client.rate_limit.to_dict(),
            "selected_repositories": list(self.settings.selected_repositories),
            "clone_root": str(self.git.root),
        }
        if not self.vault.available:
            return base
        try:
            token = self.vault.load()
            if token is None or not self._token_matches_settings(token):
                return base
            base["connected"] = True
            base["expires_at"] = token.expires_at
            if verify:
                base["account"] = self.account()
        except AssistantError as exc:
            base["connected"] = False
            base["error"] = str(exc)
        return base

    def account(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._account_cache and self._account_cache[0] > now:
            return self._account_cache[1]
        payload = self.client.request("GET", "/user", required_permission="Metadata: read")
        account = _pick(
            payload,
            "login",
            "id",
            "node_id",
            "name",
            "avatar_url",
            "html_url",
            "type",
            "company",
            "location",
        )
        self._account_cache = (now + 60, account)
        return account

    # -------------------------------------------------------------- reads

    def read(self, operation: str, params: dict[str, Any] | None = None) -> Any:
        self._require_enabled()
        p = dict(params or {})
        handlers = {
            "account": lambda: self.account(force=_boolean(p, "refresh", default=False)),
            "installations": lambda: self.client.paginate(
                "/user/installations", item_key="installations", max_items=_limit(p)
            ),
            "installation_repositories": lambda: self.client.paginate(
                f"/user/installations/{_positive_id(p, 'installation_id')}/repositories",
                item_key="repositories",
                max_items=_limit(p),
            ),
            "repositories": lambda: self.client.paginate(
                "/user/repos",
                params={
                    "affiliation": _affiliations(p),
                    "visibility": _enum_default(
                        p, "visibility", {"all", "public", "private"}, "all"
                    ),
                    "sort": _enum_default(
                        p, "sort", {"created", "updated", "pushed", "full_name"}, "updated"
                    ),
                },
                max_items=_limit(p),
            ),
            "repository": lambda: self._repo_get(p),
            "contents": lambda: self._contents(p),
            "file_text": lambda: self._file_text(p),
            "repository_tree": lambda: self._repository_tree(p),
            "commits": lambda: self._repo_page(p, "commits", permission="Contents: read"),
            "commit": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/commits/{quote(_api_ref(p, 'ref'), safe='')}",
                required_permission="Contents: read",
            ),
            "compare": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/compare/{quote(_api_ref(p, 'base'), safe='')}...{quote(_api_ref(p, 'head'), safe='')}",
                required_permission="Contents: read",
            ),
            "languages": lambda: self.client.request(
                "GET", f"{self._repo_path(p)}/languages", required_permission="Metadata: read"
            ),
            "contributors": lambda: self._repo_page(
                p, "contributors", permission="Metadata: read"
            ),
            "branches": lambda: self._repo_page(p, "branches", permission="Contents: read"),
            "branch_protection": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/branches/{quote(_api_ref(p, 'branch'), safe='')}/protection",
                required_permission="Administration: read",
            ),
            "branch_rules": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/rules/branches/{quote(_api_ref(p, 'branch'), safe='')}",
                required_permission="Metadata: read",
            ),
            "rulesets": lambda: self._repo_page(p, "rulesets", permission="Metadata: read"),
            "ruleset": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/rulesets/{_positive_id(p, 'ruleset_id')}",
                params={"includes_parents": _boolean(p, "includes_parents", default=True)},
                required_permission="Metadata: read",
            ),
            "ruleset_history": lambda: self.client.paginate(
                f"{self._repo_path(p)}/rulesets/{_positive_id(p, 'ruleset_id')}/history",
                max_items=_limit(p),
                required_permission="Administration: read",
            ),
            "tags": lambda: self._repo_page(p, "tags", permission="Metadata: read"),
            # GitHub's Issues endpoint also returns pull requests.  Keep this
            # operation semantically limited to Issues; PRs have their own
            # dedicated `pulls` operation and workspace section.
            "issues": lambda: self._issues(p),
            "issue": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}",
                required_permission="Issues: read",
            ),
            "issue_comments": lambda: self.client.paginate(
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}/comments",
                max_items=_limit(p),
                required_permission="Issues: read",
            ),
            "pulls": lambda: self._repo_page(
                p,
                "pulls",
                query={
                    "state": _enum_default(p, "state", {"open", "closed", "all"}, "open"),
                    "sort": _enum_default(
                        p, "sort", {"created", "updated", "popularity", "long-running"}, "updated"
                    ),
                },
                permission="Pull requests: read",
            ),
            "pull": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}",
                required_permission="Pull requests: read",
            ),
            "pull_files": lambda: self.client.paginate(
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}/files",
                max_items=_limit(p),
                required_permission="Pull requests: read",
            ),
            "pull_reviews": lambda: self.client.paginate(
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}/reviews",
                max_items=_limit(p),
                required_permission="Pull requests: read",
            ),
            "discussion_categories": lambda: self._discussion_categories(p),
            "discussions": lambda: self._discussions(p),
            "discussion": lambda: self._discussion(p),
            "check_runs": lambda: self.client.paginate(
                f"{self._repo_path(p)}/commits/{quote(_api_ref(p, 'ref'), safe='')}/check-runs",
                item_key="check_runs",
                max_items=_limit(p),
                required_permission="Checks: read",
            ),
            "check_run": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/check-runs/{_positive_id(p, 'check_run_id')}",
                required_permission="Checks: read",
            ),
            "check_run_annotations": lambda: self.client.paginate(
                f"{self._repo_path(p)}/check-runs/{_positive_id(p, 'check_run_id')}/annotations",
                max_items=_limit(p),
                required_permission="Checks: read",
            ),
            "check_suites": lambda: self.client.paginate(
                f"{self._repo_path(p)}/commits/{quote(_api_ref(p, 'ref'), safe='')}/check-suites",
                item_key="check_suites",
                max_items=_limit(p),
                required_permission="Checks: read",
            ),
            "check_suite": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/check-suites/{_positive_id(p, 'check_suite_id')}",
                required_permission="Checks: read",
            ),
            "check_suite_runs": lambda: self.client.paginate(
                f"{self._repo_path(p)}/check-suites/{_positive_id(p, 'check_suite_id')}/check-runs",
                item_key="check_runs",
                max_items=_limit(p),
                required_permission="Checks: read",
            ),
            "workflows": lambda: self._repo_page(
                p, "actions/workflows", item_key="workflows", permission="Actions: read"
            ),
            "workflow": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/actions/workflows/{quote(_required(p, 'workflow_id'), safe='')}",
                required_permission="Actions: read",
            ),
            "workflow_runs": lambda: self._workflow_runs(p),
            "workflow_run": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/actions/runs/{_positive_id(p, 'run_id')}",
                required_permission="Actions: read",
            ),
            "workflow_run_jobs": lambda: self.client.paginate(
                f"{self._repo_path(p)}/actions/runs/{_positive_id(p, 'run_id')}/jobs",
                item_key="jobs",
                max_items=_limit(p),
                required_permission="Actions: read",
            ),
            "artifacts": lambda: self._repo_page(
                p, "actions/artifacts", item_key="artifacts", permission="Actions: read"
            ),
            "actions_secrets": lambda: self._repo_page(
                p, "actions/secrets", item_key="secrets", permission="Secrets: read"
            ),
            "actions_variables": lambda: self._repo_page(
                p, "actions/variables", item_key="variables", permission="Variables: read"
            ),
            "organization_actions_secrets": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/actions/secrets",
                item_key="secrets",
                max_items=_limit(p),
                required_permission="Organization Secrets: read",
            ),
            "organization_actions_variables": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/actions/variables",
                item_key="variables",
                max_items=_limit(p),
                required_permission="Organization Variables: read",
            ),
            "environment_actions_secrets": lambda: self._repo_page(
                p,
                f"environments/{quote(_path_name(p, 'environment'), safe='')}/secrets",
                item_key="secrets",
                permission="Environments: read",
            ),
            "environment_actions_variables": lambda: self._repo_page(
                p,
                f"environments/{quote(_path_name(p, 'environment'), safe='')}/variables",
                item_key="variables",
                permission="Environments: read",
            ),
            "actions_caches": lambda: self._repo_page(
                p, "actions/caches", item_key="actions_caches", permission="Actions: read"
            ),
            "actions_cache_usage": lambda: self.client.request(
                "GET", f"{self._repo_path(p)}/actions/cache/usage", required_permission="Actions: read"
            ),
            "self_hosted_runners": lambda: self._repo_page(
                p, "actions/runners", item_key="runners", permission="Administration: read"
            ),
            "releases": lambda: self._repo_page(p, "releases", permission="Contents: read"),
            "release": lambda: self._release(p),
            "deployments": lambda: self._repo_page(
                p, "deployments", permission="Deployments: read"
            ),
            "deployment_statuses": lambda: self.client.paginate(
                f"{self._repo_path(p)}/deployments/{_positive_id(p, 'deployment_id')}/statuses",
                max_items=_limit(p),
                required_permission="Deployments: read",
            ),
            "environments": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/environments",
                required_permission="Environments: read",
            ),
            "collaborators": lambda: self._repo_page(
                p, "collaborators", permission="Administration: read"
            ),
            "webhooks": lambda: self._repo_page(p, "hooks", permission="Webhooks: read"),
            "webhook": lambda: self.client.request(
                "GET",
                f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}",
                required_permission="Webhooks: read",
            ),
            "webhook_deliveries": lambda: self.client.paginate(
                f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}/deliveries",
                max_items=_limit(p),
                required_permission="Webhooks: read",
            ),
            "repository_codespaces": lambda: self._repo_page(
                p, "codespaces", item_key="codespaces", permission="Codespaces: read"
            ),
            "codespaces": lambda: self.client.paginate(
                "/user/codespaces",
                item_key="codespaces",
                max_items=_limit(p),
                required_permission="Codespaces: read",
            ),
            "codespace": lambda: self.client.request(
                "GET",
                f"/user/codespaces/{quote(_path_name(p, 'codespace_name'), safe='')}",
                required_permission="Codespaces: read",
            ),
            "codespace_machines": lambda: self.client.paginate(
                f"/user/codespaces/{quote(_path_name(p, 'codespace_name'), safe='')}/machines",
                item_key="machines",
                max_items=_limit(p),
                required_permission="Codespaces metadata: read",
            ),
            "codespace_secrets": lambda: self.client.paginate(
                "/user/codespaces/secrets",
                item_key="secrets",
                max_items=_limit(p),
                required_permission="Codespaces: read",
            ),
            "packages": lambda: self._packages(p),
            "package_versions": lambda: self._package_versions(p),
            "dependabot_alerts": lambda: self._repo_page(
                p, "dependabot/alerts", permission="Dependabot alerts: read"
            ),
            "code_scanning_alerts": lambda: self._repo_page(
                p, "code-scanning/alerts", permission="Code scanning alerts: read"
            ),
            "secret_scanning_alerts": lambda: self._repo_page(
                p, "secret-scanning/alerts", permission="Secret scanning alerts: read"
            ),
            "security_advisories": lambda: self._repo_page(
                p, "security-advisories", permission="Repository security advisories: read"
            ),
            "organizations": lambda: self.client.paginate(
                "/user/orgs", max_items=_limit(p), required_permission="Organization members: read"
            ),
            "organization_repositories": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/repos",
                max_items=_limit(p),
                required_permission="Metadata: read",
            ),
            "organization_members": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/members",
                max_items=_limit(p),
                required_permission="Organization members: read",
            ),
            "organization_runners": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/actions/runners",
                item_key="runners",
                max_items=_limit(p),
                required_permission="Organization self-hosted runners: read",
            ),
            "organization_webhooks": lambda: self.client.paginate(
                f"/orgs/{_name(p, 'org')}/hooks",
                max_items=_limit(p),
                required_permission="Organization webhooks: read",
            ),
            "notifications": lambda: self.client.paginate(
                "/notifications",
                params={
                    "all": _boolean(p, "all", default=False),
                    "participating": _boolean(p, "participating", default=False),
                },
                max_items=_limit(p),
                required_permission="Notifications: read",
            ),
            "notification_thread": lambda: self.client.request(
                "GET",
                f"/notifications/threads/{_positive_id(p, 'thread_id')}",
                required_permission="Notifications: read",
            ),
            "notification_subscription": lambda: self.client.request(
                "GET",
                f"/notifications/threads/{_positive_id(p, 'thread_id')}/subscription",
                required_permission="Notifications: read",
            ),
            "search": lambda: self._search(p),
            "projects": lambda: self._projects(p),
            "project": lambda: self._project(p),
        }
        handler = handlers.get(operation)
        if handler is None:
            raise AssistantError(f"عملیات خواندن GitHub ناشناخته است: {operation}")
        return handler()

    def download(self, operation: str, params: dict[str, Any]) -> tuple[bytes, str, str]:
        self._require_enabled()
        if operation == "workflow_logs":
            path = f"{self._repo_path(params)}/actions/runs/{_positive_id(params, 'run_id')}/logs"
            return (
                self.client.request("GET", path, raw=True, required_permission="Actions: read"),
                "workflow-logs.zip",
                "application/zip",
            )
        if operation == "artifact":
            artifact_id = _positive_id(params, "artifact_id")
            path = f"{self._repo_path(params)}/actions/artifacts/{artifact_id}/zip"
            return (
                self.client.request("GET", path, raw=True, required_permission="Actions: read"),
                f"artifact-{artifact_id}.zip",
                "application/zip",
            )
        if operation == "release_asset":
            asset_id = _positive_id(params, "asset_id")
            path = f"{self._repo_path(params)}/releases/assets/{asset_id}"
            data = self.client.request(
                "GET",
                path,
                raw=True,
                headers={"Accept": "application/octet-stream"},
                required_permission="Contents: read",
            )
            return data, f"release-asset-{asset_id}", "application/octet-stream"
        raise AssistantError("دانلود GitHub ناشناخته است")

    def upload_release_asset(
        self,
        params: dict[str, Any],
        *,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Upload one bounded browser-provided file to an allow-listed repository release."""
        self._require_enabled()
        p = dict(params)
        release_id = _positive_id(p, "release_id")
        raw_name = Path(_required(p, "name")).name
        if (
            not raw_name
            or raw_name in {".", ".."}
            or len(raw_name.encode("utf-8")) > 255
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_name)
        ):
            raise AssistantError("نام فایل Release نامعتبر است")
        label = _optional_string(p, "label").strip()
        if len(label.encode("utf-8")) > 255 or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in label
        ):
            raise AssistantError("برچسب فایل Release نامعتبر است")
        media = str(content_type or "application/octet-stream").strip().lower()
        if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media):
            media = "application/octet-stream"
        path = f"{self._repo_path(p)}/releases/{release_id}/assets"
        return self.client.upload_release_asset(
            path,
            name=raw_name,
            label=label,
            data=data,
            content_type=media,
        )

    # ------------------------------------------------------------- writes

    def write(self, operation: str, params: dict[str, Any] | None = None) -> Any:
        self._require_enabled()
        p = dict(params or {})
        handlers = {
            "repository_create": lambda: self._repository_create(p),
            "repository_update": lambda: self.client.request(
                "PATCH",
                self._repo_path(p),
                json_body=_update_body(
                    p,
                    "repository",
                    "name",
                    "description",
                    "homepage",
                    "private",
                    "visibility",
                    "has_issues",
                    "has_projects",
                    "has_wiki",
                    "is_template",
                    "default_branch",
                    "allow_squash_merge",
                    "allow_merge_commit",
                    "allow_rebase_merge",
                    "archived",
                    "security_and_analysis",
                ),
                required_permission="Administration: write",
            ),
            "repository_delete": lambda: self.client.request(
                "DELETE", self._repo_path(p), required_permission="Administration: write"
            ),
            "repository_transfer": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/transfer",
                json_body={"new_owner": _name(p, "new_owner"), **_body(p, "new_name", "team_ids")},
                required_permission="Administration: write",
            ),
            "repository_topics": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/topics",
                json_body={"names": _string_list(p, "names")},
                required_permission="Administration: write",
            ),
            "fork": lambda: self._fork(p),
            "file_upsert": lambda: self._file_upsert(p),
            "file_delete": lambda: self._file_delete(p),
            "branch_create": lambda: self._branch_create(p),
            "branch_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/git/refs/heads/{quote(_required(p, 'branch'), safe='')}",
                required_permission="Contents: write",
            ),
            "branch_protection_update": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/branches/{quote(_api_ref(p, 'branch'), safe='')}/protection",
                json_body=_branch_protection_body(p),
                required_permission="Administration: write",
            ),
            "branch_protection_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/branches/{quote(_api_ref(p, 'branch'), safe='')}/protection",
                required_permission="Administration: write",
            ),
            "ruleset_create": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/rulesets",
                json_body=_ruleset_body(p, create=True),
                required_permission="Administration: write",
            ),
            "ruleset_update": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/rulesets/{_positive_id(p, 'ruleset_id')}",
                json_body=_ruleset_body(p, create=False),
                required_permission="Administration: write",
            ),
            "ruleset_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/rulesets/{_positive_id(p, 'ruleset_id')}",
                required_permission="Administration: write",
            ),
            "issue_create": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/issues",
                json_body=_required_body(
                    p, "title", optional=("body", "assignees", "milestone", "labels", "type")
                ),
                required_permission="Issues: write",
            ),
            "issue_update": lambda: self.client.request(
                "PATCH",
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}",
                json_body=_update_body(
                    p,
                    "issue",
                    "title",
                    "body",
                    "state",
                    "state_reason",
                    "assignees",
                    "milestone",
                    "labels",
                    "type",
                ),
                required_permission="Issues: write",
            ),
            "issue_comment": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}/comments",
                json_body={"body": _required(p, "body")},
                required_permission="Issues: write",
            ),
            "issue_lock": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}/lock",
                json_body=_body(p, "lock_reason"),
                required_permission="Issues: write",
            ),
            "issue_unlock": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/issues/{_positive_id(p, 'number')}/lock",
                required_permission="Issues: write",
            ),
            "pull_create": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/pulls",
                json_body=_required_body(
                    p, "title", "head", "base", optional=("body", "draft", "maintainer_can_modify")
                ),
                required_permission="Pull requests: write",
            ),
            "pull_update": lambda: self.client.request(
                "PATCH",
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}",
                json_body=_update_body(
                    p, "pull request", "title", "body", "state", "base", "maintainer_can_modify"
                ),
                required_permission="Pull requests: write",
            ),
            "pull_review": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}/reviews",
                json_body={
                    "event": _enum(p, "event", {"APPROVE", "REQUEST_CHANGES", "COMMENT"}),
                    **_body(p, "body", "commit_id", "comments"),
                },
                required_permission="Pull requests: write",
            ),
            "pull_merge": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/pulls/{_positive_id(p, 'number')}/merge",
                json_body=_body(p, "commit_title", "commit_message", "sha", "merge_method"),
                required_permission="Pull requests: write; Contents: write",
            ),
            "discussion_create": lambda: self._discussion_create(p),
            "discussion_update": lambda: self._discussion_update(p),
            "discussion_delete": lambda: self._graphql_node_mutation(
                "deleteDiscussion",
                "discussionId",
                _required(p, "discussion_id"),
                "clientMutationId",
            ),
            "discussion_comment": lambda: self._discussion_comment(p),
            "discussion_comment_update": lambda: self._discussion_comment_update(p),
            "discussion_comment_delete": lambda: self._graphql_node_mutation(
                "deleteDiscussionComment",
                "id",
                _required(p, "comment_id"),
                "clientMutationId",
            ),
            "discussion_close": lambda: self._discussion_state(p, close=True),
            "discussion_reopen": lambda: self._discussion_state(p, close=False),
            "check_run_create": lambda: self._check_run_create(p),
            "check_run_update": lambda: self._check_run_update(p),
            "check_run_rerequest": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/check-runs/{_positive_id(p, 'check_run_id')}/rerequest",
                required_permission="Checks: write",
            ),
            "check_suite_rerequest": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/check-suites/{_positive_id(p, 'check_suite_id')}/rerequest",
                required_permission="Checks: write",
            ),
            "workflow_dispatch": lambda: self._workflow_dispatch(p),
            "workflow_run_rerun": lambda: self._workflow_run_command(p, "rerun"),
            "workflow_run_cancel": lambda: self._workflow_run_command(p, "cancel"),
            "workflow_run_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/actions/runs/{_positive_id(p, 'run_id')}",
                required_permission="Actions: write",
            ),
            "artifact_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/actions/artifacts/{_positive_id(p, 'artifact_id')}",
                required_permission="Actions: write",
            ),
            "actions_secret_set": lambda: self._actions_secret_set(p),
            "actions_secret_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/actions/secrets/{_secret_name(p)}",
                required_permission="Secrets: write",
            ),
            "actions_variable_set": lambda: self._actions_variable_set(p),
            "actions_variable_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/actions/variables/{_secret_name(p)}",
                required_permission="Variables: write",
            ),
            "organization_actions_secret_set": lambda: self._scoped_actions_secret_set(
                p, scope="organization"
            ),
            "organization_actions_secret_repositories_set": lambda: self.client.request(
                "PUT",
                f"/orgs/{_name(p, 'org')}/actions/secrets/{_secret_name(p)}/repositories",
                json_body={
                    "selected_repository_ids": _positive_id_list(
                        p, "selected_repository_ids", allow_empty=True
                    )
                },
                required_permission="Organization Secrets: write",
            ),
            "organization_actions_secret_delete": lambda: self.client.request(
                "DELETE",
                f"/orgs/{_name(p, 'org')}/actions/secrets/{_secret_name(p)}",
                required_permission="Organization Secrets: write",
            ),
            "organization_actions_variable_set": lambda: self._scoped_actions_variable_set(
                p, scope="organization"
            ),
            "organization_actions_variable_delete": lambda: self.client.request(
                "DELETE",
                f"/orgs/{_name(p, 'org')}/actions/variables/{_secret_name(p)}",
                required_permission="Organization Variables: write",
            ),
            "environment_actions_secret_set": lambda: self._scoped_actions_secret_set(
                p, scope="environment"
            ),
            "environment_actions_secret_delete": lambda: self.client.request(
                "DELETE",
                f"{self._environment_path(p)}/secrets/{_secret_name(p)}",
                required_permission="Environments: write",
            ),
            "environment_actions_variable_set": lambda: self._scoped_actions_variable_set(
                p, scope="environment"
            ),
            "environment_actions_variable_delete": lambda: self.client.request(
                "DELETE",
                f"{self._environment_path(p)}/variables/{_secret_name(p)}",
                required_permission="Environments: write",
            ),
            "workflow_enable": lambda: self._workflow_toggle(p, enable=True),
            "workflow_disable": lambda: self._workflow_toggle(p, enable=False),
            "actions_cache_delete": lambda: self._actions_cache_delete(p),
            "runner_remove": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/actions/runners/{_positive_id(p, 'runner_id')}",
                required_permission="Administration: write",
            ),
            "runner_labels_set": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/actions/runners/{_positive_id(p, 'runner_id')}/labels",
                json_body={"labels": _string_list(p, "labels")},
                required_permission="Administration: write",
            ),
            "release_create": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/releases",
                json_body=_required_body(
                    p,
                    "tag_name",
                    optional=(
                        "target_commitish",
                        "name",
                        "body",
                        "draft",
                        "prerelease",
                        "generate_release_notes",
                        "make_latest",
                    ),
                ),
                required_permission="Contents: write",
            ),
            "release_update": lambda: self.client.request(
                "PATCH",
                f"{self._repo_path(p)}/releases/{_positive_id(p, 'release_id')}",
                json_body=_update_body(
                    p,
                    "release",
                    "tag_name",
                    "target_commitish",
                    "name",
                    "body",
                    "draft",
                    "prerelease",
                    "make_latest",
                ),
                required_permission="Contents: write",
            ),
            "release_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/releases/{_positive_id(p, 'release_id')}",
                required_permission="Contents: write",
            ),
            "release_asset_update": lambda: self.client.request(
                "PATCH",
                f"{self._repo_path(p)}/releases/assets/{_positive_id(p, 'asset_id')}",
                json_body={
                    "name": _path_name(p, "name"),
                    **_body(p, "label"),
                },
                required_permission="Contents: write",
            ),
            "release_asset_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/releases/assets/{_positive_id(p, 'asset_id')}",
                required_permission="Contents: write",
            ),
            "deployment_create": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/deployments",
                json_body=_required_body(
                    p,
                    "ref",
                    optional=(
                        "task",
                        "auto_merge",
                        "required_contexts",
                        "payload",
                        "environment",
                        "description",
                        "transient_environment",
                        "production_environment",
                    ),
                ),
                required_permission="Deployments: write",
            ),
            "deployment_status": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/deployments/{_positive_id(p, 'deployment_id')}/statuses",
                json_body=_required_body(
                    p,
                    "state",
                    optional=(
                        "target_url",
                        "log_url",
                        "description",
                        "environment",
                        "environment_url",
                        "auto_inactive",
                    ),
                ),
                required_permission="Deployments: write",
            ),
            "environment_update": lambda: self.client.request(
                "PUT",
                self._environment_path(p),
                json_body=_body(
                    p,
                    "wait_timer",
                    "prevent_self_review",
                    "reviewers",
                    "deployment_branch_policy",
                ),
                required_permission="Environments: write",
            ),
            "environment_delete": lambda: self.client.request(
                "DELETE", self._environment_path(p), required_permission="Environments: write"
            ),
            "collaborator_add": lambda: self.client.request(
                "PUT",
                f"{self._repo_path(p)}/collaborators/{_name(p, 'username')}",
                json_body={
                    "permission": _enum_default(
                        p, "permission", {"pull", "triage", "push", "maintain", "admin"}, "push"
                    )
                },
                required_permission="Administration: write",
            ),
            "collaborator_remove": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/collaborators/{_name(p, 'username')}",
                required_permission="Administration: write",
            ),
            "organization_membership_set": lambda: self.client.request(
                "PUT",
                f"/orgs/{_name(p, 'org')}/memberships/{_name(p, 'username')}",
                json_body={"role": _enum(p, "role", {"admin", "member"})},
                required_permission="Organization members: write",
            ),
            "organization_membership_remove": lambda: self.client.request(
                "DELETE",
                f"/orgs/{_name(p, 'org')}/memberships/{_name(p, 'username')}",
                required_permission="Organization members: write",
            ),
            "notification_mark": lambda: self._notification_mark(p),
            "notification_subscription_set": lambda: self.client.request(
                "PUT",
                f"/notifications/threads/{_positive_id(p, 'thread_id')}/subscription",
                json_body={
                    "subscribed": _boolean(p, "subscribed", default=True),
                    "ignored": _boolean(p, "ignored", default=False),
                },
                required_permission="Notifications: write",
            ),
            "notification_subscription_delete": lambda: self.client.request(
                "DELETE",
                f"/notifications/threads/{_positive_id(p, 'thread_id')}/subscription",
                required_permission="Notifications: write",
            ),
            "webhook_create": lambda: self._webhook_create(p),
            "webhook_update": lambda: self._webhook_update(p),
            "webhook_delete": lambda: self.client.request(
                "DELETE",
                f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}",
                required_permission="Webhooks: write",
            ),
            "webhook_ping": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}/pings",
                required_permission="Webhooks: write",
            ),
            "webhook_redeliver": lambda: self.client.request(
                "POST",
                f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}/deliveries/{_positive_id(p, 'delivery_id')}/attempts",
                required_permission="Webhooks: write",
            ),
            "codespace_create": lambda: self._codespace_create(p),
            "codespace_update": lambda: self.client.request(
                "PATCH",
                f"/user/codespaces/{quote(_path_name(p, 'codespace_name'), safe='')}",
                json_body=_update_body(
                    p, "codespace", "machine", "display_name", "recent_folders"
                ),
                required_permission="Codespaces: write",
            ),
            "codespace_start": lambda: self._codespace_command(p, "start"),
            "codespace_stop": lambda: self._codespace_command(p, "stop"),
            "codespace_delete": lambda: self.client.request(
                "DELETE",
                f"/user/codespaces/{quote(_path_name(p, 'codespace_name'), safe='')}",
                required_permission="Codespaces: write",
            ),
            "codespace_secret_set": lambda: self._codespace_secret_set(p),
            "codespace_secret_repositories_set": lambda: self.client.request(
                "PUT",
                f"/user/codespaces/secrets/{_secret_name(p)}/repositories",
                json_body={
                    "selected_repository_ids": _positive_id_list(
                        p, "selected_repository_ids", allow_empty=True
                    )
                },
                required_permission="Codespaces: write",
            ),
            "codespace_secret_delete": lambda: self.client.request(
                "DELETE",
                f"/user/codespaces/secrets/{_secret_name(p)}",
                required_permission="Codespaces: write",
            ),
            "package_version_delete": lambda: self._package_version_command(p, "DELETE"),
            "package_version_restore": lambda: self._package_version_command(p, "POST"),
            "dependabot_alert_update": lambda: self._dependabot_alert_update(p),
            "code_scanning_alert_update": lambda: self._code_scanning_alert_update(p),
            "secret_scanning_alert_update": lambda: self._secret_scanning_alert_update(p),
            "project_create": lambda: self._project_create(p),
            "project_update": lambda: self._project_update(p),
            "project_delete": lambda: self._project_delete(p),
            "project_add_item": lambda: self._project_add_item(p),
            "project_add_draft_issue": lambda: self._project_add_draft_issue(p),
            "project_update_draft_issue": lambda: self._project_update_draft_issue(p),
            "project_archive_item": lambda: self._project_archive_item(p),
            "project_unarchive_item": lambda: self._project_archive_item(p, archive=False),
            "project_delete_item": lambda: self._project_delete_item(p),
            "project_update_item_field": lambda: self._project_update_item_field(p),
            "project_clear_item_field": lambda: self._project_clear_item_field(p),
            "project_update_item_position": lambda: self._project_update_item_position(p),
            "local_clone": lambda: self._local_clone(p),
            "local_pull": lambda: self.git.pull(
                _required(p, "path"),
                remote=_optional_string(p, "remote", default="origin"),
                branch=_optional_string(p, "branch"),
            ),
            "local_push": lambda: self.git.push(
                _required(p, "path"),
                remote=_optional_string(p, "remote", default="origin"),
                branch=_optional_string(p, "branch"),
                set_upstream=_boolean(p, "set_upstream", default=False),
            ),
            "local_branch_create": lambda: self.git.branch_create(
                _required(p, "path"),
                _required(p, "branch"),
                start_point=_optional_string(p, "start_point"),
                switch=_boolean(p, "switch", default=True),
            ),
            "local_branch_switch": lambda: self.git.branch_switch(
                _required(p, "path"), _required(p, "branch")
            ),
            "local_branch_delete": lambda: self.git.branch_delete(
                _required(p, "path"),
                _required(p, "branch"),
                force=_boolean(p, "force", default=False),
            ),
            "local_commit": lambda: self._local_commit(p),
            "local_tag": lambda: self.git.tag(
                _required(p, "path"),
                _required(p, "tag"),
                message=_optional_string(p, "message"),
                push=_boolean(p, "push", default=False),
            ),
        }
        handler = handlers.get(operation)
        if handler is None:
            raise AssistantError(f"عملیات نوشتن GitHub ناشناخته است: {operation}")
        return handler()

    def local_read(self, operation: str, params: dict[str, Any]) -> Any:
        self._require_enabled()
        handlers = {
            "local_repositories": self.git.repositories,
            "local_status": lambda: self.git.status(_required(params, "path")),
            "local_branches": lambda: self.git.branches(_required(params, "path")),
            "local_log": lambda: self.git.log(
                _required(params, "path"), limit=min(_limit(params), 200)
            ),
            "local_remotes": lambda: self.git.remotes(_required(params, "path")),
            "local_diff": lambda: self.git.diff(
                _required(params, "path"),
                staged=_boolean(params, "staged", default=False),
                ref=_optional_string(params, "ref"),
            ),
        }
        handler = handlers.get(operation)
        if handler is None:
            raise AssistantError(f"عملیات محلی GitHub ناشناخته است: {operation}")
        return handler()

    # ------------------------------------------------------------ internals

    def _repo_path(self, p: dict[str, Any]) -> str:
        owner, repo = _name(p, "owner"), _name(p, "repo")
        full_name = f"{owner}/{repo}"
        selected = {item.casefold() for item in self.settings.selected_repositories}
        authorized = selected | self._session_repositories
        if self.settings.enabled and not authorized:
            raise AssistantError("هنوز هیچ مخزنی در تنظیمات GitHub انتخاب نشده است")
        if full_name.casefold() not in authorized:
            raise AssistantError(f"مخزن {full_name} در تنظیمات GitHub انتخاب نشده است")
        return f"/repos/{owner}/{repo}"

    def _repo_get(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.client.request("GET", self._repo_path(p), required_permission="Metadata: read")

    def _repo_page(
        self,
        p: dict[str, Any],
        suffix: str,
        *,
        query: dict[str, Any] | None = None,
        item_key: str | None = None,
        permission: str = "",
    ) -> dict[str, Any]:
        return self.client.paginate(
            f"{self._repo_path(p)}/{suffix}",
            params=query,
            item_key=item_key,
            max_items=_limit(p),
            required_permission=permission,
        )

    def _issues(self, p: dict[str, Any]) -> dict[str, Any]:
        result = self._repo_page(
            p,
            "issues",
            query={
                "state": _enum_default(p, "state", {"open", "closed", "all"}, "open"),
                "labels": _optional_string(p, "labels"),
                "sort": _enum_default(
                    p, "sort", {"created", "updated", "comments"}, "updated"
                ),
            },
            permission="Issues: read",
        )
        items = result.get("items")
        if isinstance(items, list):
            issue_items = [
                item
                for item in items
                if not isinstance(item, dict) or "pull_request" not in item
            ]
            result["items"] = issue_items
            result["excluded_pull_requests"] = len(items) - len(issue_items)
        return result

    def _contents(self, p: dict[str, Any]) -> Any:
        requested_path = _optional_string(p, "path")
        content_path = _content_path(requested_path, allow_empty=True)
        ref = _optional_string(p, "ref")
        query = {"ref": _api_ref({"ref": ref}, "ref")} if ref else None
        return self.client.request(
            "GET",
            f"{self._repo_path(p)}/contents/{content_path}",
            params=query,
            required_permission="Contents: read",
        )

    def _file_text(self, p: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded UTF-8 file without making callers decode base64."""
        payload = self._contents(p)
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise AssistantError("مسیر انتخاب‌شده یک فایل معمولی نیست")
        try:
            size = int(payload.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise AssistantError("اندازهٔ فایل گزارش‌شده توسط GitHub نامعتبر است") from exc
        maximum = _bounded_integer(
            p,
            "max_bytes",
            default=256 * 1024,
            minimum=1,
            maximum=1024 * 1024,
        )
        if size < 0 or size > maximum:
            raise AssistantError(f"فایل برای نمایش متنی از سقف {maximum} بایت بزرگ‌تر است")
        encoded = payload.get("content")
        if payload.get("encoding") != "base64" or not isinstance(encoded, str):
            raise AssistantError("محتوای متنی فایل در پاسخ GitHub موجود نیست")
        compact = "".join(encoded.split())
        if len(compact) > ((maximum + 2) // 3) * 4 + 4:
            raise AssistantError("پاسخ فایل از سقف امن بزرگ‌تر است")
        try:
            raw = base64.b64decode(compact, validate=True)
            text = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AssistantError("فایل UTF-8 متنی نیست") from exc
        if len(raw) > maximum or "\x00" in text:
            raise AssistantError("فایل متنی نامعتبر یا بیش از حد بزرگ است")
        return {
            "path": payload.get("path", _optional_string(p, "path")),
            "name": payload.get("name", ""),
            "sha": payload.get("sha", ""),
            "size": len(raw),
            "text": text,
            "html_url": payload.get("html_url", ""),
        }

    def _repository_tree(self, p: dict[str, Any]) -> dict[str, Any]:
        """Inspect a whole repository tree in one bounded GitHub API call."""
        raw_ref = _optional_string(p, "ref")
        treeish = _api_ref({"ref": raw_ref}, "ref") if raw_ref else ""
        if not treeish:
            repository = self._repo_get(p)
            treeish = str(repository.get("default_branch") or "").strip()
        if not treeish or len(treeish.encode("utf-8")) > 255:
            raise AssistantError("ref درخت مخزن نامعتبر است")
        payload = self.client.request(
            "GET",
            f"{self._repo_path(p)}/git/trees/{quote(treeish, safe='')}",
            params={"recursive": "1"},
            required_permission="Contents: read",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise AssistantError("قالب درخت مخزن GitHub ناشناخته است")
        limit = _limit(p)
        tree = payload["tree"]
        return {
            "sha": payload.get("sha", ""),
            "tree": tree[:limit],
            "count": min(len(tree), limit),
            "truncated": bool(payload.get("truncated")) or len(tree) > limit,
        }

    def _workflow_runs(self, p: dict[str, Any]) -> dict[str, Any]:
        workflow = _optional_string(p, "workflow_id")
        suffix = (
            f"actions/workflows/{quote(workflow, safe='')}/runs"
            if workflow
            else "actions/runs"
        )
        query = _body(
            p,
            "actor",
            "branch",
            "event",
            "status",
            "created",
            "exclude_pull_requests",
            "check_suite_id",
            "head_sha",
        )
        return self._repo_page(
            p, suffix, query=query, item_key="workflow_runs", permission="Actions: read"
        )

    def _search(self, p: dict[str, Any]) -> dict[str, Any]:
        kind = _enum(p, "type", _SEARCH_TYPES)
        query = _required(p, "query")
        return self.client.paginate(
            f"/search/{kind}",
            params={
                "q": query,
                "sort": _optional_string(p, "sort") or None,
                "order": _enum_default(p, "order", {"asc", "desc"}, "desc"),
            },
            item_key="items",
            max_items=_limit(p),
            required_permission="Endpoint-dependent read permission",
        )

    def _release(self, p: dict[str, Any]) -> dict[str, Any]:
        repo_path = self._repo_path(p)
        if p.get("release_id") is not None:
            suffix = str(_positive_id(p, "release_id"))
        elif _optional_string(p, "tag"):
            suffix = f"tags/{quote(_required(p, 'tag'), safe='')}"
        elif _boolean(p, "latest", default=False):
            suffix = "latest"
        else:
            raise AssistantError("یکی از release_id، tag یا latest برای release الزامی است")
        return self.client.request(
            "GET", f"{repo_path}/releases/{suffix}", required_permission="Contents: read"
        )

    def _local_clone(self, p: dict[str, Any]) -> Any:
        owner, repo = _name(p, "owner"), _name(p, "repo")
        # Reuse repository selection enforcement before starting Git.
        self._repo_path({"owner": owner, "repo": repo})
        destination = _optional_string(p, "destination")
        return self.git.clone(owner, repo, destination=destination or None)

    def _local_commit(self, p: dict[str, Any]) -> Any:
        author_name = _optional_string(p, "author_name").strip()
        author_email = _optional_string(p, "author_email").strip()
        if not author_name or not author_email:
            account = self.account()
            login = str(account.get("login") or "github-user")
            account_id = account.get("id")
            noreply_local = f"{account_id}+{login}" if isinstance(account_id, int) and account_id > 0 else login
            author_name = author_name or str(account.get("name") or login)
            author_email = author_email or f"{noreply_local}@users.noreply.github.com"
        return self.git.commit(
            _required(p, "path"),
            _required(p, "message"),
            paths=p.get("paths"),
            all_tracked=_boolean(p, "all_tracked", default=False),
            author_name=author_name,
            author_email=author_email,
        )

    def _repository_create(self, p: dict[str, Any]) -> Any:
        body = _required_body(
            p,
            "name",
            optional=(
                "description",
                "homepage",
                "private",
                "visibility",
                "has_issues",
                "has_projects",
                "has_wiki",
                "is_template",
                "auto_init",
                "gitignore_template",
                "license_template",
                "allow_squash_merge",
                "allow_merge_commit",
                "allow_rebase_merge",
                "delete_branch_on_merge",
            ),
        )
        org = _optional_string(p, "org").strip()
        result = self.client.request(
            "POST",
            f"/orgs/{_name({'org': org}, 'org')}/repos" if org else "/user/repos",
            json_body=body,
            required_permission="Administration: write",
        )
        return self._register_created_repository(result, action="ساخت")

    def _fork(self, p: dict[str, Any]) -> dict[str, Any]:
        result = self.client.request(
            "POST",
            f"{self._repo_path(p)}/forks",
            json_body=_body(p, "organization", "name", "default_branch_only"),
            required_permission="Contents: read",
        )
        return self._register_created_repository(result, action="Fork")

    def _register_created_repository(self, result: Any, *, action: str) -> dict[str, Any]:
        """Immediately bind a newly created repository to the local allow-list."""
        if not isinstance(result, dict):
            raise AssistantError(f"قالب پاسخ {action} مخزن GitHub ناشناخته است")
        full_name = str(result.get("full_name") or "")
        parts = full_name.split("/")
        if len(parts) != 2 or any(not _NAME_RE.fullmatch(part) for part in parts):
            raise AssistantError(
                f"عملیات {action} مخزن در GitHub انجام شد، اما شناسهٔ معتبر مخزن برنگشت"
            )
        folded = full_name.casefold()
        self._session_repositories.add(folded)
        if self.git.allowed_repositories is not None:
            self.git.allowed_repositories.add(folded)
        if self._repository_created_callback is not None:
            try:
                self._repository_created_callback(full_name)
            except AssistantError as exc:
                # The remote mutation has already succeeded and cannot be
                # rolled back safely. Keep it usable for this process and make
                # the persistence problem explicit to the caller.
                result = dict(result)
                result["selection_warning"] = str(exc)
        return result

    def _file_upsert(self, p: dict[str, Any]) -> Any:
        content = _required(p, "content")
        if len(content.encode("utf-8")) > 5 * 1024 * 1024:
            raise AssistantError("ویرایش مستقیم فایل به ۵ مگابایت محدود است")
        path = _content_path(_required(p, "path"))
        body = {
            "message": _required(p, "message"),
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            **_body(p, "sha", "branch", "committer", "author"),
        }
        return self.client.request(
            "PUT",
            f"{self._repo_path(p)}/contents/{path}",
            json_body=body,
            required_permission="Contents: write; Workflows: write for .github/workflows",
        )

    def _file_delete(self, p: dict[str, Any]) -> Any:
        path = _content_path(_required(p, "path"))
        body = _required_body(p, "message", "sha", optional=("branch", "committer", "author"))
        return self.client.request(
            "DELETE",
            f"{self._repo_path(p)}/contents/{path}",
            json_body=body,
            required_permission="Contents: write; Workflows: write for .github/workflows",
        )

    def _branch_create(self, p: dict[str, Any]) -> Any:
        source = _required(p, "source_sha")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", source):
            raise AssistantError("source_sha باید SHA کامل ۴۰ نویسه‌ای باشد")
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/git/refs",
            json_body={"ref": f"refs/heads/{_required(p, 'branch')}", "sha": source},
            required_permission="Contents: write",
        )

    def _workflow_dispatch(self, p: dict[str, Any]) -> Any:
        workflow = quote(_required(p, "workflow_id"), safe="")
        inputs = p.get("inputs", {})
        if not isinstance(inputs, dict):
            raise AssistantError("inputs اجرای Workflow باید یک شیء JSON باشد")
        _bounded_json(inputs, "ورودی‌های Workflow", maximum=64 * 1024)
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/actions/workflows/{workflow}/dispatches",
            json_body={"ref": _api_ref(p, "ref"), "inputs": inputs},
            required_permission="Actions: write",
        )

    def _workflow_run_command(self, p: dict[str, Any], command: str) -> Any:
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/actions/runs/{_positive_id(p, 'run_id')}/{command}",
            required_permission="Actions: write",
        )

    def _workflow_toggle(self, p: dict[str, Any], *, enable: bool) -> Any:
        workflow = quote(_path_name(p, "workflow_id"), safe="")
        command = "enable" if enable else "disable"
        return self.client.request(
            "PUT",
            f"{self._repo_path(p)}/actions/workflows/{workflow}/{command}",
            required_permission="Actions: write",
        )

    def _actions_cache_delete(self, p: dict[str, Any]) -> Any:
        cache_id = p.get("cache_id")
        if cache_id is not None:
            suffix = str(_positive_id(p, "cache_id"))
            params = None
        else:
            key = _required(p, "key")
            if len(key.encode("utf-8")) > 512:
                raise AssistantError("کلید cache بیش از حد بلند است")
            suffix = ""
            params = {"key": key, **_body(p, "ref")}
        return self.client.request(
            "DELETE",
            f"{self._repo_path(p)}/actions/caches" + (f"/{suffix}" if suffix else ""),
            params=params,
            required_permission="Actions: write",
        )

    def _environment_path(self, p: dict[str, Any]) -> str:
        environment = quote(_path_name(p, "environment"), safe="")
        return f"{self._repo_path(p)}/environments/{environment}"

    def _encrypt_actions_secret(self, public_key_path: str, value: str, permission: str) -> dict[str, str]:
        try:
            from nacl import encoding, public
        except ImportError as exc:
            raise AssistantError("برای ثبت secret بستهٔ PyNaCl لازم است (افزونهٔ github)") from exc
        if len(value.encode("utf-8")) > 48 * 1024:
            raise AssistantError("مقدار Secret بیش از سقف ۴۸ کیلوبایتی GitHub است")
        key = self.client.request(
            "GET", public_key_path, required_permission=permission
        )
        if not isinstance(key, dict) or not isinstance(key.get("key"), str) or not key.get("key_id"):
            raise AssistantError("کلید عمومی Secret در پاسخ GitHub معتبر نیست")
        try:
            box = public.SealedBox(
                public.PublicKey(key["key"].encode("ascii"), encoding.Base64Encoder())
            )
            encrypted = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
        except (ValueError, TypeError) as exc:
            raise AssistantError("کلید عمومی Secret دریافتی از GitHub معتبر نیست") from exc
        return {"encrypted_value": encrypted, "key_id": str(key["key_id"])}

    def _scoped_actions_secret_set(self, p: dict[str, Any], *, scope: str) -> Any:
        name, value = _secret_name(p), _required(p, "value")
        if scope == "organization":
            base = f"/orgs/{_name(p, 'org')}/actions"
            permission = "Organization Secrets: write"
            body: dict[str, Any] = {
                **self._encrypt_actions_secret(f"{base}/secrets/public-key", value, permission),
                "visibility": _enum(p, "visibility", {"all", "private", "selected"}),
            }
            if body["visibility"] == "selected":
                body["selected_repository_ids"] = _positive_id_list(p, "selected_repository_ids")
        elif scope == "environment":
            base = self._environment_path(p)
            permission = "Environments: write"
            body = self._encrypt_actions_secret(f"{base}/secrets/public-key", value, permission)
        else:  # pragma: no cover - internal programming invariant
            raise ValueError("Unknown Actions secret scope")
        return self.client.request(
            "PUT",
            f"{base}/secrets/{name}",
            json_body=body,
            required_permission=permission,
        )

    def _scoped_actions_variable_set(self, p: dict[str, Any], *, scope: str) -> Any:
        name, value = _secret_name(p), _required(p, "value")
        if len(value.encode("utf-8")) > 48 * 1024:
            raise AssistantError("مقدار Variable بیش از سقف ۴۸ کیلوبایتی GitHub است")
        if scope == "organization":
            base = f"/orgs/{_name(p, 'org')}/actions/variables"
            permission = "Organization Variables: write"
            body: dict[str, Any] = {
                "name": name,
                "value": value,
                "visibility": _enum(p, "visibility", {"all", "private", "selected"}),
            }
            if body["visibility"] == "selected":
                body["selected_repository_ids"] = _positive_id_list(p, "selected_repository_ids")
        elif scope == "environment":
            base = f"{self._environment_path(p)}/variables"
            permission = "Environments: write"
            body = {"name": name, "value": value}
        else:  # pragma: no cover - internal programming invariant
            raise ValueError("Unknown Actions variable scope")
        exists = True
        try:
            self.client.request("GET", f"{base}/{name}", required_permission=permission)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                exists = False
            else:
                raise
        return self.client.request(
            "PATCH" if exists else "POST",
            f"{base}/{name}" if exists else base,
            json_body=body,
            required_permission=permission,
        )

    def _actions_secret_set(self, p: dict[str, Any]) -> Any:
        repo_path = self._repo_path(p)
        body = self._encrypt_actions_secret(
            f"{repo_path}/actions/secrets/public-key",
            _required(p, "value"),
            "Secrets: write",
        )
        return self.client.request(
            "PUT",
            f"{repo_path}/actions/secrets/{_secret_name(p)}",
            json_body=body,
            required_permission="Secrets: write",
        )

    def _actions_variable_set(self, p: dict[str, Any]) -> Any:
        name, value = _secret_name(p), _required(p, "value")
        if len(value.encode("utf-8")) > 48 * 1024:
            raise AssistantError("مقدار Variable بیش از سقف ۴۸ کیلوبایتی GitHub است")
        exists = True
        try:
            self.client.request(
                "GET",
                f"{self._repo_path(p)}/actions/variables/{name}",
                required_permission="Variables: read",
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                exists = False
            else:
                raise
        method = "PATCH" if exists else "POST"
        path = (
            f"{self._repo_path(p)}/actions/variables/{name}"
            if exists
            else f"{self._repo_path(p)}/actions/variables"
        )
        return self.client.request(
            method,
            path,
            json_body={"name": name, "value": value},
            required_permission="Variables: write",
        )

    def _notification_mark(self, p: dict[str, Any]) -> Any:
        thread_id = p.get("thread_id")
        if thread_id is not None:
            return self.client.request(
                "PATCH",
                f"/notifications/threads/{_positive_id(p, 'thread_id')}",
                required_permission="Notifications: write",
            )
        return self.client.request(
            "PUT",
            "/notifications",
            json_body=_body(p, "last_read_at", "read"),
            required_permission="Notifications: write",
        )

    def _webhook_config(self, p: dict[str, Any], *, require_url: bool) -> dict[str, str]:
        raw_url = (
            _required(p, "url").strip()
            if require_url
            else _optional_string(p, "url").strip()
        )
        config: dict[str, str] = {}
        if raw_url:
            try:
                parsed = urlparse(raw_url)
                port = parsed.port
            except ValueError as exc:
                raise AssistantError("URL وب‌هوک نامعتبر است") from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or port is not None and not 1 <= port <= 65535
                or len(raw_url) > 2048
            ):
                raise AssistantError("URL وب‌هوک باید HTTPS معتبر و بدون credential یا fragment باشد")
            config["url"] = raw_url
        if "content_type" in p:
            config["content_type"] = _enum(p, "content_type", {"json", "form"})
        if "secret" in p:
            secret = _optional_string(p, "secret")
            if not secret or len(secret.encode("utf-8")) > 1024 or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in secret
            ):
                raise AssistantError("Secret وب‌هوک نامعتبر است")
            config["secret"] = secret
        config["insecure_ssl"] = "0"
        return config

    def _webhook_create(self, p: dict[str, Any]) -> Any:
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/hooks",
            json_body={
                "name": "web",
                "active": _boolean(p, "active", default=True),
                "events": _string_list(p, "events") if "events" in p else ["push"],
                "config": self._webhook_config(p, require_url=True),
            },
            required_permission="Webhooks: write",
        )

    def _webhook_update(self, p: dict[str, Any]) -> Any:
        body: dict[str, Any] = {}
        if "active" in p:
            body["active"] = _boolean(p, "active", default=True)
        for field in ("events", "add_events", "remove_events"):
            if field in p:
                body[field] = _string_list(p, field)
        config = self._webhook_config(p, require_url=False)
        if config.keys() != {"insecure_ssl"} or any(key in p for key in ("url", "content_type", "secret")):
            body["config"] = config
        if not body:
            raise AssistantError("برای ویرایش webhook حداقل یک فیلد لازم است")
        return self.client.request(
            "PATCH",
            f"{self._repo_path(p)}/hooks/{_positive_id(p, 'hook_id')}",
            json_body=body,
            required_permission="Webhooks: write",
        )

    def _codespace_create(self, p: dict[str, Any]) -> Any:
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/codespaces",
            json_body=_required_body(
                p,
                "ref",
                optional=(
                    "machine",
                    "location",
                    "devcontainer_path",
                    "working_directory",
                    "idle_timeout_minutes",
                    "display_name",
                    "retention_period_minutes",
                    "multi_repo_permissions_opt_out",
                ),
            ),
            required_permission="Codespaces: write",
        )

    def _codespace_command(self, p: dict[str, Any], command: str) -> Any:
        return self.client.request(
            "POST",
            f"/user/codespaces/{quote(_path_name(p, 'codespace_name'), safe='')}/{command}",
            required_permission="Codespaces: write",
        )

    def _codespace_secret_set(self, p: dict[str, Any]) -> Any:
        body: dict[str, Any] = self._encrypt_actions_secret(
            "/user/codespaces/secrets/public-key",
            _required(p, "value"),
            "Codespaces: write",
        )
        if "selected_repository_ids" in p:
            body["selected_repository_ids"] = _positive_id_list(
                p, "selected_repository_ids", allow_empty=True
            )
        return self.client.request(
            "PUT",
            f"/user/codespaces/secrets/{_secret_name(p)}",
            json_body=body,
            required_permission="Codespaces: write",
        )

    def _package_base(self, p: dict[str, Any]) -> str:
        package_type = _enum(p, "package_type", {"npm", "maven", "rubygems", "docker", "nuget", "container"})
        owner = _name(p, "owner")
        owner_type = _enum(p, "owner_type", {"user", "organization"})
        prefix = f"/users/{owner}" if owner_type == "user" else f"/orgs/{owner}"
        name = p.get("package_name")
        suffix = (
            f"/{quote(_path_name(p, 'package_name'), safe='')}"
            if name is not None
            else ""
        )
        return f"{prefix}/packages/{package_type}{suffix}"

    def _packages(self, p: dict[str, Any]) -> dict[str, Any]:
        base = self._package_base({key: value for key, value in p.items() if key != "package_name"})
        visibility = (
            _enum(p, "visibility", {"public", "private", "internal"})
            if p.get("visibility") is not None
            else None
        )
        return self.client.paginate(
            base,
            params={"visibility": visibility},
            max_items=_limit(p),
            required_permission="Packages: read",
        )

    def _package_versions(self, p: dict[str, Any]) -> dict[str, Any]:
        state = _enum(p, "state", {"active", "deleted"}) if "state" in p else "active"
        return self.client.paginate(
            f"{self._package_base(p)}/versions",
            params={"state": state},
            max_items=_limit(p),
            required_permission="Packages: read",
        )

    def _package_version_command(self, p: dict[str, Any], method: str) -> Any:
        path = f"{self._package_base(p)}/versions/{_positive_id(p, 'package_version_id')}"
        if method == "POST":
            path += "/restore"
        return self.client.request(method, path, required_permission="Packages: write")

    def _dependabot_alert_update(self, p: dict[str, Any]) -> Any:
        body = _body(p, "dismissed_comment")
        state = p.get("state")
        if state is not None:
            body["state"] = _enum(p, "state", {"dismissed", "open"})
        reason = p.get("dismissed_reason")
        if reason is not None:
            body["dismissed_reason"] = _enum(
                p,
                "dismissed_reason",
                {"fix_started", "inaccurate", "no_bandwidth", "not_used", "tolerable_risk"},
            )
        if state == "dismissed" and reason is None:
            raise AssistantError("dismissed_reason برای بستن هشدار Dependabot الزامی است")
        if "assignees" in p:
            body["assignees"] = _string_list(p, "assignees")
        if not body:
            raise AssistantError("برای ویرایش هشدار Dependabot حداقل یک فیلد لازم است")
        return self.client.request(
            "PATCH",
            f"{self._repo_path(p)}/dependabot/alerts/{_positive_id(p, 'alert_number')}",
            json_body=_bounded_json(body, "ویرایش هشدار Dependabot", maximum=64 * 1024),
            required_permission="Dependabot alerts: write",
        )

    def _code_scanning_alert_update(self, p: dict[str, Any]) -> Any:
        body = _body(p, "dismissed_comment")
        state = p.get("state")
        if state is not None:
            body["state"] = _enum(p, "state", {"dismissed", "open"})
        reason = p.get("dismissed_reason")
        if reason is not None:
            body["dismissed_reason"] = _enum(
                p,
                "dismissed_reason",
                {"false positive", "used in tests", "won't fix"},
            )
        if state == "dismissed" and reason is None:
            raise AssistantError("dismissed_reason برای بستن هشدار Code Scanning الزامی است")
        if "create_request" in p:
            body["create_request"] = _boolean(p, "create_request", default=False)
        if "assignees" in p:
            body["assignees"] = _string_list(p, "assignees")
        if not body:
            raise AssistantError("برای ویرایش هشدار Code Scanning حداقل یک فیلد لازم است")
        return self.client.request(
            "PATCH",
            f"{self._repo_path(p)}/code-scanning/alerts/{_positive_id(p, 'alert_number')}",
            json_body=_bounded_json(body, "ویرایش هشدار Code Scanning", maximum=64 * 1024),
            required_permission="Code scanning alerts: write",
        )

    def _secret_scanning_alert_update(self, p: dict[str, Any]) -> Any:
        body = _body(p, "resolution_comment")
        state = p.get("state")
        if state is not None:
            body["state"] = _enum(p, "state", {"open", "resolved"})
        resolution = p.get("resolution")
        if resolution is not None:
            body["resolution"] = _enum(
                p,
                "resolution",
                {"false_positive", "revoked", "used_in_tests", "wont_fix"},
            )
        if state == "resolved" and resolution is None:
            raise AssistantError("resolution برای بستن هشدار Secret Scanning الزامی است")
        if "assignee" in p:
            assignee = p["assignee"]
            if assignee is not None:
                assignee = _name({"assignee": assignee}, "assignee")
            body["assignee"] = assignee
        if "validity" in p:
            validity = p["validity"]
            body["validity"] = (
                None
                if validity is None
                else _enum(p, "validity", {"active", "inactive"})
            )
        if not body:
            raise AssistantError("برای ویرایش هشدار Secret Scanning حداقل یک فیلد لازم است")
        return self.client.request(
            "PATCH",
            f"{self._repo_path(p)}/secret-scanning/alerts/{_positive_id(p, 'alert_number')}",
            json_body=_bounded_json(body, "ویرایش هشدار Secret Scanning", maximum=64 * 1024),
            required_permission="Secret scanning alerts: write",
        )

    def _discussion_categories(self, p: dict[str, Any]) -> dict[str, Any]:
        self._repo_path(p)
        query = "query($owner:String!,$repo:String!,$first:Int!){repository(owner:$owner,name:$repo){id discussionCategories(first:$first){nodes{id name description emoji isAnswerable}}} rateLimit{limit remaining resetAt}}"
        return self.client.graphql(
            query,
            {"owner": _name(p, "owner"), "repo": _name(p, "repo"), "first": min(_limit(p), 100)},
        )

    def _discussions(self, p: dict[str, Any]) -> dict[str, Any]:
        self._repo_path(p)
        query = "query($owner:String!,$repo:String!,$first:Int!,$after:String){repository(owner:$owner,name:$repo){discussions(first:$first,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{id number title body url closed locked createdAt updatedAt author{login} category{id name emoji} comments{totalCount}} pageInfo{hasNextPage endCursor}}} rateLimit{limit remaining resetAt}}"
        return self.client.graphql(
            query,
            {
                "owner": _name(p, "owner"),
                "repo": _name(p, "repo"),
                "first": min(_limit(p), 100),
                "after": _optional_string(p, "after") or None,
            },
        )

    def _discussion(self, p: dict[str, Any]) -> dict[str, Any]:
        self._repo_path(p)
        query = "query($owner:String!,$repo:String!,$number:Int!,$first:Int!,$after:String){repository(owner:$owner,name:$repo){discussion(number:$number){id number title body url closed locked createdAt updatedAt author{login} category{id name emoji} comments(first:$first,after:$after){nodes{id body url createdAt updatedAt isAnswer author{login} replies(first:25){nodes{id body url createdAt author{login}}}} pageInfo{hasNextPage endCursor}}}} rateLimit{limit remaining resetAt}}"
        return self.client.graphql(
            query,
            {
                "owner": _name(p, "owner"),
                "repo": _name(p, "repo"),
                "number": _positive_id(p, "number"),
                "first": min(_limit(p), 100),
                "after": _optional_string(p, "after") or None,
            },
        )

    def _discussion_create(self, p: dict[str, Any]) -> dict[str, Any]:
        repo = self._repo_get(p)
        query = "mutation($repo:ID!,$category:ID!,$title:String!,$body:String!){createDiscussion(input:{repositoryId:$repo,categoryId:$category,title:$title,body:$body}){discussion{id number title url}}}"
        return self.client.graphql(
            query,
            {
                "repo": _required(repo, "node_id"),
                "category": _required(p, "category_id"),
                "title": _required(p, "title"),
                "body": _required(p, "body"),
            },
        )

    def _discussion_update(self, p: dict[str, Any]) -> dict[str, Any]:
        values = {"discussionId": _required(p, "discussion_id")}
        values.update(_body(p, "title", "body", "categoryId"))
        if len(values) == 1:
            raise AssistantError("برای ویرایش Discussion حداقل یک فیلد لازم است")
        query = "mutation($input:UpdateDiscussionInput!){updateDiscussion(input:$input){discussion{id number title body url}}}"
        return self.client.graphql(query, {"input": values})

    def _discussion_comment(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($discussion:ID!,$body:String!,$reply:ID){addDiscussionComment(input:{discussionId:$discussion,body:$body,replyToId:$reply}){comment{id body url}}}"
        return self.client.graphql(
            query,
            {
                "discussion": _required(p, "discussion_id"),
                "body": _required(p, "body"),
                "reply": _optional_string(p, "reply_to_id") or None,
            },
        )

    def _discussion_comment_update(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($id:ID!,$body:String!){updateDiscussionComment(input:{commentId:$id,body:$body}){comment{id body url}}}"
        return self.client.graphql(
            query, {"id": _required(p, "comment_id"), "body": _required(p, "body")}
        )

    def _discussion_state(self, p: dict[str, Any], *, close: bool) -> dict[str, Any]:
        if close:
            query = "mutation($id:ID!,$reason:DiscussionCloseReason){closeDiscussion(input:{discussionId:$id,reason:$reason}){discussion{id closed}}}"
            reason = (
                _enum(p, "reason", {"OUTDATED", "RESOLVED", "DUPLICATE"})
                if p.get("reason") is not None
                else None
            )
            variables = {"id": _required(p, "discussion_id"), "reason": reason}
        else:
            query = "mutation($id:ID!){reopenDiscussion(input:{discussionId:$id}){discussion{id closed}}}"
            variables = {"id": _required(p, "discussion_id")}
        return self.client.graphql(query, variables)

    def _graphql_node_mutation(
        self, mutation: str, input_name: str, node_id: str, result_field: str
    ) -> dict[str, Any]:
        allowed = {
            ("deleteDiscussion", "discussionId", "clientMutationId"),
            ("deleteDiscussionComment", "id", "clientMutationId"),
        }
        if (mutation, input_name, result_field) not in allowed:
            raise ValueError("GraphQL mutation is not allow-listed")
        query = f"mutation($id:ID!){{{mutation}(input:{{{input_name}:$id}}){{{result_field}}}}}"
        return self.client.graphql(query, {"id": node_id})

    def _check_run_create(self, p: dict[str, Any]) -> Any:
        head_sha = _required(p, "head_sha")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise AssistantError("head_sha باید SHA کامل ۴۰ نویسه‌ای باشد")
        body = {
            "name": _required(p, "name"),
            "head_sha": head_sha,
            **_body(
                p,
                "details_url",
                "external_id",
                "status",
                "started_at",
                "conclusion",
                "completed_at",
                "output",
                "actions",
            ),
        }
        _bounded_json(body, "بدنهٔ check run")
        return self.client.request(
            "POST",
            f"{self._repo_path(p)}/check-runs",
            json_body=body,
            required_permission="Checks: write",
        )

    def _check_run_update(self, p: dict[str, Any]) -> Any:
        body = _body(
            p,
            "name",
            "details_url",
            "external_id",
            "status",
            "started_at",
            "conclusion",
            "completed_at",
            "output",
            "actions",
        )
        if not body:
            raise AssistantError("برای ویرایش check run حداقل یک فیلد لازم است")
        _bounded_json(body, "بدنهٔ check run")
        return self.client.request(
            "PATCH",
            f"{self._repo_path(p)}/check-runs/{_positive_id(p, 'check_run_id')}",
            json_body=body,
            required_permission="Checks: write",
        )

    def _projects(self, p: dict[str, Any]) -> dict[str, Any]:
        login = _name(p, "owner")
        owner_type = _enum(p, "owner_type", {"user", "organization"})
        field = "user" if owner_type == "user" else "organization"
        query = f"query($login:String!,$first:Int!,$after:String){{{field}(login:$login){{id login projectsV2(first:$first,after:$after){{nodes{{id number title shortDescription public closed url updatedAt}} pageInfo{{hasNextPage endCursor}}}}}} rateLimit{{limit remaining resetAt}}}}"
        return self.client.graphql(
            query, {"login": login, "first": min(_limit(p), 100), "after": _optional_string(p, "after") or None}
        )

    def _project(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "query($id:ID!,$first:Int!,$after:String){node(id:$id){... on ProjectV2{id number title shortDescription public closed url fields(first:50){nodes{... on ProjectV2FieldCommon{id name dataType}}} items(first:$first,after:$after){nodes{id type content{... on Issue{id number title url state} ... on PullRequest{id number title url state} ... on DraftIssue{id title body}}} pageInfo{hasNextPage endCursor}}}} rateLimit{limit remaining resetAt}}"
        return self.client.graphql(
            query,
            {
                "id": _required(p, "project_id"),
                "first": min(_limit(p), 100),
                "after": _optional_string(p, "after") or None,
            },
        )

    def _project_create(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($owner:ID!,$title:String!,$repo:ID){createProjectV2(input:{ownerId:$owner,title:$title,repositoryId:$repo}){projectV2{id number title url}}}"
        return self.client.graphql(
            query,
            {
                "owner": _required(p, "owner_id"),
                "title": _required(p, "title"),
                "repo": _optional_string(p, "repository_id") or None,
            },
        )

    def _project_update(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($input:UpdateProjectV2Input!){updateProjectV2(input:$input){projectV2{id title shortDescription public closed url}}}"
        values = {"projectId": _required(p, "project_id")}
        values.update(_body(p, "title", "shortDescription", "public", "closed"))
        if len(values) == 1:
            raise AssistantError("برای ویرایش Project حداقل یک فیلد لازم است")
        return self.client.graphql(query, {"input": values})

    def _project_add_item(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($project:ID!,$content:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id type}}}"
        return self.client.graphql(
            query, {"project": _required(p, "project_id"), "content": _required(p, "content_id")}
        )

    def _project_delete(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($id:ID!){deleteProjectV2(input:{projectId:$id}){projectV2{id title}}}"
        return self.client.graphql(query, {"id": _required(p, "project_id")})

    def _project_add_draft_issue(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($project:ID!,$title:String!,$body:String){addProjectV2DraftIssue(input:{projectId:$project,title:$title,body:$body}){projectV2Item{id type content{... on DraftIssue{id title body}}}}}"
        return self.client.graphql(
            query,
            {
                "project": _required(p, "project_id"),
                "title": _required(p, "title"),
                "body": _optional_string(p, "body") or None,
            },
        )

    def _project_update_draft_issue(self, p: dict[str, Any]) -> dict[str, Any]:
        values = {"draftIssueId": _required(p, "draft_issue_id")}
        values.update(_body(p, "title", "body"))
        if len(values) == 1:
            raise AssistantError("برای ویرایش draft issue حداقل title یا body لازم است")
        query = "mutation($input:UpdateProjectV2DraftIssueInput!){updateProjectV2DraftIssue(input:$input){draftIssue{id title body}}}"
        return self.client.graphql(query, {"input": values})

    def _project_archive_item(
        self, p: dict[str, Any], *, archive: bool = True
    ) -> dict[str, Any]:
        mutation = "archiveProjectV2Item" if archive else "unarchiveProjectV2Item"
        query = f"mutation($project:ID!,$item:ID!){{{mutation}(input:{{projectId:$project,itemId:$item}}){{item{{id isArchived}}}}}}"
        return self.client.graphql(
            query, {"project": _required(p, "project_id"), "item": _required(p, "item_id")}
        )

    def _project_delete_item(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($project:ID!,$item:ID!){deleteProjectV2Item(input:{projectId:$project,itemId:$item}){deletedItemId}}"
        return self.client.graphql(
            query, {"project": _required(p, "project_id"), "item": _required(p, "item_id")}
        )

    def _project_update_item_field(self, p: dict[str, Any]) -> dict[str, Any]:
        raw_value = p.get("value")
        if not isinstance(raw_value, dict) or len(raw_value) != 1:
            raise AssistantError("value باید شیء شامل دقیقاً یکی از text/number/date/singleSelectOptionId/iterationId باشد")
        allowed = {"text", "number", "date", "singleSelectOptionId", "iterationId"}
        if not set(raw_value).issubset(allowed):
            raise AssistantError("نوع مقدار فیلد Project پشتیبانی نمی‌شود")
        _bounded_json(raw_value, "مقدار فیلد Project", maximum=16 * 1024)
        query = "mutation($project:ID!,$item:ID!,$field:ID!,$value:ProjectV2FieldValue!){updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,value:$value}){projectV2Item{id}}}"
        return self.client.graphql(
            query,
            {
                "project": _required(p, "project_id"),
                "item": _required(p, "item_id"),
                "field": _required(p, "field_id"),
                "value": raw_value,
            },
        )

    def _project_clear_item_field(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($project:ID!,$item:ID!,$field:ID!){clearProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field}){projectV2Item{id}}}"
        return self.client.graphql(
            query,
            {
                "project": _required(p, "project_id"),
                "item": _required(p, "item_id"),
                "field": _required(p, "field_id"),
            },
        )

    def _project_update_item_position(self, p: dict[str, Any]) -> dict[str, Any]:
        query = "mutation($project:ID!,$item:ID!,$after:ID){updateProjectV2ItemPosition(input:{projectId:$project,itemId:$item,afterId:$after}){clientMutationId}}"
        return self.client.graphql(
            query,
            {
                "project": _required(p, "project_id"),
                "item": _required(p, "item_id"),
                "after": _optional_string(p, "after_id") or None,
            },
        )


def _name(p: dict[str, Any], key: str) -> str:
    raw = p.get(key)
    if not isinstance(raw, str):
        raise AssistantError(f"{key} نامعتبر است")
    value = raw.strip()
    if not _NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise AssistantError(f"{key} نامعتبر است")
    return value


def _path_name(p: dict[str, Any], key: str) -> str:
    value = _required(p, key).strip()
    if (
        not value
        or value in {".", ".."}
        or len(value.encode("utf-8")) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise AssistantError(f"{key} نامعتبر است")
    return value


def _content_path(value: str, *, allow_empty: bool = False) -> str:
    value = value.lstrip("/")
    if not value:
        if allow_empty:
            return ""
        raise AssistantError("path نامعتبر است")
    parts = value.split("/")
    if (
        len(value.encode("utf-8")) > 4096
        or any(not part or part in {".", ".."} for part in parts)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise AssistantError("path نامعتبر است")
    return quote(value, safe="/")


def _required(p: dict[str, Any], key: str) -> str:
    value = p.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AssistantError(f"پارامتر {key} باید رشتهٔ غیرخالی باشد")
    return value


def _optional_string(p: dict[str, Any], key: str, *, default: str = "") -> str:
    value = p.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise AssistantError(f"پارامتر {key} باید رشته باشد")
    return value


def _api_ref(p: dict[str, Any], key: str) -> str:
    value = _required(p, key).strip()
    if (
        len(value.encode("utf-8")) > 255
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise AssistantError(f"پارامتر {key} یک ref معتبر نیست")
    return value


def _boolean(p: dict[str, Any], key: str, *, default: bool) -> bool:
    value = p.get(key, default)
    if not isinstance(value, bool):
        raise AssistantError(f"{key} باید true یا false باشد")
    return value


def _positive_id(p: dict[str, Any], key: str) -> int:
    raw = p.get(key)
    if isinstance(raw, bool) or not (
        isinstance(raw, int) or isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]*", raw)
    ):
        raise AssistantError(f"{key} باید شناسهٔ عددی باشد")
    value = int(raw)
    if value < 1 or value > 9_223_372_036_854_775_807:
        raise AssistantError(f"{key} باید شناسهٔ مثبت معتبر باشد")
    return value


def _bounded_integer(
    p: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = p.get(key, default)
    if isinstance(raw, bool) or not (
        isinstance(raw, int) or isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw)
    ):
        raise AssistantError(f"{key} باید عدد صحیح بین {minimum} و {maximum} باشد")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise AssistantError(f"{key} باید عدد صحیح بین {minimum} و {maximum} باشد")
    return value


def _limit(p: dict[str, Any]) -> int:
    return _bounded_integer(p, "limit", default=100, minimum=1, maximum=2_000)


def _enum(p: dict[str, Any], key: str, choices: set[str]) -> str:
    value = p.get(key)
    if not isinstance(value, str) or value not in choices:
        raise AssistantError(f"{key} باید یکی از {', '.join(sorted(choices))} باشد")
    return value


def _enum_default(
    p: dict[str, Any], key: str, choices: set[str], default: str
) -> str:
    if key not in p or p[key] is None:
        return default
    return _enum(p, key, choices)


def _affiliations(p: dict[str, Any]) -> str:
    value = _optional_string(
        p,
        "affiliation",
        default="owner,collaborator,organization_member",
    )
    parts = value.split(",")
    allowed = {"owner", "collaborator", "organization_member"}
    if not parts or len(parts) > len(allowed) or any(part not in allowed for part in parts):
        raise AssistantError("affiliation شامل مقدار نامعتبر است")
    return ",".join(dict.fromkeys(parts))


_BOOLEAN_BODY_FIELDS = frozenset(
    {
        "private",
        "has_issues",
        "has_projects",
        "has_wiki",
        "is_template",
        "auto_init",
        "allow_squash_merge",
        "allow_merge_commit",
        "allow_rebase_merge",
        "delete_branch_on_merge",
        "archived",
        "default_branch_only",
        "draft",
        "maintainer_can_modify",
        "prerelease",
        "generate_release_notes",
        "auto_merge",
        "transient_environment",
        "production_environment",
        "auto_inactive",
        "prevent_self_review",
        "read",
        "exclude_pull_requests",
        "public",
        "closed",
        "multi_repo_permissions_opt_out",
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "block_creations",
        "required_conversation_resolution",
        "lock_branch",
        "allow_fork_syncing",
    }
)


def _body(p: dict[str, Any], *keys: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key in keys:
        if key not in p or p[key] is None:
            continue
        body[key] = (
            _boolean(p, key, default=False)
            if key in _BOOLEAN_BODY_FIELDS
            else p[key]
        )
    return body


def _update_body(p: dict[str, Any], subject: str, *keys: str) -> dict[str, Any]:
    body = _body(p, *keys)
    if not body:
        raise AssistantError(f"برای ویرایش {subject} حداقل یک فیلد لازم است")
    return body


def _required_body(
    p: dict[str, Any], *required: str, optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    body = {key: _required(p, key) for key in required}
    body.update(_body(p, *optional))
    return body


def _string_list(p: dict[str, Any], key: str) -> list[str]:
    value = p.get(key)
    if (
        not isinstance(value, list)
        or len(value) > 1_000
        or not all(
            isinstance(item, str)
            and 0 < len(item.encode("utf-8")) <= 512
            and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in item)
            for item in value
        )
    ):
        raise AssistantError(f"{key} باید فهرست حداکثر ۱۰۰۰ رشتهٔ معتبر باشد")
    return value


def _positive_id_list(
    p: dict[str, Any], key: str, *, allow_empty: bool = False
) -> list[int]:
    value = p.get(key)
    if (
        not isinstance(value, list)
        or not allow_empty and not value
        or len(value) > 1_000
    ):
        requirement = "فهرست شناسه‌های مثبت" if allow_empty else "فهرست غیرخالی از شناسه‌های مثبت"
        raise AssistantError(f"{key} باید {requirement} باشد")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not (
            isinstance(item, int)
            or isinstance(item, str) and re.fullmatch(r"[1-9][0-9]*", item)
        ):
            raise AssistantError(f"{key} باید فهرست شناسه‌های مثبت باشد")
        number = int(item)
        if number < 1 or number > 9_223_372_036_854_775_807:
            raise AssistantError(f"{key} باید فهرست شناسه‌های مثبت باشد")
        result.append(number)
    return result


def _bounded_json(value: Any, label: str, *, maximum: int = 1024 * 1024) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AssistantError(f"{label} باید JSON معتبر باشد") from exc
    if len(encoded) > maximum:
        raise AssistantError(f"{label} بیش از سقف مجاز است")
    return value


def _branch_protection_body(p: dict[str, Any]) -> dict[str, Any]:
    required = (
        "required_status_checks",
        "enforce_admins",
        "required_pull_request_reviews",
        "restrictions",
    )
    missing = [key for key in required if key not in p]
    if missing:
        raise AssistantError("تنظیم branch protection باید چهار فیلد اصلی را صریحاً شامل شود")
    body = {key: p[key] for key in required}
    if body["enforce_admins"] is not None and not isinstance(body["enforce_admins"], bool):
        raise AssistantError("enforce_admins باید true، false یا null باشد")
    body.update(
        _body(
            p,
            "required_linear_history",
            "allow_force_pushes",
            "allow_deletions",
            "block_creations",
            "required_conversation_resolution",
            "lock_branch",
            "allow_fork_syncing",
        )
    )
    return _bounded_json(body, "تنظیم branch protection")


def _ruleset_body(p: dict[str, Any], *, create: bool) -> dict[str, Any]:
    keys = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
    body = _body(p, *keys)
    if create:
        body["name"] = _required(p, "name")
        body["target"] = _enum(p, "target", {"branch", "tag", "push"})
        body["enforcement"] = _enum(p, "enforcement", {"disabled", "active", "evaluate"})
        if "rules" not in p or not isinstance(p["rules"], list):
            raise AssistantError("rules در ساخت ruleset باید فهرست باشد")
    else:
        if "name" in body:
            body["name"] = _required(p, "name")
        if "target" in body:
            body["target"] = _enum(p, "target", {"branch", "tag", "push"})
        if "enforcement" in body:
            body["enforcement"] = _enum(p, "enforcement", {"disabled", "active", "evaluate"})
        if "rules" in body and not isinstance(body["rules"], list):
            raise AssistantError("rules در ویرایش ruleset باید فهرست باشد")
        if not body:
            raise AssistantError("برای ویرایش ruleset حداقل یک فیلد لازم است")
    return _bounded_json(body, "تنظیم ruleset")


def _secret_name(p: dict[str, Any]) -> str:
    value = _required(p, "name").upper()
    if len(value) > 100 or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise AssistantError("نام secret/variable نامعتبر است")
    return value


def _pick(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys}
