"""Constrained local-Git operations authenticated without tokenized remotes."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..core.errors import AssistantError, DependencyMissing

_REF_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*[~^:?*\\\[\s])[^\x00-\x1f\x7f]{1,200}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MAX_CONFIG_BYTES = 1 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_REMOTE_URL_BYTES = 8 * 1024


class LocalGit:
    def __init__(
        self,
        root: Path,
        token_provider: Callable[[], str],
        *,
        web_url: str,
        allowed_repositories: tuple[str, ...] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self._token_provider = token_provider
        self.web_url = web_url.rstrip("/")
        self.allowed_repositories = (
            None
            if allowed_repositories is None
            else {repository.casefold() for repository in allowed_repositories}
        )

    def update(
        self,
        root: Path,
        *,
        web_url: str,
        allowed_repositories: tuple[str, ...] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.web_url = web_url.rstrip("/")
        self.allowed_repositories = (
            None
            if allowed_repositories is None
            else {repository.casefold() for repository in allowed_repositories}
        )

    def clone(self, owner: str, repo: str, *, destination: Any = None) -> dict[str, Any]:
        target = self._path(str(destination or repo), must_exist=False)
        if target.exists():
            raise AssistantError("مسیر مقصد clone از قبل وجود دارد")
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.web_url}/{owner}/{repo}.git"
        self._run(["clone", "--", url, str(target)], cwd=self.root, authenticated=True, timeout=300)
        return {"ok": True, "path": str(target), "repository": f"{owner}/{repo}"}

    def status(self, path: str) -> dict[str, Any]:
        repo = self._repo(path)
        branch = self._run(["branch", "--show-current"], repo).strip()
        porcelain = self._run(["status", "--porcelain=v1", "--branch"], repo)
        return {
            "path": str(repo),
            "branch": branch,
            "porcelain": porcelain,
            "clean": not any(line and not line.startswith("##") for line in porcelain.splitlines()),
        }

    def branches(self, path: str) -> list[dict[str, Any]]:
        repo = self._repo(path)
        output = self._run(
            ["branch", "--all", "--format=%(refname:short)%09%(objectname)%09%(HEAD)"], repo
        )
        result = []
        for line in output.splitlines():
            name, sha, current = (line.split("\t") + ["", ""])[:3]
            result.append({"name": name, "sha": sha, "current": current == "*"})
        return result

    def log(self, path: str, *, limit: int = 50) -> list[dict[str, str]]:
        repo = self._repo(path)
        fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"
        output = self._run(["log", f"--max-count={limit}", f"--format={fmt}"], repo)
        records = []
        for record in output.strip("\x1e\n").split("\x1e") if output else []:
            fields = record.strip().split("\x1f")
            if len(fields) == 5:
                records.append(
                    dict(zip(("sha", "author", "email", "date", "subject"), fields, strict=True))
                )
        return records

    def remotes(self, path: str) -> list[dict[str, str]]:
        repo = self._repo(path)
        output = self._run(["remote", "-v"], repo)
        result: list[dict[str, str]] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                result.append(
                    {"name": parts[0], "url": _redact_url(parts[1]), "kind": parts[2].strip("()")}
                )
        return result

    def diff(self, path: str, *, staged: bool = False, ref: str = "") -> dict[str, Any]:
        repo = self._repo(path)
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        if ref:
            args.append(self._revision(ref))
        output = self._run(args, repo)
        return {"path": str(repo), "staged": staged, "diff": output}

    def repositories(self) -> list[dict[str, Any]]:
        """List immediate, non-symlinked clones under the configured root."""
        if not self.root.exists():
            return []
        if not self.root.is_dir() or self.root.is_symlink():
            raise AssistantError("ریشهٔ clone محلی یک پوشهٔ امن نیست")
        result: list[dict[str, Any]] = []
        for candidate in sorted(self.root.iterdir(), key=lambda item: item.name.casefold())[:200]:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if not (candidate / ".git").exists():
                continue
            try:
                result.append(self.status(candidate.name))
            except AssistantError:
                # Ignore unrelated or no-longer-selected repositories rather
                # than disclosing their metadata through the integration.
                continue
        return result

    def pull(self, path: str, *, remote: str = "origin", branch: str = "") -> dict[str, Any]:
        repo = self._repo(path)
        remote = self._authenticated_remote(repo, remote, push=False)
        branch = self._current_or_requested_branch(repo, branch)
        output = self._run(
            ["pull", "--ff-only", remote, f"refs/heads/{branch}"],
            repo,
            authenticated=True,
            timeout=300,
        )
        return {"ok": True, "branch": branch, "output": output}

    def push(
        self, path: str, *, remote: str = "origin", branch: str = "", set_upstream: bool = False
    ) -> dict[str, Any]:
        repo = self._repo(path)
        remote = self._authenticated_remote(repo, remote, push=True)
        branch = self._current_or_requested_branch(repo, branch)
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        args.extend([remote, f"refs/heads/{branch}:refs/heads/{branch}"])
        output = self._run(args, repo, authenticated=True, timeout=300)
        return {"ok": True, "branch": branch, "output": output}

    def branch_create(
        self, path: str, branch: str, *, start_point: str = "", switch: bool = True
    ) -> dict[str, Any]:
        repo, branch = self._repo(path), self._ref(branch)
        args = ["switch", "-c", branch] if switch else ["branch", branch]
        if start_point:
            args.append(self._revision(start_point))
        return {"ok": True, "output": self._run(args, repo)}

    def branch_switch(self, path: str, branch: str) -> dict[str, Any]:
        return {"ok": True, "output": self._run(["switch", self._ref(branch)], self._repo(path))}

    def branch_delete(self, path: str, branch: str, *, force: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "output": self._run(
                ["branch", "-D" if force else "-d", self._ref(branch)], self._repo(path)
            ),
        }

    def commit(
        self,
        path: str,
        message: str,
        *,
        paths: Any = None,
        all_tracked: bool = False,
        author_name: str,
        author_email: str,
    ) -> dict[str, Any]:
        repo = self._repo(path)
        if (
            not isinstance(message, str)
            or len(message) > 10_000
            or "\x00" in message
        ):
            raise AssistantError("پیام commit نامعتبر یا بیش از حد بلند است")
        if (
            not isinstance(author_name, str)
            or not author_name.strip()
            or len(author_name.encode("utf-8")) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in author_name)
            or not isinstance(author_email, str)
            or not re.fullmatch(r"[^\s<>@]{1,128}@[^\s<>@]{1,190}", author_email)
        ):
            raise AssistantError("نام یا ایمیل نویسندهٔ commit نامعتبر است")
        if all_tracked:
            self._run(["add", "--update"], repo)
        if paths is not None:
            if (
                not isinstance(paths, list)
                or not paths
                or len(paths) > 1_000
                or not all(
                    isinstance(item, str)
                    and 0 < len(item.encode("utf-8")) <= 4096
                    and not any(
                        ord(character) < 0x20 or ord(character) == 0x7F
                        for character in item
                    )
                    for item in paths
                )
            ):
                raise AssistantError("paths باید فهرست غیرخالی از حداکثر ۱۰۰۰ مسیر معتبر باشد")
            self._run(["add", "--", *paths], repo)
        output = self._run(
            [
                "-c",
                f"user.name={author_name.strip()}",
                "-c",
                f"user.email={author_email}",
                "commit",
                "-m",
                message,
            ],
            repo,
        )
        sha = self._run(["rev-parse", "HEAD"], repo).strip()
        return {"ok": True, "sha": sha, "output": output}

    def tag(self, path: str, tag: str, *, message: str = "", push: bool = False) -> dict[str, Any]:
        repo, tag = self._repo(path), self._ref(tag)
        if not isinstance(message, str) or len(message) > 10_000 or "\x00" in message:
            raise AssistantError("پیام tag نامعتبر یا بیش از حد بلند است")
        args = ["tag", "-a", tag, "-m", message] if message else ["tag", tag]
        output = self._run(args, repo)
        if push:
            remote = self._authenticated_remote(repo, "origin", push=True)
            output += "\n" + self._run(
                ["push", remote, f"refs/tags/{tag}"],
                repo,
                authenticated=True,
                timeout=300,
            )
        return {"ok": True, "output": output.strip()}

    def _authenticated_remote(self, repo: Path, remote: str, *, push: bool) -> str:
        """Refuse to send the OAuth token to a non-configured GitHub host."""
        remote = self._remote(remote)
        args = ["remote", "get-url"]
        if push:
            args.append("--push")
        args.extend(["--all", remote])
        urls = [line.strip() for line in self._run(args, repo).splitlines() if line.strip()]
        if not urls or any(not self._is_allowed_authenticated_url(url) for url in urls):
            raise AssistantError(
                "ریموت Git باید یک URL امن روی میزبان GitHub تنظیم‌شده باشد؛ توکن ارسال نشد"
            )
        return remote

    def _is_allowed_authenticated_url(self, value: str) -> bool:
        return self._allowed_repository_for_url(value) is not None

    def _allowed_repository_for_url(self, value: str) -> str | None:
        if not _bounded_printable(value, _MAX_REMOTE_URL_BYTES):
            return None
        try:
            expected = urlsplit(self.web_url)
            actual = urlsplit(value)
            expected_port = expected.port or (443 if expected.scheme == "https" else 80)
            actual_port = actual.port or (443 if actual.scheme == "https" else 80)
            base_path = expected.path.rstrip("/")
            valid_origin = bool(
                actual.scheme == expected.scheme
                and actual.scheme in {"http", "https"}
                and actual.hostname
                and expected.hostname
                and actual.hostname.casefold() == expected.hostname.casefold()
                and actual_port == expected_port
                and actual.username is None
                and actual.password is None
                and not actual.query
                and not actual.fragment
                and (not base_path or actual.path.startswith(base_path + "/"))
            )
            repository = self._repository_from_path(actual.path, base_path)
            if not valid_origin or repository is None:
                return None
            if (
                self.allowed_repositories is not None
                and repository.casefold() not in self.allowed_repositories
            ):
                return None
            return repository.casefold()
        except ValueError:
            return None

    @staticmethod
    def _repository_from_path(path: str, base_path: str) -> str | None:
        relative = path[len(base_path) :] if base_path and path.startswith(base_path + "/") else path
        parts = relative.strip("/").split("/")
        if len(parts) != 2:
            return None
        owner, repo = parts
        repo = repo.removesuffix(".git")
        if (
            not _REMOTE_RE.fullmatch(owner)
            or not _REMOTE_RE.fullmatch(repo)
            or owner in {".", ".."}
            or repo in {".", ".."}
        ):
            return None
        return f"{owner}/{repo}"

    def _path(self, raw: str, *, must_exist: bool) -> Path:
        candidate = Path(raw).expanduser()
        candidate = (
            candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        )
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise AssistantError("مسیر Git باید داخل ریشهٔ clone تنظیم‌شده باشد") from exc
        if must_exist and not candidate.exists():
            raise AssistantError("مسیر Git وجود ندارد")
        return candidate

    def _repo(self, raw: str) -> Path:
        path = self._path(raw, must_exist=True)
        self._assert_contained_git_directory(path)
        if self.allowed_repositories is not None:
            try:
                fetch_urls = [
                    line.strip()
                    for line in self._run(["remote", "get-url", "--all", "origin"], path).splitlines()
                    if line.strip()
                ]
                push_urls = [
                    line.strip()
                    for line in self._run(
                        ["remote", "get-url", "--push", "--all", "origin"], path
                    ).splitlines()
                    if line.strip()
                ]
            except AssistantError as exc:
                raise AssistantError(
                    "مخزن محلی باید origin یکسان و انتخاب‌شده‌ای برای fetch و push داشته باشد"
                ) from exc
            repositories = [
                self._allowed_repository_for_url(url) for url in [*fetch_urls, *push_urls]
            ]
            if (
                not fetch_urls
                or not push_urls
                or any(repository is None for repository in repositories)
                or len(set(repositories)) != 1
            ):
                raise AssistantError(
                    "مخزن محلی باید origin یکسان و انتخاب‌شده‌ای برای fetch و push داشته باشد"
                )
        return path

    def _assert_contained_git_directory(self, path: Path) -> tuple[Path, Path]:
        marker = path / ".git"
        try:
            if marker.is_dir() and not marker.is_symlink():
                git_directory = marker.resolve(strict=True)
            elif (
                marker.is_file()
                and not marker.is_symlink()
                and marker.stat().st_size <= 4096
            ):
                raw = marker.read_text(encoding="utf-8")
                if not raw.startswith("gitdir:"):
                    raise ValueError("invalid gitdir marker")
                pointer = raw.removeprefix("gitdir:").strip()
                if not pointer or any(
                    ord(character) < 0x20 or ord(character) == 0x7F for character in pointer
                ):
                    raise ValueError("invalid gitdir pointer")
                candidate = Path(pointer)
                git_directory = (
                    candidate.resolve(strict=True)
                    if candidate.is_absolute()
                    else (path / candidate).resolve(strict=True)
                )
                if not git_directory.is_dir():
                    raise ValueError("gitdir is not a directory")
            else:
                raise ValueError("missing gitdir")
            git_directory.relative_to(self.root)
            common_directory = git_directory
            common_marker = git_directory / "commondir"
            if common_marker.exists() or common_marker.is_symlink():
                if (
                    not common_marker.is_file()
                    or common_marker.is_symlink()
                    or common_marker.stat().st_size > 4096
                ):
                    raise ValueError("invalid common gitdir")
                common_raw = common_marker.read_text(encoding="utf-8")
                common_value = common_raw.rstrip("\r\n")
                if (
                    not common_value
                    or any(
                        ord(character) < 0x20 or ord(character) == 0x7F
                        for character in common_value
                    )
                    or common_raw not in {common_value, common_value + "\n", common_value + "\r\n"}
                ):
                    raise ValueError("invalid common gitdir")
                common = Path(common_value)
                common_directory = (
                    common.resolve(strict=True)
                    if common.is_absolute()
                    else (git_directory / common).resolve(strict=True)
                )
                common_directory.relative_to(self.root)
                if not common_directory.is_dir():
                    raise ValueError("common gitdir is not a directory")
            return git_directory, common_directory
        except (OSError, UnicodeError, ValueError) as exc:
            raise AssistantError(
                "دایرکتوری داخلی Git باید به‌طور کامل داخل ریشهٔ clone تنظیم‌شده باشد"
            ) from exc

    def _assert_contained_git_configuration(self, path: Path) -> None:
        """Require every repository config file to be regular and contained."""
        if not (path / ".git").exists() and not (path / ".git").is_symlink():
            return  # clone destination: no repository-local config exists yet
        git_directory, common_directory = self._assert_contained_git_directory(path)
        config = common_directory / "config"
        candidates = {config, git_directory / "config.worktree"}
        try:
            for candidate in candidates:
                if candidate == config and not candidate.exists():
                    raise ValueError("missing repository config")
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                if candidate.is_symlink():
                    raise ValueError("symlinked repository config")
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.root)
                metadata = candidate.stat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > _MAX_CONFIG_BYTES
                    or metadata.st_nlink != 1
                ):
                    raise ValueError("unsafe repository config")
        except (OSError, ValueError) as exc:
            raise AssistantError(
                "فایل‌های پیکربندی Git باید معمولی، محدود و داخل ریشهٔ clone باشند"
            ) from exc

    def _run(
        self, args: list[str], cwd: Path, *, authenticated: bool = False, timeout: int = 120
    ) -> str:
        executable = shutil.which("git")
        if not executable:
            raise DependencyMissing(
                "Git نصب یا در PATH موجود نیست", install_hint="https://git-scm.com/downloads"
            )
        env = os.environ.copy()
        # Caller-provided Git environment can redirect config/work trees or
        # inject command-line config. Build a deterministic environment.
        for key in list(env):
            if key.startswith("GIT_") or key in {"GH_TOKEN", "GITHUB_TOKEN"}:
                env.pop(key, None)
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_PAGER": "cat",
                "GIT_EDITOR": "true",
                "GIT_SEQUENCE_EDITOR": "true",
                "GIT_MERGE_AUTOEDIT": "no",
            }
        )
        token = ""
        credential_file: Path | None = None
        temporary_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            # A repository may contain hooks or executable filter/fsmonitor
            # configuration. Isolate every Git invocation, including reads and
            # local-only writes—not just commands that temporarily hold a token.
            env["GIT_CONFIG_GLOBAL"] = os.devnull
            self._assert_contained_git_configuration(cwd)
            self._assert_safe_local_config(executable, cwd, env)
            temporary_dir = tempfile.TemporaryDirectory(prefix="pla-git-")
            temporary = Path(temporary_dir.name)
            hooks = temporary / "empty-hooks"
            hooks.mkdir()
            git_options = [
                "--literal-pathspecs",
                "-c",
                "credential.helper=",
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "diff.external=",
                "-c",
                "interactive.diffFilter=",
                "-c",
                "log.showSignature=false",
                "-c",
                "merge.verifySignatures=false",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                "-c",
                "submodule.recurse=false",
                "-c",
                "fetch.recurseSubmodules=false",
                "-c",
                "protocol.allow=never",
                "-c",
                f"protocol.{urlsplit(self.web_url).scheme}.allow=always",
            ]
            if authenticated:
                token = self._token_provider()
                if (
                    not isinstance(token, str)
                    or not token
                    or len(token) > 16_384
                    or token != token.strip()
                    or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
                ):
                    raise AssistantError("توکن GitHub برای احراز هویت Git معتبر نیست")
                credential_file = temporary / "credential"
                askpass = _write_askpass(temporary, token)
                env["GIT_ASKPASS"] = str(askpass)
                git_options.extend(
                    [
                        "-c",
                        "http.followRedirects=false",
                        "-c",
                        "http.sslVerify=true",
                        "-c",
                        "http.proxy=",
                    ]
                )
            command = [executable, *git_options, "-C", str(cwd), *args]
            # Redirect to files so a hostile repository cannot make Python
            # allocate unbounded memory through stdout/stderr. Reject oversized
            # output rather than returning a misleading truncated parse.
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                completed = subprocess.run(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    check=False,
                )
                stdout_size = stdout_file.tell()
                stderr_size = stderr_file.tell()
                if stdout_size + stderr_size > _MAX_GIT_OUTPUT_BYTES:
                    raise AssistantError("خروجی عملیات Git بیش از حد مجاز بود و رد شد")
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().decode("utf-8", errors="replace")
                stderr = stderr_file.read().decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise AssistantError("عملیات Git به دلیل پایان مهلت متوقف شد") from exc
        finally:
            cleanup_error: OSError | None = None
            if credential_file is not None:
                try:
                    credential_file.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_error = exc
            if temporary_dir is not None:
                try:
                    temporary_dir.cleanup()
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise AssistantError("پاک‌سازی فایل موقت اعتبار GitHub ناموفق بود") from cleanup_error
        output = (stdout + ("\n" + stderr if stderr else "")).strip()
        if token:
            output = output.replace(token, "[REDACTED]")
        if completed.returncode:
            raise AssistantError(f"Git با کد {completed.returncode} شکست خورد: {output[-2000:]}")
        return output

    @staticmethod
    def _assert_safe_local_config(executable: str, cwd: Path, env: dict[str, str]) -> None:
        """Reject repository config capable of executing code or rerouting authentication."""
        if not (cwd / ".git").exists():
            return  # clone destination: no repository-local config exists yet
        probe_env = dict(env)
        probe_env["GIT_CONFIG_GLOBAL"] = os.devnull
        completed = subprocess.run(
            [
                executable,
                "-C",
                str(cwd),
                "config",
                "--local",
                "--no-includes",
                "--null",
                "--list",
            ],
            cwd=str(cwd),
            env=probe_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode:
            raise AssistantError("بررسی پیکربندی امن مخزن Git ناموفق بود؛ توکن ارسال نشد")
        if len(completed.stdout.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise AssistantError("خروجی پیکربندی محلی Git بیش از حد مجاز است؛ توکن ارسال نشد")
        entries: list[tuple[str, str]] = []
        for record in completed.stdout.split("\0"):
            if not record:
                continue
            key, separator, value = record.partition("\n")
            if not separator or not key:
                raise AssistantError("قالب پیکربندی محلی Git نامعتبر است؛ توکن ارسال نشد")
            entries.append((key.casefold(), value))
        false_only = {
            "commit.gpgsign",
            "core.bare",
            "core.fsmonitor",
            "log.showsignature",
            "tag.gpgsign",
        }
        false_values = {"", "false", "no", "off", "0"}
        exact = {
            "core.askpass",
            "core.gitproxy",
            "core.hookspath",
            "core.sshcommand",
            "core.worktree",
            "interactive.difffilter",
        }
        suffixes = (
            ".cmd",
            ".command",
            ".driver",
            ".editor",
            ".pager",
            ".program",
            ".proxy",
            ".proxyauthmethod",
            ".receivepack",
            ".textconv",
            ".uploadpack",
            ".vcs",
        )
        prefixes = (
            "alias.",
            "credential.",
            "diff.",
            "filter.",
            "gpg.",
            "http.",
            "include.",
            "includeif.",
            "merge.",
            "pager.",
            "protocol.",
            "submodule.",
            "url.",
        )
        for key, value in entries:
            boolean_override = key in false_only and value.strip().casefold() not in false_values
            if (
                boolean_override
                or key in exact
                or key.startswith(prefixes)
                or key.endswith(suffixes)
            ):
                raise AssistantError(
                    "پیکربندی محلی مخزن می‌تواند احراز هویت Git را تغییر دهد؛ توکن ارسال نشد"
                )

    def _current_or_requested_branch(self, repo: Path, value: str) -> str:
        branch = value or self._run(["branch", "--show-current"], repo).strip()
        if not branch:
            raise AssistantError("برای مخزن detached HEAD باید نام branch صریحاً مشخص شود")
        return self._ref(branch)

    @staticmethod
    def _ref(value: str) -> str:
        parts = value.split("/") if isinstance(value, str) else []
        if (
            not _bounded_printable(value, 512)
            or not _REF_RE.fullmatch(value)
            or value in {"@", "HEAD"}
            or value.startswith(("/", "refs/"))
            or value.endswith((".", "/"))
            or "@{" in value
            or any(
                not part or part.startswith(".") or part.casefold().endswith(".lock")
                for part in parts
            )
        ):
            raise AssistantError("نام branch/tag نامعتبر است")
        return value

    @classmethod
    def _revision(cls, value: str) -> str:
        if isinstance(value, str) and (
            value == "HEAD" or re.fullmatch(r"[0-9a-fA-F]{40}", value)
        ):
            return value
        return cls._ref(value)

    @staticmethod
    def _remote(value: str) -> str:
        if not _REMOTE_RE.fullmatch(value):
            raise AssistantError("نام remote نامعتبر است")
        return value


def _write_askpass(directory: Path, token: str, *, windows: bool | None = None) -> Path:
    secret = directory / "credential"
    secret.write_text(token, encoding="utf-8")
    secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        path = directory / "askpass.cmd"
        path.write_text(
            '@echo off\necho %~1 | findstr /I "Username" >nul\n'
            "if %errorlevel%==0 goto username\n"
            'type "%~dp0credential"\necho.\nexit /b\n'
            ":username\necho x-access-token\n",
            encoding="utf-8",
        )
    else:
        path = directory / "askpass.sh"
        path.write_text(
            '#!/bin/sh\nIFS= read -r pla_token < "${0%/*}/credential"\n'
            'case "$1" in *Username*) printf "%s\\n" "x-access-token";; '
            '*) printf "%s\\n" "$pla_token";; esac\n',
            encoding="utf-8",
        )
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _bounded_printable(value: Any, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value or any(not character.isprintable() for character in value):
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def _redact_url(value: str) -> str:
    """Render a remote without user-info, query credentials, or malformed data."""
    if not _bounded_printable(value, _MAX_REMOTE_URL_BYTES):
        return "[invalid remote URL]"
    try:
        if "://" in value:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            if not parsed.scheme or (parsed.scheme != "file" and not hostname):
                return "[invalid remote URL]"
            if hostname:
                host = f"[{hostname}]" if ":" in hostname else hostname
                if parsed.port:
                    host += f":{parsed.port}"
            else:
                host = ""
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        # Git's scp-like syntax is ``[user@]host:path``. Drop user-info even
        # when it is a legitimate ``git`` username so an embedded token can
        # never reach UI, model context, or logs.
        scp = re.fullmatch(r"(?:[^/@:]+@)?([^/@:]+):(.+)", value)
        if scp:
            return f"{scp.group(1)}:{scp.group(2).split('?', 1)[0].split('#', 1)[0]}"
        # Local path remotes do not carry URL user-info, but strip URL-style
        # query/fragment text defensively.
        if "@" not in value:
            return value.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        pass
    return "[invalid remote URL]"
