"""Process control: list, inspect, and stop running processes."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.process")


def register_process_control(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="list_processes",
        description=(
            "List currently running processes with PID, name, and (Windows) memory "
            "in MB. Optional filter limits the output to a substring of the name."
        ),
        parameters={
            "filter": {"type": "string", "description": "Substring filter (case-insensitive)."},
            "max_results": {"type": "integer", "description": "Limit (default 50)."},
        },
    )(list_processes)

    registry.decorator(
        name="kill_process",
        description=(
            "Kill a process by PID. This is DESTRUCTIVE and always requires "
            "confirmation. Use close_application when possible."
        ),
        parameters={
            "pid": {"type": "integer", "description": "Process ID to terminate."},
        },
        required=("pid",),
    )(kill_process)

    registry.decorator(
        name="open_task_manager",
        description="Open the Windows Task Manager (taskmgr.exe).",
        parameters={},
    )(open_task_manager)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_processes(
    *, filter: str = "", max_results: int = 50, context: ActionContext
) -> str:
    needle = (filter or "").strip().lower()
    limit = max(1, min(int(max_results or 50), 500))

    if os.name == "nt":
        try:
            script = (
                "$ErrorActionPreference='SilentlyContinue'; "
                "Get-Process | Select-Object Id,ProcessName,@{N='MemMB';E={[math]::Round($_.WorkingSet64/1MB,1)}} "
                "| Sort-Object MemMB -Descending | ConvertTo-Csv -NoTypeInformation"
            )
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"could not list processes: {exc}") from exc
        if completed.returncode != 0:
            raise AssistantError(f"powershell failed: {completed.stderr[:200]}")
        lines = [ln for ln in completed.stdout.splitlines() if ln.strip()]
        if len(lines) <= 1:
            return "no processes returned by PowerShell."
        # Skip header line
        rows: list[str] = []
        for line in lines[1:]:
            try:
                pid_str, name, mem = [c.strip() for c in line.split(",", 2)]
            except ValueError:
                continue
            if needle and needle not in name.lower():
                continue
            rows.append(f"  • {name:30s} pid={pid_str:>6}  {mem} MB")
            if len(rows) >= limit:
                break
        if not rows:
            return f"no processes matched filter {needle!r}."
        return "\n".join(rows)

    # POSIX fallback (used in tests/dev). Reads /proc.
    rows = []
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as handle:
                name = handle.read().strip()
        except OSError:
            continue
        if needle and needle not in name.lower():
            continue
        rows.append(f"  • {name:30s} pid={entry}")
        if len(rows) >= limit:
            break
    return "\n".join(rows) or f"no processes matched filter {needle!r}."


@risk(Risk.SYSTEM)
def kill_process(*, pid: int, context: ActionContext) -> str:
    if pid <= 0:
        raise AssistantError("pid must be positive")
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"taskkill failed: {exc}") from exc
        if completed.returncode == 0:
            return f"killed pid {pid}"
        raise AssistantError(
            f"taskkill exit {completed.returncode}: "
            f"{(completed.stdout + completed.stderr).strip()[:200]}"
        )
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"pid {pid} was not running"
    except PermissionError as exc:
        raise AssistantError(f"cannot kill {pid}: {exc}") from exc
    return f"sent SIGTERM to pid {pid}"


@risk(Risk.SAFE)
def open_task_manager(*, context: ActionContext) -> str:
    if os.name != "nt":
        return "task manager is Windows-only; try `ps` or `top`."
    try:
        subprocess.Popen(["taskmgr"], close_fds=True)
    except OSError as exc:
        raise AssistantError(f"could not start task manager: {exc}") from exc
    return "opened task manager."
