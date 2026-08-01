"""System actions: shell command execution and a few OS queries.

We deliberately keep this surface small. Real shell access is the
riskiest capability the assistant has, so it is gated behind
``Risk.DESTRUCTIVE`` and the user can disable it via the policy flag
``safety.restrict_shell_to_workdir``.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from ..utils.encoding import TEXT_IO, decode_output
from ..utils.platform import is_linux, is_windows
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.system")


def register_system(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="run_shell",
        description=(
            "Run a shell command and return its output. On Windows, commands are "
            "executed via cmd.exe /c; on Linux/macOS via bash -lc. DESTRUCTIVE — "
            "always asks for confirmation unless the policy says otherwise."
        ),
        parameters={
            "command": {"type": "string"},
            "working_dir": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        required=("command",),
    )(run_shell)

    registry.decorator(
        name="system_info",
        description=(
            "Return a short summary of the host: OS, hostname, current user, "
            "Python version, working directory. Always safe."
        ),
        parameters={},
    )(system_info)

    registry.decorator(
        name="open_path",
        description=(
            "Open a file or directory with the system's default handler "
            "(Explorer on Windows, xdg-open on Linux). Safe."
        ),
        parameters={"path": {"type": "string"}},
        required=("path",),
    )(open_path)

    registry.decorator(
        name="shutdown_computer",
        description=(
            "Shut down the local computer. SYSTEM-level — always confirmed. "
            "delay_seconds defaults to 60 so you can cancel if needed."
        ),
        parameters={
            "delay_seconds": {"type": "integer"},
            "restart": {"type": "boolean"},
        },
    )(shutdown_computer)

    registry.decorator(
        name="cancel_shutdown",
        description="Cancel a previously scheduled shutdown.",
        parameters={},
    )(cancel_shutdown)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.DESTRUCTIVE)
def run_shell(
    *,
    command: str,
    working_dir: str = "",
    timeout: int = 30,
    context: ActionContext,
) -> str:
    if not isinstance(command, str) or not command.strip():
        raise AssistantError("command must be a non-empty string")
    timeout = max(1, min(int(timeout or 30), context.runtime.settings.safety.shell_timeout_seconds))
    cwd: Path | None = None
    if working_dir:
        candidate = Path(working_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (context.work_dir / candidate).resolve()
        if not candidate.is_dir():
            raise AssistantError(f"working dir does not exist: {candidate}")
        cwd = candidate
    elif context.runtime.settings.safety.restrict_shell_to_workdir:
        cwd = context.work_dir

    if os.name == "nt":
        argv = ["cmd.exe", "/d", "/s", "/c", command]
    else:
        argv = ["bash", "-lc", command]

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            **TEXT_IO,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return f"command timed out after {timeout}s"
    except FileNotFoundError as exc:
        raise AssistantError(f"shell not found: {exc}") from exc
    except OSError as exc:
        raise AssistantError(f"shell error: {exc}") from exc

    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    output = stdout + stderr
    snippet = output[: context.runtime.settings.llm.max_context_chars]
    return (
        f"$ {command}\n\n"
        f"{snippet}"
        f"\n\n[exit code: {completed.returncode}]"
    )


@risk(Risk.SAFE)
def system_info(*, context: ActionContext) -> str:
    info = {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "hostname": platform.node(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "cwd": str(context.work_dir),
        "data_dir": str(context.runtime.settings.data_dir),
    }
    return "\n".join(f"  {key}: {value}" for key, value in info.items())


@risk(Risk.SAFE)
def open_path(*, path: str, context: ActionContext) -> str:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = (context.work_dir / target).resolve()
    if not target.exists():
        raise AssistantError(f"path does not exist: {target}")
    try:
        if is_windows():
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(target)], close_fds=True)
    except OSError as exc:
        raise AssistantError(f"could not open {target}: {exc}") from exc
    return f"opened {target}"


@risk(Risk.SYSTEM)
def shutdown_computer(
    *, delay_seconds: int = 60, restart: bool = False, context: ActionContext
) -> str:
    delay = max(0, min(int(delay_seconds or 60), 3600))

    if is_windows():
        flag = "/r" if restart else "/s"
        try:
            subprocess.run(
                ["shutdown", flag, "/t", str(delay), "/c", "scheduled by local assistant"],
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"shutdown failed: {exc}") from exc
    else:
        # Linux: use systemctl or shutdown command
        cmd = None
        if shutil.which("systemctl"):
            action = "reboot" if restart else "poweroff"
            cmd = ["systemctl", action]
        elif shutil.which("shutdown"):
            flag = "-r" if restart else "-h"
            cmd = ["shutdown", flag, f"+{max(1, delay // 60)}"]
        else:
            raise AssistantError(
                "دستور خاموش کردن روی این سیستم در دسترس نیست. "
                "systemctl یا shutdown را نصب کنید."
            )
        try:
            subprocess.run(cmd, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"shutdown failed: {exc}") from exc

    verb = "restart" if restart else "shutdown"
    return f"{verb} scheduled in {delay} seconds. Use cancel_shutdown to abort."


@risk(Risk.SYSTEM)
def cancel_shutdown(*, context: ActionContext) -> str:
    if is_windows():
        try:
            subprocess.run(["shutdown", "/a"], check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"cancel failed: {exc}") from exc
    else:
        # Linux
        if shutil.which("shutdown"):
            try:
                subprocess.run(["shutdown", "-c"], check=False, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AssistantError(f"cancel failed: {exc}") from exc
        elif shutil.which("systemctl"):
            try:
                subprocess.run(["systemctl", "cancel"], check=False, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AssistantError(f"cancel failed: {exc}") from exc
        else:
            return "دستور لغو خاموش کردن در دسترس نیست."
    return "shutdown cancelled (if one was pending)."
