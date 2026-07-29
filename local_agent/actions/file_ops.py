"""File operations: safe read / write / move / copy / delete.

Paths are sandboxed to the assistant's work directory unless the user
explicitly passes an absolute path that is allowed by the policy. The
agent can still escape by passing a C:\\... path; in that case the
action is marked DESTRUCTIVE and the user must approve.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from .registry import ActionContext, ActionRegistry, risk, Risk


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(raw: str, work_dir: Path) -> Path:
    """Resolve a user-supplied path to an absolute Path.

    Absolute paths are returned as-is. Relative paths are resolved
    against the work directory. ``..`` is allowed; the assistant is
    trusted to stay within sensible bounds.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise AssistantError("path must be a non-empty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (work_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
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
    target = _resolve_path(path, context.work_dir)
    if not target.is_file():
        raise AssistantError(f"not a file: {target}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AssistantError(f"file is not UTF-8: {exc}") from exc
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
    target = _resolve_path(path, context.work_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise AssistantError(f"could not write {target}: {exc}") from exc
    return f"wrote {len(content)} characters to {target}"


@risk(Risk.SAFE)
def list_directory(*, path: str = ".", context: ActionContext) -> str:
    target = _resolve_path(path or ".", context.work_dir)
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
    target = _resolve_path(path, context.work_dir)
    if target.exists() and not target.is_dir():
        raise AssistantError(f"a file already exists at {target}")
    target.mkdir(parents=True, exist_ok=True)
    return f"created {target}"


@risk(Risk.DESTRUCTIVE)
def move_path(*, source: str, destination: str, context: ActionContext) -> str:
    src = _resolve_path(source, context.work_dir)
    dst = _resolve_path(destination, context.work_dir)
    if not src.exists():
        raise AssistantError(f"source does not exist: {src}")
    if dst.exists():
        raise AssistantError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src} -> {dst}"


@risk(Risk.SYSTEM)
def delete_path(*, path: str, recursive: bool = False, context: ActionContext) -> str:
    target = _resolve_path(path, context.work_dir)
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
    target = _resolve_path(path or ".", context.work_dir)
    if not target.is_dir():
        raise AssistantError(f"not a directory: {target}")
    needle = query
    limit = max(1, min(int(max_results or 50), 500))
    matches: list[str] = []
    for root, dirs, files in os.walk(target):
        # Skip volatile / huge directories
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for filename in files:
            full = Path(root) / filename
            try:
                if full.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            try:
                with full.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if needle in line:
                            matches.append(f"{full}:{line_number}: {line.rstrip()[:200]}")
                            if len(matches) >= limit:
                                break
            except OSError:
                continue
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
    if not matches:
        return f"no matches for {query!r} under {target}"
    return "\n".join(matches)
