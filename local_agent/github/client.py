"""GitHub API client for the local assistant.

Supports two authentication methods:
  1. **Personal Access Token (PAT)** — set ``github_token`` in config.json
     or ``LOCAL_AGENT_GITHUB__TOKEN`` env var.
  2. **Device Flow** (OAuth 2.0 Device Authorization Grant) — interactive
     login that shows a user-code and opens the browser.  The token is
     persisted in ``<data_dir>/github_token.json``.

Uses ``PyGithub`` for the heavy lifting and falls back to raw ``requests``
for endpoints PyGithub does not cover (search, notifications, etc.).
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("github")


class GitHubError(AssistantError):
    """A user-facing failure from the GitHub client."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RepoInfo:
    full_name: str
    description: str = ""
    language: str = ""
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    is_private: bool = False
    is_fork: bool = False
    default_branch: str = "main"
    updated_at: str = ""
    url: str = ""
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "description": self.description,
            "language": self.language,
            "stars": self.stars,
            "forks": self.forks,
            "open_issues": self.open_issues,
            "is_private": self.is_private,
            "is_fork": self.is_fork,
            "default_branch": self.default_branch,
            "updated_at": self.updated_at,
            "url": self.url,
            "topics": self.topics,
        }


@dataclass
class IssueInfo:
    number: int
    title: str
    state: str
    body: str = ""
    user: str = ""
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    comments: int = 0
    created_at: str = ""
    updated_at: str = ""
    url: str = ""
    is_pull_request: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "body": self.body[:500],
            "user": self.user,
            "labels": self.labels,
            "assignees": self.assignees,
            "comments": self.comments,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "url": self.url,
            "is_pull_request": self.is_pull_request,
        }


@dataclass
class PRInfo:
    number: int
    title: str
    state: str
    user: str = ""
    head: str = ""
    base: str = ""
    body: str = ""
    merged: bool = False
    mergeable: bool | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    comments: int = 0
    review_comments: int = 0
    created_at: str = ""
    updated_at: str = ""
    url: str = ""
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "user": self.user,
            "head": self.head,
            "base": self.base,
            "body": self.body[:500],
            "merged": self.merged,
            "mergeable": self.mergeable,
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_files": self.changed_files,
            "comments": self.comments,
            "review_comments": self.review_comments,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "url": self.url,
            "labels": self.labels,
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin wrapper around the GitHub REST API v3.

    Uses ``PyGithub`` when available for convenience and falls back to
    raw ``requests`` calls otherwise.  The token is never logged.
    """

    def __init__(
        self,
        *,
        token: str = "",
        token_path: Path | None = None,
    ) -> None:
        self._token = token.strip()
        self._token_path = token_path
        self._user: dict[str, Any] | None = None
        self._lock = threading.RLock()
        # Try to load persisted token if none was provided.
        if not self._token and self._token_path and self._token_path.is_file():
            try:
                data = json.loads(self._token_path.read_text(encoding="utf-8"))
                self._token = str(data.get("access_token", "")).strip()
            except Exception:
                pass

    # -------------------------------------------------------- Auth

    @property
    def is_authenticated(self) -> bool:
        return bool(self._token)

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        """Set the access token and persist it."""
        with self._lock:
            self._token = token.strip()
            self._user = None
            if self._token_path and self._token:
                self._token_path.parent.mkdir(parents=True, exist_ok=True)
                self._token_path.write_text(
                    json.dumps({"access_token": self._token,
                                "saved_at": datetime.now().isoformat()},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    def clear_token(self) -> None:
        """Remove the stored token."""
        with self._lock:
            self._token = ""
            self._user = None
            if self._token_path and self._token_path.is_file():
                self._token_path.unlink(missing_ok=True)

    # -------------------------------------------------------- Device Flow

    def start_device_flow(self, client_id: str = "", scopes: str = "repo,read:user,notifications") -> dict[str, Any]:
        """Begin the GitHub Device Authorization Grant (RFC 8628).

        Returns ``{"user_code": ..., "verification_uri": ..., "device_code": ...}``
        so the caller can display instructions to the user.  Call
        :meth:`poll_device_flow` afterwards to wait for completion.
        """
        if not client_id:
            raise GitHubError(
                "GitHub OAuth client_id تنظیم نشده است. "
                "یک OAuth App در GitHub بسازید و client_id را در config.json تنظیم کنید. "
                "یا از Personal Access Token استفاده کنید."
            )
        try:
            response = requests.post(
                "https://github.com/login/device/code",
                data={"client_id": client_id, "scope": scopes},
                headers={"Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise GitHubError(f"شروع Device Flow ناموفق بود: {exc}") from exc

        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri", "https://github.com/login/device"),
            "expires_in": data.get("expires_in", 900),
            "interval": data.get("interval", 5),
            "client_id": client_id,
        }

    def poll_device_flow(self, device_code: str, client_id: str,
                         interval: int = 5, timeout: int = 900) -> dict[str, Any]:
        """Poll GitHub until the user authorizes or the code expires.

        Returns ``{"access_token": ..., "token_type": ..., "scope": ...}``
        on success.  Raises ``GitHubError`` on denial or timeout.
        """
        deadline = time.time() + timeout
        current_interval = max(interval, 5)
        while time.time() < deadline:
            time.sleep(current_interval)
            try:
                response = requests.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": client_id,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                data = response.json()
            except Exception as exc:
                logger.debug("device flow poll error: %s", exc)
                continue

            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                current_interval += 5
                continue
            if error == "expired_token":
                raise GitHubError("کد Device Flow منقضی شد؛ دوباره تلاش کنید.")
            if error == "access_denied":
                raise GitHubError("کاربر دسترسی را رد کرد.")
            if error:
                raise GitHubError(f"خطای Device Flow: {error}")

            token = data.get("access_token", "")
            if token:
                self.set_token(token)
                return {"access_token": token,
                        "token_type": data.get("token_type", "bearer"),
                        "scope": data.get("scope", "")}

        raise GitHubError("زمان انتظار Device Flow تمام شد.")

    # -------------------------------------------------------- HTTP helpers

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: dict | None = None, timeout: int = 30) -> Any:
        self._require_auth()
        url = f"https://api.github.com{path}" if path.startswith("/") else path
        try:
            response = requests.get(url, headers=self._headers, params=params, timeout=timeout)
            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset = response.headers.get("X-RateLimit-Reset")
                hint = f" (reset: {reset})" if reset else ""
                raise GitHubError(f"GitHub rate limit فعال شد{hint}. کمی بعد دوباره تلاش کنید.")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise GitHubError(f"خطای GitHub API: {exc}") from exc

    def _post(self, path: str, data: dict | None = None, timeout: int = 30) -> Any:
        self._require_auth()
        url = f"https://api.github.com{path}" if path.startswith("/") else path
        try:
            response = requests.post(url, headers=self._headers, json=data, timeout=timeout)
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            return response.json() if response.text else {}
        except requests.RequestException as exc:
            raise GitHubError(f"خطای GitHub API: {exc}") from exc

    def _patch(self, path: str, data: dict | None = None, timeout: int = 30) -> Any:
        self._require_auth()
        url = f"https://api.github.com{path}" if path.startswith("/") else path
        try:
            response = requests.patch(url, headers=self._headers, json=data, timeout=timeout)
            response.raise_for_status()
            return response.json() if response.text else {}
        except requests.RequestException as exc:
            raise GitHubError(f"خطای GitHub API: {exc}") from exc

    def _delete(self, path: str, timeout: int = 30) -> None:
        self._require_auth()
        url = f"https://api.github.com{path}" if path.startswith("/") else path
        try:
            response = requests.delete(url, headers=self._headers, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GitHubError(f"خطای GitHub API: {exc}") from exc

    def _require_auth(self) -> None:
        if not self._token:
            raise GitHubError(
                "GitHub وصل نیست. ابتدا از طریق دکمهٔ «اتصال GitHub» یا "
                "Personal Access Token وصل شوید."
            )

    # -------------------------------------------------------- User

    def get_user(self) -> dict[str, Any]:
        """Return the authenticated user's profile."""
        if self._user:
            return self._user
        data = self._get("/user")
        self._user = {
            "login": data.get("login", ""),
            "name": data.get("name", "") or "",
            "email": data.get("email", "") or "",
            "bio": data.get("bio", "") or "",
            "public_repos": data.get("public_repos", 0),
            "private_repos": data.get("total_private_repos", 0),
            "followers": data.get("followers", 0),
            "following": data.get("following", 0),
            "avatar_url": data.get("avatar_url", ""),
            "url": data.get("html_url", ""),
        }
        return self._user

    def status(self) -> dict[str, Any]:
        """Return connection status and basic account info."""
        if not self._token:
            return {"connected": False, "message": "وصل نیست"}
        try:
            user = self.get_user()
            return {"connected": True, **user}
        except GitHubError as exc:
            return {"connected": False, "message": str(exc)}

    # -------------------------------------------------------- Repositories

    def list_repos(self, sort: str = "updated", direction: str = "desc",
                   per_page: int = 30, visibility: str = "all",
                   query: str = "") -> list[RepoInfo]:
        """List the authenticated user's repositories."""
        params: dict[str, Any] = {
            "sort": sort, "direction": direction,
            "per_page": min(per_page, 100), "type": visibility,
        }
        data = self._get("/user/repos", params=params)
        repos = [self._to_repo_info(r) for r in data]
        if query:
            q = query.lower()
            repos = [r for r in repos
                     if q in r.full_name.lower() or q in r.description.lower()]
        return repos

    def get_repo(self, repo: str) -> RepoInfo:
        """Get details of a specific repository (owner/name or full URL)."""
        owner, name = self._parse_repo(repo)
        data = self._get(f"/repos/{owner}/{name}")
        return self._to_repo_info(data)

    def search_repos(self, query: str, sort: str = "stars",
                     per_page: int = 20) -> list[RepoInfo]:
        """Search public repositories."""
        params = {"q": query, "sort": sort, "per_page": min(per_page, 100)}
        data = self._get("/search/repositories", params=params)
        return [self._to_repo_info(r) for r in data.get("items", [])]

    # -------------------------------------------------------- Issues

    def list_issues(self, repo: str, state: str = "open",
                    labels: str = "", per_page: int = 30,
                    query: str = "") -> list[IssueInfo]:
        owner, name = self._parse_repo(repo)
        params: dict[str, Any] = {
            "state": state, "per_page": min(per_page, 100),
        }
        if labels:
            params["labels"] = labels
        data = self._get(f"/repos/{owner}/{name}/issues", params=params)
        issues = [self._to_issue_info(i) for i in data if "pull_request" not in i]
        if query:
            q = query.lower()
            issues = [i for i in issues if q in i.title.lower() or q in i.body.lower()]
        return issues

    def get_issue(self, repo: str, number: int) -> IssueInfo:
        owner, name = self._parse_repo(repo)
        data = self._get(f"/repos/{owner}/{name}/issues/{int(number)}")
        return self._to_issue_info(data)

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels: list[str] | None = None,
                     assignees: list[str] | None = None) -> IssueInfo:
        owner, name = self._parse_repo(repo)
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = self._post(f"/repos/{owner}/{name}/issues", payload)
        return self._to_issue_info(data)

    def close_issue(self, repo: str, number: int) -> IssueInfo:
        owner, name = self._parse_repo(repo)
        data = self._patch(f"/repos/{owner}/{name}/issues/{int(number)}",
                           {"state": "closed"})
        return self._to_issue_info(data)

    def reopen_issue(self, repo: str, number: int) -> IssueInfo:
        owner, name = self._parse_repo(repo)
        data = self._patch(f"/repos/{owner}/{name}/issues/{int(number)}",
                           {"state": "open"})
        return self._to_issue_info(data)

    def add_issue_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        owner, name = self._parse_repo(repo)
        return self._post(f"/repos/{owner}/{name}/issues/{int(number)}/comments",
                          {"body": body})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        owner, name = self._parse_repo(repo)
        self._post(f"/repos/{owner}/{name}/issues/{int(number)}/labels",
                   {"labels": labels})

    def assign_issue(self, repo: str, number: int, assignees: list[str]) -> None:
        owner, name = self._parse_repo(repo)
        self._post(f"/repos/{owner}/{name}/issues/{int(number)}/assignees",
                   {"assignees": assignees})

    # -------------------------------------------------------- Pull Requests

    def list_prs(self, repo: str, state: str = "open",
                 per_page: int = 30) -> list[PRInfo]:
        owner, name = self._parse_repo(repo)
        params = {"state": state, "per_page": min(per_page, 100)}
        data = self._get(f"/repos/{owner}/{name}/pulls", params=params)
        return [self._to_pr_info(pr) for pr in data]

    def get_pr(self, repo: str, number: int) -> PRInfo:
        owner, name = self._parse_repo(repo)
        data = self._get(f"/repos/{owner}/{name}/pulls/{int(number)}")
        return self._to_pr_info(data)

    def create_pr(self, repo: str, title: str, head: str, base: str = "main",
                  body: str = "", draft: bool = False) -> PRInfo:
        owner, name = self._parse_repo(repo)
        payload: dict[str, Any] = {
            "title": title, "head": head, "base": base,
        }
        if body:
            payload["body"] = body
        if draft:
            payload["draft"] = True
        data = self._post(f"/repos/{owner}/{name}/pulls", payload)
        return self._to_pr_info(data)

    def merge_pr(self, repo: str, number: int,
                 method: str = "merge", message: str = "") -> dict[str, Any]:
        owner, name = self._parse_repo(repo)
        payload: dict[str, Any] = {"merge_method": method}
        if message:
            payload["commit_message"] = message
        return self._post(f"/repos/{owner}/{name}/pulls/{int(number)}/merge", payload)

    def close_pr(self, repo: str, number: int) -> PRInfo:
        owner, name = self._parse_repo(repo)
        data = self._patch(f"/repos/{owner}/{name}/pulls/{int(number)}",
                           {"state": "closed"})
        return self._to_pr_info(data)

    def get_pr_diff(self, repo: str, number: int) -> str:
        """Get the diff of a pull request."""
        owner, name = self._parse_repo(repo)
        url = f"https://api.github.com/repos/{owner}/{name}/pulls/{int(number)}"
        headers = dict(self._headers)
        headers["Accept"] = "application/vnd.github.diff"
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text[:20000]

    # -------------------------------------------------------- Branches

    def list_branches(self, repo: str, per_page: int = 30) -> list[dict[str, Any]]:
        owner, name = self._parse_repo(repo)
        params = {"per_page": min(per_page, 100)}
        data = self._get(f"/repos/{owner}/{name}/branches", params=params)
        return [
            {
                "name": b["name"],
                "sha": b["commit"]["sha"][:12],
                "protected": b.get("protected", False),
            }
            for b in data
        ]

    def create_branch(self, repo: str, branch_name: str,
                      from_ref: str = "") -> dict[str, Any]:
        owner, name = self._parse_repo(repo)
        if not from_ref:
            repo_info = self.get_repo(repo)
            from_ref = repo_info.default_branch
        # Get the SHA of the source ref
        ref_data = self._get(f"/repos/{owner}/{name}/git/ref/heads/{from_ref}")
        sha = ref_data["object"]["sha"]
        return self._post(f"/repos/{owner}/{name}/git/refs",
                          {"ref": f"refs/heads/{branch_name}", "sha": sha})

    def delete_branch(self, repo: str, branch_name: str) -> None:
        owner, name = self._parse_repo(repo)
        self._delete(f"/repos/{owner}/{name}/git/refs/heads/{branch_name}")

    # -------------------------------------------------------- Commits

    def get_commits(self, repo: str, sha: str = "", per_page: int = 20,
                    path: str = "") -> list[dict[str, Any]]:
        owner, name = self._parse_repo(repo)
        params: dict[str, Any] = {"per_page": min(per_page, 100)}
        if sha:
            params["sha"] = sha
        if path:
            params["path"] = path
        data = self._get(f"/repos/{owner}/{name}/commits", params=params)
        return [
            {
                "sha": c["sha"][:12],
                "message": c["commit"]["message"].split("\n")[0][:120],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
                "url": c["html_url"],
            }
            for c in data
        ]

    # -------------------------------------------------------- Files

    def get_file(self, repo: str, path: str, ref: str = "") -> dict[str, Any]:
        """Read a file's content from a repository."""
        owner, name = self._parse_repo(repo)
        params: dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        data = self._get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        import base64
        content = ""
        if data.get("encoding") == "base64" and data.get("content"):
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                content = "(binary file)"
        return {
            "path": data.get("path", path),
            "size": data.get("size", 0),
            "sha": data.get("sha", ""),
            "content": content[:20000],
            "url": data.get("html_url", ""),
        }

    def list_files(self, repo: str, path: str = "", ref: str = "") -> list[dict[str, Any]]:
        """List files/directories in a repository."""
        owner, name = self._parse_repo(repo)
        params: dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        api_path = f"/repos/{owner}/{name}/contents"
        if path:
            api_path += f"/{path}"
        data = self._get(api_path, params=params)
        if isinstance(data, dict):
            data = [data]
        return [
            {
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "type": item.get("type", "file"),
                "size": item.get("size", 0),
            }
            for item in data
        ]

    def update_file(self, repo: str, path: str, content: str,
                    message: str = "", branch: str = "") -> dict[str, Any]:
        """Create or update a file in a repository."""
        owner, name = self._parse_repo(repo)
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload: dict[str, Any] = {
            "message": message or f"Update {path}",
            "content": encoded,
        }
        if branch:
            payload["branch"] = branch
        # Check if file exists to get its SHA
        try:
            existing = self._get(f"/repos/{owner}/{name}/contents/{path}")
            payload["sha"] = existing["sha"]
        except GitHubError:
            pass  # File doesn't exist yet, that's fine
        return self._post(f"/repos/{owner}/{name}/contents/{path}", payload) if False else \
            self._put(f"/repos/{owner}/{name}/contents/{path}", payload)

    def _put(self, path: str, data: dict | None = None, timeout: int = 30) -> Any:
        self._require_auth()
        url = f"https://api.github.com{path}" if path.startswith("/") else path
        try:
            response = requests.put(url, headers=self._headers, json=data, timeout=timeout)
            response.raise_for_status()
            return response.json() if response.text else {}
        except requests.RequestException as exc:
            raise GitHubError(f"خطای GitHub API: {exc}") from exc

    # -------------------------------------------------------- Releases

    def list_releases(self, repo: str, per_page: int = 10) -> list[dict[str, Any]]:
        owner, name = self._parse_repo(repo)
        params = {"per_page": min(per_page, 100)}
        data = self._get(f"/repos/{owner}/{name}/releases", params=params)
        return [
            {
                "tag": r.get("tag_name", ""),
                "name": r.get("name", "") or r.get("tag_name", ""),
                "body": (r.get("body", "") or "")[:300],
                "prerelease": r.get("prerelease", False),
                "draft": r.get("draft", False),
                "created_at": r.get("created_at", ""),
                "published_at": r.get("published_at", ""),
                "url": r.get("html_url", ""),
            }
            for r in data
        ]

    def create_release(self, repo: str, tag: str, name: str = "",
                       body: str = "", draft: bool = False,
                       prerelease: bool = False) -> dict[str, Any]:
        owner, repo_name = self._parse_repo(repo)
        payload: dict[str, Any] = {
            "tag_name": tag,
            "name": name or tag,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
        }
        return self._post(f"/repos/{owner}/{repo_name}/releases", payload)

    # -------------------------------------------------------- Search

    def search_code(self, query: str, per_page: int = 20) -> list[dict[str, Any]]:
        """Search code across GitHub (public repos)."""
        params = {"q": query, "per_page": min(per_page, 100)}
        data = self._get("/search/code", params=params)
        return [
            {
                "path": item.get("path", ""),
                "repo": item.get("repository", {}).get("full_name", ""),
                "url": item.get("html_url", ""),
                "name": item.get("name", ""),
            }
            for item in data.get("items", [])
        ]

    def search_issues(self, query: str, per_page: int = 20) -> list[IssueInfo]:
        """Search issues and PRs across GitHub."""
        params = {"q": query, "per_page": min(per_page, 100)}
        data = self._get("/search/issues", params=params)
        return [self._to_issue_info(i) for i in data.get("items", [])]

    # -------------------------------------------------------- Notifications

    def list_notifications(self, all: bool = False, per_page: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": min(per_page, 100)}
        if all:
            params["all"] = "true"
        data = self._get("/notifications", params=params)
        return [
            {
                "id": n.get("id", ""),
                "unread": n.get("unread", False),
                "reason": n.get("reason", ""),
                "type": n.get("subject", {}).get("type", ""),
                "title": n.get("subject", {}).get("title", ""),
                "repo": n.get("repository", {}).get("full_name", ""),
                "url": n.get("subject", {}).get("url", ""),
                "updated_at": n.get("updated_at", ""),
            }
            for n in data
        ]

    def mark_notifications_read(self) -> None:
        """Mark all notifications as read."""
        self._put("/notifications", {"read": True})

    # -------------------------------------------------------- Helpers

    @staticmethod
    def _parse_repo(repo: str) -> tuple[str, str]:
        """Parse ``owner/name``, a full URL, or ``owner/name`` from user input."""
        cleaned = str(repo).strip().rstrip("/")
        # Full URL: https://github.com/owner/name/...
        if "github.com/" in cleaned:
            parts = cleaned.split("github.com/", 1)[1].split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
        # owner/name
        if "/" in cleaned:
            parts = cleaned.split("/", 1)
            return parts[0], parts[1]
        raise GitHubError(
            f"نام repository نامعتبر است: «{cleaned}». "
            "فرمت: owner/name (مثلاً Alirezahjf/AI_Agent_OLLAMA)"
        )

    @staticmethod
    def _to_repo_info(data: dict) -> RepoInfo:
        return RepoInfo(
            full_name=data.get("full_name", ""),
            description=data.get("description", "") or "",
            language=data.get("language", "") or "",
            stars=data.get("stargazers_count", 0) or 0,
            forks=data.get("forks_count", 0) or 0,
            open_issues=data.get("open_issues_count", 0) or 0,
            is_private=data.get("private", False),
            is_fork=data.get("fork", False),
            default_branch=data.get("default_branch", "main"),
            updated_at=data.get("updated_at", ""),
            url=data.get("html_url", ""),
            topics=data.get("topics", []) or [],
        )

    @staticmethod
    def _to_issue_info(data: dict) -> IssueInfo:
        return IssueInfo(
            number=data.get("number", 0),
            title=data.get("title", ""),
            state=data.get("state", ""),
            body=data.get("body", "") or "",
            user=data.get("user", {}).get("login", "") if data.get("user") else "",
            labels=[l.get("name", "") if isinstance(l, dict) else str(l)
                    for l in (data.get("labels", []) or [])],
            assignees=[a.get("login", "") for a in (data.get("assignees", []) or [])
                       if isinstance(a, dict)],
            comments=data.get("comments", 0) or 0,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            url=data.get("html_url", ""),
            is_pull_request="pull_request" in data,
        )

    @staticmethod
    def _to_pr_info(data: dict) -> PRInfo:
        return PRInfo(
            number=data.get("number", 0),
            title=data.get("title", ""),
            state=data.get("state", ""),
            user=data.get("user", {}).get("login", "") if data.get("user") else "",
            head=data.get("head", {}).get("ref", "") if data.get("head") else "",
            base=data.get("base", {}).get("ref", "") if data.get("base") else "",
            body=data.get("body", "") or "",
            merged=data.get("merged", False),
            mergeable=data.get("mergeable"),
            additions=data.get("additions", 0) or 0,
            deletions=data.get("deletions", 0) or 0,
            changed_files=data.get("changed_files", 0) or 0,
            comments=data.get("comments", 0) or 0,
            review_comments=data.get("review_comments", 0) or 0,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            url=data.get("html_url", ""),
            labels=[l.get("name", "") if isinstance(l, dict) else str(l)
                    for l in (data.get("labels", []) or [])],
        )
