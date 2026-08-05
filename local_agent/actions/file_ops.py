"""File operations: safe read / write / move / copy / delete / search.

Paths are sandboxed to the assistant's work directory.  With
``safety.full_system_access`` enabled the whole filesystem becomes
reachable, but sensitive files (``.ssh``, ``.env``, credentials, ...)
stay blocked in *both* modes and destructive actions still ask for
confirmation.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from .registry import ActionContext, ActionRegistry, Risk, risk

logger = get_logger("actions.file_ops")


def register_file_ops(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="read_file",
        description=(
            "Read a UTF-8 text file from disk. Returns the content or a substring. "
            "Optional line range for large files. Path is relative to the working "
            "directory or absolute within the work dir."
        ),
        parameters={
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "max_lines": {"type": "integer"},
        },
        required=("path",),
    )(read_file)

    registry.decorator(
        name="write_file",
        description=(
            "Write a UTF-8 text file (atomic). Overwrites existing files. DESTRUCTIVE."
        ),
        parameters={"path": {"type": "string"}, "content": {"type": "string"}},
        required=("path", "content"),
    )(write_file)

    registry.decorator(
        name="append_file",
        description=(
            "Append text to a file. Creates the file if it does not exist. "
            "DESTRUCTIVE — modifies existing files."
        ),
        parameters={
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        required=("path", "content"),
    )(append_file)

    registry.decorator(
        name="copy_path",
        description=(
            "Copy a file or directory. If the source is a directory, copies "
            "recursively. DESTRUCTIVE."
        ),
        parameters={
            "source": {"type": "string"},
            "destination": {"type": "string"},
        },
        required=("source", "destination"),
    )(copy_path)

    registry.decorator(
        name="list_directory",
        description="List the immediate children of a directory.",
        parameters={"path": {"type": "string"}},
    )(list_directory)

    registry.decorator(
        name="make_directory",
        description="Create a directory (and parents). DESTRUCTIVE.",
        parameters={"path": {"type": "string"}},
        required=("path",),
    )(make_directory)

    registry.decorator(
        name="move_path",
        description="Move a file or directory. DESTRUCTIVE.",
        parameters={"source": {"type": "string"}, "destination": {"type": "string"}},
        required=("source", "destination"),
    )(move_path)

    registry.decorator(
        name="delete_path",
        description=(
            "Delete a file or directory. Deleting a directory requires recursive=True. "
            "DESTRUCTIVE."
        ),
        parameters={
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
        },
        required=("path",),
    )(delete_path)

    registry.decorator(
        name="search_files",
        description=(
            "Search inside text files under a directory for a query string. Returns "
            "matching paths with line numbers."
        ),
        parameters={
            "query": {"type": "string"},
            "path": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        required=("query",),
    )(search_files)

    registry.decorator(
        name="zip_directory",
        description=(
            "Compress a directory into a ZIP archive. Returns the archive path."
        ),
        parameters={
            "source": {"type": "string", "description": "Directory to compress."},
            "destination": {"type": "string", "description": "Output ZIP path."},
        },
        required=("source",),
    )(zip_directory)

    registry.decorator(
        name="unzip_file",
        description=(
            "Extract a ZIP archive to a directory. Returns the extraction directory."
        ),
        parameters={
            "source": {"type": "string", "description": "ZIP file to extract."},
            "destination": {"type": "string", "description": "Output directory."},
        },
        required=("source",),
    )(unzip_file)

    registry.decorator(
        name="download_file",
        description=(
            "Download a file from a URL and save it to disk. Returns the saved path. "
            "SAFE — only writes to the workspace."
        ),
        parameters={
            "url": {"type": "string", "description": "URL to download."},
            "path": {"type": "string", "description": "Local filename (relative to workspace)."},
        },
        required=("url",),
    )(download_file)

    registry.decorator(
        name="get_env",
        description=(
            "Read the value of an environment variable. Returns an empty string "
            "if the variable is not set."
        ),
        parameters={
            "name": {"type": "string", "description": "Variable name."},
        },
        required=("name",),
    )(get_env)

    registry.decorator(
        name="set_env",
        description=(
            "Set an environment variable for the current session. "
            "DESTRUCTIVE — modifies the process environment."
        ),
        parameters={
            "name": {"type": "string", "description": "Variable name."},
            "value": {"type": "string", "description": "Variable value."},
        },
        required=("name", "value"),
    )(set_env)

    registry.decorator(
        name="wait",
        description=(
            "Wait for a specified number of seconds. Useful for GUI sequencing. "
            "SAFE."
        ),
        parameters={
            "seconds": {"type": "number", "description": "Duration in seconds."},
        },
        required=("seconds",),
    )(wait_action)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SENSITIVE_NAMES = {
    ".env", ".envrc", "id_rsa", "id_ed25519", "credentials",
    "credentials.json", ".netrc", ".git-credentials"
}
SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".config/gcloud", ".docker"}


def _assert_not_sensitive(path: Path, work_dir: Path) -> None:
    """Block access to secret or credential files even inside workspace."""
    try:
        rel = path.resolve().relative_to(work_dir.resolve())
        parts = rel.parts
    except (ValueError, OSError):
        # Outside workspace is already blocked by _resolve_path, but keep safe
        parts = path.parts
    lower_parts = {p.lower() for p in parts}
    # Check for sensitive directories in path
    for sensitive in SENSITIVE_PARTS:
        matches_part = sensitive.split("/")[-1].lower() in lower_parts
        if matches_part and (
            any(sensitive.lower() in part for part in lower_parts)
            or sensitive.lower() in str(path).lower().replace("\\", "/")
        ):
            raise AssistantError(f"دسترسی به مسیر حساس مسدود شد: {sensitive}")
    name = path.name.lower()
    if name in SENSITIVE_NAMES or name.startswith(".env.") or "credential" in name:
        raise AssistantError(f"فایل محرمانه محافظت شده است: {path.name}")


def _resolve_path(raw: str, work_dir: Path, *, full_system_access: bool = False) -> Path:
    """Resolve a user-supplied path to an absolute Path.

    By default the path is sandboxed to ``work_dir``.  With
    ``full_system_access=True`` the whole filesystem becomes reachable,
    but sensitive files (``.ssh``, ``.env``, credentials, ...) stay
    blocked in both modes via :func:`_assert_not_sensitive`.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise AssistantError("path must be a non-empty string")
    if len(raw) > 1024:
        raise AssistantError("مسیر بیش از حد طولانی است")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (work_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not full_system_access:
        # Enforce sandbox: resolved path must be inside work_dir (or work_dir itself)
        try:
            work_resolved = work_dir.resolve()
            # allow the work_dir itself
            if candidate != work_resolved:
                candidate.relative_to(work_resolved)
        except ValueError:
            raise AssistantError(f"مسیر خارج از فضای کاری است: {raw!r} — فقط داخل workspace مجاز است")
        except OSError as exc:
            raise AssistantError(f"خواندن مسیر ممکن نشد {raw!r}: {exc}") from exc
    _assert_not_sensitive(candidate, work_dir)
    return candidate


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def read_file(
    *,
    path: str,
    start_line: int = 1,
    max_lines: int = 400,
    context: ActionContext,
) -> str:
    target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not target.is_file():
        raise AssistantError(f"not a file: {target}")
    # Check for binary files
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise AssistantError(f"could not stat {target}: {exc}") from exc
    if size > 10_000_000:
        return f"فایل بزرگ است ({size / 1_000_000:.1f} مگابایت). از پارامتر start_line و max_lines برای خواندن بخشی استفاده کنید."
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary file
        return f"فایل باینری است و نمی‌توان آن را به صورت متنی خواند. اندازه: {size} بایت."
    except OSError as exc:
        raise AssistantError(f"could not read {target}: {exc}") from exc
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), start + max(1, int(max_lines or 400)) - 1)
    selected = lines[start - 1 : end]
    rendered = "\n".join(f"{i:5d} | {line}" for i, line in enumerate(selected, start))
    if end < len(lines):
        rendered += f"\n... ({len(lines) - end} more lines)"
    return rendered or "(empty file)"


@risk(Risk.DESTRUCTIVE)
def write_file(
    *,
    path: str,
    content: str,
    context: ActionContext,
) -> str:
    target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise AssistantError(f"could not write {target}: {exc}") from exc
    return f"wrote {len(content)} characters to {target}"


@risk(Risk.DESTRUCTIVE)
def append_file(
    *,
    path: str,
    content: str,
    context: ActionContext,
) -> str:
    target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        raise AssistantError(f"could not append to {target}: {exc}") from exc
    return f"appended {len(content)} characters to {target}"


@risk(Risk.DESTRUCTIVE)
def copy_path(
    *,
    source: str,
    destination: str,
    context: ActionContext,
) -> str:
    src = _resolve_path(source, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    dst = _resolve_path(destination, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not src.exists():
        raise AssistantError(f"source does not exist: {src}")
    if dst.exists():
        raise AssistantError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
    except OSError as exc:
        raise AssistantError(f"copy failed: {exc}") from exc
    return f"copied {src} -> {dst}"


@risk(Risk.SAFE)
def list_directory(*, path: str = ".", context: ActionContext) -> str:
    target = _resolve_path(path or ".", context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not target.is_dir():
        raise AssistantError(f"not a directory: {target}")
    try:
        entries = list(target.iterdir())
    except OSError as exc:
        raise AssistantError(f"could not list {target}: {exc}") from exc
    entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for entry in entries[:200]:
        marker = "DIR " if entry.is_dir() else "FILE"
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = 0
        lines.append(f"  [{marker}] {entry.name}  ({size} bytes)")
    if len(entries) > 200:
        lines.append(f"  ... ({len(entries) - 200} more)")
    return "\n".join(lines) or "(empty directory)"


@risk(Risk.DESTRUCTIVE)
def make_directory(*, path: str, context: ActionContext) -> str:
    target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if target.exists() and not target.is_dir():
        raise AssistantError(f"a file already exists at {target}")
    target.mkdir(parents=True, exist_ok=True)
    return f"created {target}"


@risk(Risk.DESTRUCTIVE)
def move_path(*, source: str, destination: str, context: ActionContext) -> str:
    src = _resolve_path(source, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    dst = _resolve_path(destination, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not src.exists():
        raise AssistantError(f"source does not exist: {src}")
    if dst.exists():
        raise AssistantError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src} -> {dst}"


@risk(Risk.SYSTEM)
def delete_path(*, path: str, recursive: bool = False, context: ActionContext) -> str:
    target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not target.exists():
        return f"path did not exist: {target}"
    if target.is_dir() and not recursive:
        raise AssistantError("refusing to delete a directory without recursive=True")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise AssistantError(f"delete failed: {exc}") from exc
    return f"deleted {target}"


@risk(Risk.SAFE)
def search_files(
    *,
    query: str,
    path: str = ".",
    max_results: int = 50,
    context: ActionContext,
) -> str:
    if not isinstance(query, str) or not query.strip():
        raise AssistantError("query must be a non-empty string")
    if len(query) > 500:
        raise AssistantError("عبارت جستجو خیلی طولانی است")
    target = _resolve_path(path or ".", context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not target.is_dir():
        raise AssistantError(f"مسیر پوشه نیست: {target}")
    needle = query
    limit = max(1, min(int(max_results or 50), 500))
    matches: list[str] = []
    work_root = context.work_dir.resolve()
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", "dist", "build"}]
        for filename in files:
            # Skip sensitive files
            if filename.lower() in SENSITIVE_NAMES:
                continue
            full = Path(root) / filename
            try:
                # Skip hidden sensitive parts
                rel_for_check = full.resolve().relative_to(work_root)
                if ".ssh" in rel_for_check.parts or ".gnupg" in rel_for_check.parts:
                    continue
            except ValueError:
                continue
            try:
                if full.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            try:
                with full.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if needle in line:
                            try:
                                rel = full.resolve().relative_to(work_root)
                            except ValueError:
                                rel = full
                            matches.append(f"{rel}:{line_number}: {line.rstrip()[:200]}")
                            if len(matches) >= limit:
                                break
            except OSError:
                continue
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
    if not matches:
        return f"موردی برای {query!r} در {target.relative_to(work_root) if target.resolve() != work_root else '.'} پیدا نشد"
    header = f"جستجو: {query} | مسیر: {target.resolve().relative_to(work_root) if target.resolve() != work_root else '.'} | نتایج: {len(matches)}"
    return header + "\n" + "\n".join(matches)


@risk(Risk.SAFE)
def zip_directory(
    *,
    source: str,
    destination: str = "",
    context: ActionContext,
) -> str:
    src = _resolve_path(source, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not src.is_dir():
        raise AssistantError(f"not a directory: {src}")
    if destination:
        dst = _resolve_path(destination, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    else:
        dst = src.with_suffix(".zip")
    if dst.suffix.lower() != ".zip":
        dst = dst.with_suffix(dst.suffix + ".zip")
    try:
        with zipfile.ZipFile(str(dst), "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(src):
                for filename in files:
                    full = Path(root) / filename
                    arcname = full.relative_to(src)
                    zf.write(str(full), str(arcname))
    except OSError as exc:
        raise AssistantError(f"zip failed: {exc}") from exc
    size_mb = dst.stat().st_size / (1024 * 1024)
    return f"created {dst} ({size_mb:.1f} MB)"


@risk(Risk.SAFE)
def unzip_file(
    *,
    source: str,
    destination: str = "",
    context: ActionContext,
) -> str:
    src = _resolve_path(source, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    if not src.is_file():
        raise AssistantError(f"not a file: {src}")
    if destination:
        dst = _resolve_path(destination, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    else:
        dst = src.with_suffix("")
    dst.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(src), "r") as zf:
            zf.extractall(str(dst))
    except (zipfile.BadZipFile, OSError) as exc:
        raise AssistantError(f"unzip failed: {exc}") from exc
    return f"extracted to {dst}"


@risk(Risk.SAFE)
def download_file(
    *,
    url: str,
    path: str = "",
    context: ActionContext,
) -> str:
    import requests

    if not url or not isinstance(url, str):
        raise AssistantError("url must be a non-empty string")
    if len(url) > 2048:
        raise AssistantError("URL بیش از حد طولانی است")
    if not url.startswith(("http://", "https://")):
        raise AssistantError("url must start with http:// or https://")
    if path:
        target = _resolve_path(path, context.work_dir, full_system_access=context.runtime.settings.safety.full_system_access)
    else:
        # Derive filename from URL
        from urllib.parse import urlparse
        parsed = urlparse(url)
        name = Path(parsed.path).name or "download"
        # Sanitize filename
        name = "".join(c for c in name if c.isalnum() or c in "._-")[:128] or "download"
        target = context.work_dir / name
        # Ensure still inside workspace
        target = _resolve_path(str(target), context.work_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = 100 * 1024 * 1024  # 100 MB limit
    try:
        response = requests.get(
            url,
            stream=True,
            timeout=60,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LocalAssistant/2.0)"},
        )
        response.raise_for_status()
        # Check content-length if provided
        clen = response.headers.get("Content-Length")
        if clen:
            try:
                if int(clen) > max_bytes:
                    raise AssistantError(f"فایل خیلی بزرگ است ({int(clen)//1024//1024} MB > 100 MB)")
            except ValueError:
                pass
        downloaded = 0
        with target.open("wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise AssistantError("دانلود بیش از 100 MB شد و متوقف شد")
                f.write(chunk)
    except requests.RequestException as exc:
        # Clean partial file on failure
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise AssistantError(f"دانلود ناموفق بود: {exc}") from exc
    except OSError as exc:
        raise AssistantError(f"ذخیره در {target} ممکن نشد: {exc}") from exc
    try:
        size_kb = target.stat().st_size / 1024
    except OSError:
        size_kb = 0
    return f"دانلود شد: {url} -> {target} ({size_kb:.1f} KB)"


@risk(Risk.SAFE)
def get_env(*, name: str, context: ActionContext) -> str:
    if not isinstance(name, str) or not name.strip():
        raise AssistantError("name must be a non-empty string")
    return os.environ.get(name.strip(), "")


@risk(Risk.DESTRUCTIVE)
def set_env(*, name: str, value: str, context: ActionContext) -> str:
    if not isinstance(name, str) or not name.strip():
        raise AssistantError("name must be a non-empty string")
    key = name.strip()
    os.environ[key] = str(value)
    return f"set {key}={value!r}"


@risk(Risk.SAFE)
def wait_action(*, seconds: float, context: ActionContext) -> str:
    import time

    duration = max(0.1, min(float(seconds), 60))
    time.sleep(duration)
    return f"waited {duration:.1f} seconds"
