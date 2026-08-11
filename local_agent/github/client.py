"""Real GitHub client: OAuth redirect flow, Personal Access Token, REST API,
and local git operations (clone/init/status/add/commit/push/pull/branch/merge).

The client owns a single OAuth/PAT token stored in a JSON file on disk
(never in ``config.json`` plaintext).  For git push/pull the token is fed
to ``git`` through a process-local ``http.extraheader`` environment
variable so it never lands in ``.git/config``, command-line arguments or
logs — only in the short-lived subprocess environment of the same user.

OAuth web flow (the "redirect to github" the user asked for)::

    authorize_url(redirect_uri, scope) -> (url, state)
        browser -> https://github.com/login/oauth/authorize?...&state=...
        github  -> http://localhost:<port>/api/github/callback?code=...&state=...
    exchange_code(code, state) -> validates state, exchanges code for token

All network calls use ``requests`` (already a dependency).  All git calls
use the real ``git`` binary found on PATH.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("github")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_SCOPE = "repo workflow read:user"


class GitHubError(AssistantError):
    """A user-facing failure from the GitHub integration (never the token)."""


# --------------------------------------------------------------------------- #
# OAuth pending-flow state (CSRF + account routing)
# --------------------------------------------------------------------------- #


@dataclass
class PendingOAuth:
    """An in-flight OAuth authorization, keyed by the random ``state``."""

    account: str
    client_id: str
    client_secret: str
    scope: str
    redirect_uri: str
    created_at: float


# --------------------------------------------------------------------------- #
# Public model
# --------------------------------------------------------------------------- #


@dataclass
class GitHubUser:
    login: str
    name: str = ""
    id: int = 0
    avatar_url: str = ""
    html_url: str = ""
    email: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "GitHubUser":
        return cls(
            login=str(data.get("login", "")),
            name=str(data.get("name") or ""),
            id=int(data.get("id", 0) or 0),
            avatar_url=str(data.get("avatar_url", "") or ""),
            html_url=str(data.get("html_url", "") or ""),
            email=str(data.get("email", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "name": self.name,
            "id": self.id,
            "avatar_url": self.avatar_url,
            "html_url": self.html_url,
            "email": self.email,
        }


class GitHubClient:
    """One GitHub identity: OAuth App credentials or a PAT.

    The synchronous public API mirrors :class:`PersonalTelegram` /
    :class:`GmailBackend` so the actions layer can use it unchanged.
    """

    def __init__(
        self,
        *,
        account_name: str = "اصلی",
        api_base: str = "https://api.github.com",
        client_id: str = "",
        client_secret: str = "",
        token_file: Path | str | None = None,
        default_scope: str = DEFAULT_SCOPE,
        data_dir: Path | str | None = None,
        transport: Any = None,
    ) -> None:
        self.account_name = str(account_name)
        self.api_base = (api_base or "https://api.github.com").rstrip("/")
        self.client_id = str(client_id or "")
        self.client_secret = str(client_secret or "")
        self.default_scope = str(default_scope or DEFAULT_SCOPE)
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".local_assistant"
        self.token_file = (
            Path(token_file).expanduser()
            if token_file
            else self.data_dir / "github" / f"github_{_safe_name(self.account_name)}.json"
        )
        # ``transport`` is the requests module (or a fake for unit tests).
        self._http = transport or requests
        self._token: str = ""
        self._scope: str = ""
        self._token_type: str = ""
        self._user: GitHubUser | None = None
        self._connected = False
        self.connected_at: datetime | None = None
        self.last_error = ""
        # OAuth pending flows: state -> PendingOAuth.  Shared across all
        # clients of a process via the class so the web callback (which does
        # not know which client object started the flow) can resolve it.
        self.login_state = "disconnected"

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def login(self) -> str:
        return self._user.login if self._user else ""

    # ------------------------------------------------------------------ #
    # Token persistence (separate JSON file, never in config.json)
    # ------------------------------------------------------------------ #

    def _load_token(self) -> bool:
        if not self.token_file.is_file():
            return False
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("github token file unreadable: %s", exc)
            return False
        self._token = str(payload.get("access_token", "") or "")
        self._scope = str(payload.get("scope", "") or "")
        self._token_type = str(payload.get("token_type", "") or "")
        login = str(payload.get("login", "") or "")
        if login:
            self._user = GitHubUser(login=login, name=str(payload.get("name", "") or ""))
        return bool(self._token)

    def _save_token(self, token: str, scope: str, token_type: str, user: GitHubUser) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": token,
            "scope": scope,
            "token_type": token_type,
            "login": user.login,
            "name": user.name,
            "saved_at": datetime.now().isoformat(),
        }
        tmp = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.token_file)
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def _clear_token(self) -> None:
        try:
            if self.token_file.is_file():
                self.token_file.unlink()
        except OSError as exc:
            logger.debug("github token file removal failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Authentication: PAT + OAuth
    # ------------------------------------------------------------------ #

    def connect(self) -> dict[str, Any]:
        """Load a stored token and validate it via GET /user."""
        if not self._load_token():
            self.login_state = "disconnected"
            raise GitHubError(
                "هیچ توکن گیتهاب ذخیره نشده است. در تنظیمات با OAuth وصل شوید یا یک "
                "توکن دسترسی شخصی (PAT) وارد کنید."
            )
        user = self._fetch_user()
        self._user = user
        self._connected = True
        self.connected_at = datetime.now()
        self.last_error = ""
        self.login_state = "connected"
        return {"state": "connected", "user": user.to_dict(), "message": self._welcome(user)}

    def connect_pat(self, token: str) -> dict[str, Any]:
        """Validate a Personal Access Token via GET /user and store it."""
        token = str(token or "").strip()
        if not token:
            raise GitHubError("توکن خالی است.")
        self._token = token
        user = self._fetch_user()
        self._user = user
        self._scope = self._scope or "repo"
        self._save_token(token, self._scope, "pat", user)
        self._connected = True
        self.connected_at = datetime.now()
        self.last_error = ""
        self.login_state = "connected"
        return {"state": "connected", "user": user.to_dict(), "message": self._welcome(user)}

    def authorize_url(
        self,
        redirect_uri: str,
        *,
        scope: str | None = None,
        state_registry: dict[str, PendingOAuth] | None = None,
    ) -> tuple[str, str]:
        """Build the GitHub authorize URL + a one-shot ``state``.

        ``state`` carries CSRF protection AND the account routing so the
        callback handler knows which client_id/secret to exchange with.
        """
        if not self.client_id:
            raise GitHubError(
                "client_id تنظیم نشده است. در github.com/settings/developers یک OAuth App "
                "بسازید و client_id را در تنظیمات وارد کنید (callback = "
                f"{redirect_uri})."
            )
        scope = scope or self.default_scope
        nonce = secrets.token_urlsafe(24)
        state = f"{self.account_name}::{nonce}"
        if state_registry is not None:
            state_registry[state] = PendingOAuth(
                account=self.account_name,
                client_id=self.client_id,
                client_secret=self.client_secret,
                scope=scope,
                redirect_uri=redirect_uri,
                created_at=datetime.now().timestamp(),
            )
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
        url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
        return url, state

    def exchange_code(self, code: str, client_secret: str | None = None) -> dict[str, Any]:
        """Exchange an OAuth ``code`` for an ``access_token`` and store it.

        ``client_secret`` is taken from the pending flow (passed by the
        handler) so this client need not hold it permanently.
        """
        secret = client_secret if client_secret is not None else self.client_secret
        if not self.client_id:
            raise GitHubError("client_id تنظیم نشده است.")
        try:
            response = self._http.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": secret,
                    "code": str(code),
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except self._http.RequestException as exc:  # type: ignore[attr-defined]
            raise GitHubError(f"تبدیل کد OAuth ناموفق بود: {exc}") from exc
        except ValueError as exc:
            raise GitHubError("پاسخ گیتهاب قابل خواندن نبود.") from exc
        token = str(payload.get("access_token", "") or "")
        if not token:
            err = payload.get("error_description") or payload.get("error") or "نامشخص"
            raise GitHubError(f"گیتهاب توکن نداد: {err}")
        self._token = token
        self._scope = str(payload.get("scope", "") or self.default_scope)
        self._token_type = str(payload.get("token_type", "") or "bearer")
        user = self._fetch_user()
        self._user = user
        self._save_token(token, self._scope, self._token_type, user)
        self._connected = True
        self.connected_at = datetime.now()
        self.last_error = ""
        self.login_state = "connected"
        return {"state": "connected", "user": user.to_dict(), "message": self._welcome(user)}

    def disconnect(self) -> None:
        self._token = ""
        self._user = None
        self._connected = False
        self.connected_at = None
        self.login_state = "disconnected"

    def forget_token(self) -> None:
        """Disconnect AND delete the stored token file."""
        self.disconnect()
        self._clear_token()

    @staticmethod
    def _welcome(user: GitHubUser) -> str:
        return f"به‌عنوان @{user.login}" + (f" ({user.name})" if user.name else "") + " وصل شدی"

    # ------------------------------------------------------------------ #
    # REST API helpers
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise GitHubError("ابتدا به گیتهاب وصل شوید.")
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        try:
            response = self._http.request(
                method, url, headers=self._headers(), timeout=30, **kwargs
            )
        except self._http.RequestException as exc:  # type: ignore[attr-defined]
            raise GitHubError(f"ارتباط با گیتهاب ناموفق بود: {exc}") from exc
        if response.status_code not in ok:
            self._raise_for_status(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        status = getattr(response, "status_code", "?")
        try:
            payload = response.json()
            message = (payload.get("message") or "").strip()
        except (ValueError, AttributeError):
            message = ""
        if status == 401:
            raise GitHubError("توکن گیتهاب نامعتبر یا منقضی است؛ دوباره وصل شوید.")
        if status == 403 and "rate limit" in message.lower():
            raise GitHubError("محدودیت نرخ گیتهاب فعال شد؛ کمی بعد تلاش کنید.")
        if status == 404:
            raise GitHubError("مخزن/منبع پیدا نشد (یا توکن دسترسی ندارد).")
        raise GitHubError(f"گیتهاب خطا داد (HTTP {status}): {message or 'نامشخص'}")

    def _fetch_user(self) -> GitHubUser:
        data = self._request("GET", "/user")
        if not isinstance(data, dict):
            raise GitHubError("پاسخ /user نامعتبر بود.")
        return GitHubUser.from_api(data)

    # ------------------------------------------------------------------ #
    # Read-only API actions
    # ------------------------------------------------------------------ #

    def whoami(self) -> dict[str, Any]:
        if self._user is None:
            self._user = self._fetch_user()
        return self._user.to_dict()

    def list_repos(self, limit: int = 30) -> list[dict[str, Any]]:
        data = self._request("GET", "/user/repos", params={"per_page": min(max(limit, 1), 100),
                                                            "sort": "updated"})
        repos = data if isinstance(data, list) else []
        return [
            {
                "name": r.get("full_name") or r.get("name"),
                "private": bool(r.get("private")),
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "default_branch": r.get("default_branch", ""),
                "url": r.get("html_url", ""),
                "updated": r.get("updated_at", ""),
            }
            for r in repos[: min(max(limit, 1), 100)]
        ]

    def get_repo(self, repo: str) -> dict[str, Any]:
        data = self._request("GET", f"/repos/{_owner_slash(repo)}")
        return {
            "name": data.get("full_name"),
            "private": bool(data.get("private")),
            "description": data.get("description") or "",
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "default_branch": data.get("default_branch", ""),
            "clone_url": data.get("clone_url", ""),
            "url": data.get("html_url", ""),
        }

    # ------------------------------------------------------------------ #
    # Mutating API actions
    # ------------------------------------------------------------------ #

    def create_repo(self, name: str, *, private: bool = True, description: str = "") -> dict[str, Any]:
        data = self._request(
            "POST", "/user/repos",
            json={"name": str(name), "private": bool(private),
                  "description": description or "", "auto_init": True},
            ok=(200, 201),
        )
        return {
            "name": data.get("full_name"),
            "url": data.get("html_url", ""),
            "clone_url": data.get("clone_url", ""),
            "private": bool(data.get("private")),
        }

    def create_pr(self, repo: str, *, head: str, base: str, title: str, body: str = "") -> dict[str, Any]:
        data = self._request(
            "POST", f"/repos/{_owner_slash(repo)}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
            ok=(200, 201),
        )
        return {"number": data.get("number"), "url": data.get("html_url"), "state": data.get("state")}

    def list_prs(self, repo: str, *, state: str = "open", limit: int = 30) -> list[dict[str, Any]]:
        data = self._request(
            "GET", f"/repos/{_owner_slash(repo)}/pulls",
            params={"state": state, "per_page": min(max(limit, 1), 100)},
        )
        prs = data if isinstance(data, list) else []
        return [
            {"number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
             "url": p.get("html_url"), "user": (p.get("user") or {}).get("login", "")}
            for p in prs[: min(max(limit, 1), 100)]
        ]

    def merge_pr(self, repo: str, number: int, *, commit_title: str = "") -> dict[str, Any]:
        data = self._request(
            "PUT", f"/repos/{_owner_slash(repo)}/pulls/{int(number)}/merge",
            json={"commit_title": commit_title or None},
            ok=(200, 201),
        )
        return {"merged": bool(data.get("merged")), "sha": data.get("sha", "")}

    def create_issue(self, repo: str, *, title: str, body: str = "") -> dict[str, Any]:
        data = self._request(
            "POST", f"/repos/{_owner_slash(repo)}/issues",
            json={"title": title, "body": body}, ok=(200, 201),
        )
        return {"number": data.get("number"), "url": data.get("html_url"), "state": data.get("state")}

    def list_issues(self, repo: str, *, state: str = "open", limit: int = 30) -> list[dict[str, Any]]:
        data = self._request(
            "GET", f"/repos/{_owner_slash(repo)}/issues",
            params={"state": state, "per_page": min(max(limit, 1), 100)},
        )
        issues = [i for i in (data if isinstance(data, list) else []) if "pull_request" not in i]
        return [
            {"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
             "url": i.get("html_url"), "user": (i.get("user") or {}).get("login", "")}
            for i in issues[: min(max(limit, 1), 100)]
        ]

    def create_release(self, repo: str, *, tag: str, name: str = "", body: str = "") -> dict[str, Any]:
        data = self._request(
            "POST", f"/repos/{_owner_slash(repo)}/releases",
            json={"tag_name": tag, "name": name or tag, "body": body},
            ok=(200, 201),
        )
        return {"id": data.get("id"), "url": data.get("html_url"), "tag": data.get("tag_name")}

    def list_workflows(self, repo: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/repos/{_owner_slash(repo)}/actions/workflows")
        wfs = (data or {}).get("workflows") if isinstance(data, dict) else None
        return [
            {"name": w.get("name"), "state": w.get("state"), "path": w.get("path")}
            for w in (wfs or [])
        ]

    def list_workflow_runs(self, repo: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._request(
            "GET", f"/repos/{_owner_slash(repo)}/actions/runs",
            params={"per_page": min(max(limit, 1), 100)},
        )
        runs = (data or {}).get("workflow_runs") if isinstance(data, dict) else None
        return [
            {"name": (r.get("name") or r.get("display_title") or ""), "status": r.get("status"),
             "conclusion": r.get("conclusion"), "branch": r.get("head_branch"),
             "url": r.get("html_url"), "created": r.get("created_at")}
            for r in (runs or [])[: min(max(limit, 1), 100)]
        ]

    def fetch_url(self, url: str) -> dict[str, Any]:
        """Turn any github.com URL into structured data (anti prompt-injection).

        Recognises: user/org profile, repo root, issues, pulls, releases,
        commits, and raw file paths.  The returned dict is plain data the
        agent can show without executing anything from the page.
        """
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if "github.com" not in host:
            raise GitHubError("این یک آدرس گیتهاب نیست.")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return {"kind": "root", "url": url}
        owner = parts[0]
        # /owner
        if len(parts) == 1:
            data = self._request("GET", f"/users/{owner}", ok=(200, 404))
            if isinstance(data, dict) and data.get("login"):
                return {"kind": "user", "login": data.get("login"), "name": data.get("name"),
                        "bio": data.get("bio"), "url": data.get("html_url")}
            return {"kind": "unknown", "owner": owner}
        repo_name = f"{owner}/{parts[1]}"
        # /owner/repo
        if len(parts) == 2:
            return {"kind": "repo", **self.get_repo(repo_name)}
        section = parts[2]
        if section in ("issues", "pull", "pulls", "releases", "commit", "commits"):
            if len(parts) >= 4 and section in ("issues",):
                data = self._request("GET", f"/repos/{repo_name}/issues/{parts[3]}", ok=(200, 404))
                if isinstance(data, dict):
                    return {"kind": "issue", "repo": repo_name, "number": data.get("number"),
                            "title": data.get("title"), "state": data.get("state"),
                            "body": (data.get("body") or "")[:2000]}
            if len(parts) >= 4 and section in ("pull", "pulls"):
                data = self._request("GET", f"/repos/{repo_name}/pulls/{parts[3]}", ok=(200, 404))
                if isinstance(data, dict):
                    return {"kind": "pull", "repo": repo_name, "number": data.get("number"),
                            "title": data.get("title"), "state": data.get("state"),
                            "body": (data.get("body") or "")[:2000], "merged": bool(data.get("merged"))}
            return {"kind": "section", "repo": repo_name, "section": section, "url": url}
        return {"kind": "path", "repo": repo_name, "path": "/".join(parts[2:]), "url": url}

    # ------------------------------------------------------------------ #
    # Local git operations (real `git` binary, token via env, never on disk)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _git_available() -> bool:
        return shutil.which("git") is not None

    def _git_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "GIT_ASKPASS"}
        env["GIT_TERMINAL_PROMPT"] = "0"  # never interactively prompt for creds
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if self._token:
            cred = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {cred}"
        return env

    def _git(self, args: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
        if not self._git_available():
            raise GitHubError("برنامهٔ git روی سیستم نصب نیست؛ نصبش کنید.")
        try:
            return subprocess.run(
                ["git", *args], cwd=str(cwd), env=self._git_env(),
                text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"دستور git بیش از {timeout} ثانیه طول کشید.") from exc

    def _git_ok(self, args: list[str], *, cwd: Path, timeout: int = 120) -> str:
        result = self._git(args, cwd=cwd, timeout=timeout)
        if result.returncode != 0:
            raise GitHubError(f"git {' '.join(args)} ناموفق بود:\n{result.stdout.strip()[:1500]}")
        return result.stdout.strip()

    def clone(self, repo: str, dest: str | Path, *, depth: int = 0) -> str:
        target = Path(dest).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        clone_url = _normalize_clone_url(repo, self._user.login if self._user else "")
        args = ["clone"]
        if depth and depth > 0:
            args += ["--depth", str(int(depth))]
        args += [clone_url, str(target)]
        self._git_ok(args, cwd=target.parent)
        return str(target)

    def init_repo(self, path: str | Path, remote_url: str) -> str:
        target = Path(path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        self._git_ok(["init"], cwd=target)
        remote = _normalize_clone_url(remote_url, self._user.login if self._user else "")
        # Set the remote URL via env-injected URL is unsafe to persist; instead
        # store the public URL (no token) and rely on the extraheader env for
        # auth at push time.
        self._git_ok(["remote", "remove", "origin"], cwd=target) if False else None
        try:
            self._git(["remote", "remove", "origin"], cwd=target)
        except GitHubError:
            pass
        self._git_ok(["remote", "add", "origin", remote], cwd=target)
        return str(target)

    def git_status(self, path: str | Path) -> str:
        target = _require_repo(path)
        branch = self._git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target)
        status = self._git_ok(["status", "--short", "--branch"], cwd=target)
        return f"شاخه: {branch}\n{status or '(تغییری نیست)'}"

    def git_diff(self, path: str | Path, *, staged: bool = False) -> str:
        target = _require_repo(path)
        args = ["diff", "--stat"] + (["--cached"] if staged else [])
        return self._git_ok(args, cwd=target) or "(تفاوتی نیست)"

    def add_commit(self, path: str | Path, message: str, *, paths: list[str] | None = None) -> str:
        target = _require_repo(path)
        if not isinstance(message, str) or not message.strip():
            raise GitHubError("پیام commit نباید خالی باشد.")
        targets = paths or ["-A"]
        self._git_ok(["add", *targets], cwd=target)
        self._git_ok(["commit", "-m", message], cwd=target)
        head = self._git_ok(["log", "-1", "--format=%h %s"], cwd=target)
        return head

    def push(self, path: str | Path, *, remote: str = "origin", branch: str = "",
             force: bool = False, set_upstream: bool = False) -> str:
        target = _require_repo(path)
        if force:
            # force-push is dangerous: require an explicit, recent confirmation.
            # The caller (action layer) gates this; here we only run it.
            pass
        args = ["push"]
        if set_upstream:
            args.append("-u")
        if force:
            args.append("--force-with-lease")
        args.append(remote)
        if branch:
            args.append(f"HEAD:{branch}")
        self._git_ok(args, cwd=target)
        return f"push شد به {remote}" + (f"/{branch}" if branch else "")

    def pull(self, path: str | Path, *, remote: str = "origin", branch: str = "") -> str:
        target = _require_repo(path)
        args = ["pull", "--ff-only", remote]
        if branch:
            args.append(branch)
        self._git_ok(args, cwd=target)
        return f"pull شد از {remote}" + (f"/{branch}" if branch else "")

    def branch(self, path: str | Path, *, action: str = "list", name: str = "",
               base: str = "") -> str:
        target = _require_repo(path)
        action = (action or "list").lower()
        if action == "list":
            return self._git_ok(["branch", "--list"], cwd=target) or "(شاخه‌ای نیست)"
        if not name:
            raise GitHubError("نام شاخه الزامی است.")
        if action == "create":
            self._git_ok(["checkout", "-b", name] + ([base] if base else []), cwd=target)
            return f"شاخهٔ {name} ساخته و فعال شد."
        if action in ("switch", "checkout"):
            self._git_ok(["checkout", name], cwd=target)
            return f"به شاخهٔ {name} سوئیچ شد."
        if action == "delete":
            self._git_ok(["branch", "-D", name], cwd=target)
            return f"شاخهٔ {name} حذف شد."
        raise GitHubError("action نامعتبر است (list/create/switch/delete).")

    def merge(self, path: str | Path, branch: str, *, message: str = "") -> str:
        target = _require_repo(path)
        args = ["merge", "--no-ff", branch]
        if message:
            args += ["-m", message]
        self._git_ok(args, cwd=target)
        return f"شاخهٔ {branch} ادغام شد."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "account")) or "account"


def _owner_slash(repo: str) -> str:
    """Normalise 'owner/repo' (allow a full URL or a bare 'repo' for the
    authenticated user)."""
    repo = str(repo or "").strip()
    if not repo:
        raise GitHubError("نام مخزن خالی است.")
    if repo.startswith("http"):
        parsed = urlparse(repo)
        parts = [p for p in parsed.path.split("/") if p][:2]
        if len(parts) != 2:
            raise GitHubError("آدرس مخزن نامعتبر است.")
        return "/".join(parts)
    if repo.count("/") == 1:
        return repo
    raise GitHubError("مخزن را به‌صورت owner/repo بدهید.")


def _normalize_clone_url(repo: str, login: str) -> str:
    """Return a public HTTPS clone URL (no token embedded)."""
    repo = str(repo or "").strip()
    if repo.startswith("git@") or repo.startswith("https://") or repo.startswith("http://"):
        return repo
    if repo.startswith("git://"):
        return repo
    if repo.count("/") == 1:
        return f"https://github.com/{repo}.git"
    # Treat as owner/repo possibly without .git
    return f"https://github.com/{repo}.git" if not repo.endswith(".git") else repo


def _require_repo(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not (target / ".git").is_dir():
        raise GitHubError("این مسیر یک مخزن git نیست؛ ابتدا clone یا init کنید.")
    return target
