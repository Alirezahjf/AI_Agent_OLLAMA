"""Process control: list, inspect, and stop running processes.

Cross-platform: uses psutil when available, falls back to
PowerShell on Windows and /proc or ps on Linux.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.platform import is_linux, is_windows
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.process")


def register_process_control(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="list_processes",
        description=(
            "List currently running processes with PID, name, and memory "
            "in MB. Optional filter limits the output to a substring of the name. "
            "Works on Windows and Linux."
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
        description="Open the system's task/process manager (taskmgr on Windows, gnome-system-monitor on Linux).",
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

    # Try psutil first (cross-platform)
    try:
        import psutil
        return _list_psutil(psutil, needle, limit)
    except ImportError:
        pass

    # Platform-specific fallback
    if is_windows():
        return _list_powershell(needle, limit)
    return _list_linux_fallback(needle, limit)


def _list_psutil(psutil: Any, needle: str, limit: int) -> str:
    """List processes using psutil (cross-platform)."""
    rows: list[str] = []
    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name", "?")
            pid = info.get("pid", 0)
            mem = info.get("memory_info")
            mem_mb = round(mem.rss / (1024 * 1024), 1) if mem else 0
            procs.append((name, pid, mem_mb))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    procs.sort(key=lambda p: p[2], reverse=True)
    for name, pid, mem_mb in procs:
        if needle and needle not in name.lower():
            continue
        rows.append(f"  • {name:30s} pid={pid:>6}  {mem_mb} MB")
        if len(rows) >= limit:
            break
    if not rows:
        return f"no processes matched filter {needle!r}."
    return "\n".join(rows)


def _list_powershell(needle: str, limit: int) -> str:
    """List processes using PowerShell (Windows-only)."""
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


def _list_linux_fallback(needle: str, limit: int) -> str:
    """List processes using /proc or ps on Linux."""
    # Try ps first
    if shutil.which("ps"):
        try:
            completed = subprocess.run(
                ["ps", "-eo", "pid,comm,pcpu,pmem", "--sort=-pmem"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                lines = completed.stdout.splitlines()
                rows: list[str] = []
                for line in lines[1:]:  # skip header
                    parts = line.split(None, 3)
                    if len(parts) < 2:
                        continue
                    pid, name = parts[0], parts[1]
                    if needle and needle not in name.lower():
                        continue
                    cpu = parts[2] if len(parts) > 2 else ""
                    mem = parts[3] if len(parts) > 3 else ""
                    rows.append(f"  • {name:30s} pid={pid:>6}  cpu={cpu}% mem={mem}%")
                    if len(rows) >= limit:
                        break
                if not rows:
                    return f"no processes matched filter {needle!r}."
                return "\n".join(rows)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Fall back to /proc
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
    if is_windows():
        return _kill_windows(pid)
    return _kill_posix(pid)


def _kill_windows(pid: int) -> str:
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


def _kill_posix(pid: int) -> str:
    """Kill a process on Linux/macOS using os.kill."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"pid {pid} was not running"
    except PermissionError as exc:
        raise AssistantError(f"cannot kill {pid}: {exc}") from exc
    return f"sent SIGTERM to pid {pid}"


@risk(Risk.SAFE)
def open_task_manager(*, context: ActionContext) -> str:
    if is_windows():
        try:
            subprocess.Popen(["taskmgr"], close_fds=True)
        except OSError as exc:
            raise AssistantError(f"could not start task manager: {exc}") from exc
        return "opened task manager."
    # Linux
    managers = ["gnome-system-monitor", "htop", "ksysguard"]
    for mgr in managers:
        if shutil.which(mgr):
            try:
                subprocess.Popen([mgr], close_fds=True, start_new_session=True)
            except OSError as exc:
                raise AssistantError(f"could not start {mgr}: {exc}") from exc
            return f"opened {mgr}."
    return "هیچ مدیر پردازشی یافت نشد. نصب کنید: sudo apt install gnome-system-monitor"
