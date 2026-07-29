"""Application launching: open, focus, install, and locate programs.

These tools are SAFE; they don't change the system in a way that
requires confirmation. Opening a program you ask for is exactly what
the human intended.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.platform import (
    list_installed_apps_windows,
    resolve_windows_executable,
    start_windows_process,
    windows_desktop_session,
)
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.app_control")


def register_app_control(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="open_application",
        description=(
            "Open a Windows application by its common name (e.g. 'chrome', 'telegram', "
            "'photoshop', 'explorer', 'notepad', 'calculator', 'task manager', 'cmd', "
            "'powershell', 'firefox', 'edge', 'vscode'). The agent resolves aliases "
            "and starts the program non-blocking; success is reported as soon as the "
            "process is launched. Optional arguments: arguments to pass to the app, "
            "and a working directory. Use this when the user says 'open X' or 'launch X'."
        ),
        parameters={
            "name": {"type": "string", "description": "Friendly name of the app to open."},
            "arguments": {"type": "string", "description": "Optional command-line arguments."},
            "working_dir": {"type": "string", "description": "Optional working directory."},
            "wait": {"type": "boolean", "description": "If true, wait until the window appears."},
            "timeout": {"type": "integer", "description": "Wait timeout in seconds (default 10)."},
        },
        required=("name",),
    )(open_application)

    registry.decorator(
        name="close_application",
        description=(
            "Close an application gracefully by its process name (e.g. 'chrome', "
            "'Telegram', 'Photoshop'). Uses taskkill /T /IM on Windows. If force is "
            "true, the process is killed. This is DESTRUCTIVE — will be confirmed."
        ),
        parameters={
            "name": {"type": "string", "description": "Process or friendly name to close."},
            "force": {"type": "boolean", "description": "Force-kill (SIGKILL equivalent)."},
        },
        required=("name",),
    )(close_application)

    registry.decorator(
        name="focus_window",
        description=(
            "Bring a window to the foreground by partial title (e.g. 'Telegram', "
            "'Photoshop', 'Untitled - Notepad'). Restores the window if minimised."
        ),
        parameters={
            "title": {"type": "string", "description": "Partial window title."},
        },
        required=("title",),
    )(focus_window)

    registry.decorator(
        name="list_applications",
        description=(
            "Return a list of common applications installed on this Windows machine. "
            "Use before opening an app to verify its real name."
        ),
        parameters={
            "filter": {"type": "string", "description": "Substring filter (case-insensitive)."},
        },
    )(list_applications)

    registry.decorator(
        name="locate_application",
        description=(
            "Return the absolute path of an installed executable by friendly name. "
            "Returns an empty string if not found; the agent should not invent a path."
        ),
        parameters={
            "name": {"type": "string", "description": "Friendly name (chrome, telegram, ...)."},
        },
        required=("name",),
    )(locate_application)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _friendly_to_real(name: str) -> str:
    """Map a friendly app name to a known executable.

    The map is intentionally small but covers the apps the user actually
    mentioned in the brief: chrome, telegram desktop, photoshop, firefox,
    edge, vscode, task manager, file explorer, etc.
    """
    cleaned = name.strip().lower()
    aliases = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "firefox": "firefox",
        "mozilla firefox": "firefox",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "telegram": "telegram",
        "telegram desktop": "telegram",
        "whatsapp": "whatsapp",
        "vscode": "code",
        "vs code": "code",
        "visual studio code": "code",
        "code": "code",
        "notepad": "notepad",
        "notepad++": "notepad++",
        "calculator": "calc",
        "calc": "calc",
        "explorer": "explorer",
        "file explorer": "explorer",
        "photoshop": "photoshop",
        "adobe photoshop": "photoshop",
        "ps": "photoshop",
        "task manager": "taskmgr",
        "taskmanager": "taskmgr",
        "cmd": "cmd",
        "command prompt": "cmd",
        "terminal": "wt",
        "windows terminal": "wt",
        "powershell": "powershell",
        "paint": "mspaint",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "outlook": "outlook",
        "spotify": "spotify",
        "discord": "discord",
        "slack": "slack",
        "vlc": "vlc",
        "obs": "obs64",
        "obs studio": "obs64",
        "sublime text": "sublime_text",
        "sublime": "sublime_text",
        "pycharm": "pycharm64",
        "intellij": "idea64",
        "android studio": "studio64",
        "docker desktop": "docker",
    }
    return aliases.get(cleaned, cleaned)


@risk(Risk.SAFE)
def open_application(
    *,
    name: str,
    arguments: str = "",
    working_dir: str = "",
    wait: bool = False,
    timeout: int = 10,
    context: ActionContext,
) -> str:
    real = _friendly_to_real(name)
    arguments = (arguments or "").strip()
    working_dir_path = Path(working_dir).expanduser() if working_dir else None

    # Built-in Windows binaries that should always work
    builtin = {
        "calc", "notepad", "mspaint", "taskmgr", "cmd", "powershell", "wt", "explorer",
    }
    if real in builtin:
        try:
            proc = start_windows_process(real, arguments, working_dir_path)
        except OSError as exc:
            raise AssistantError(f"could not start built-in {real!r}: {exc}") from exc
        return _format_started(real, proc, arguments)

    # Try the resolution chain: PATH, common install dirs, registry, then UWP.
    exe_path = resolve_windows_executable(real)
    if exe_path is None:
        raise AssistantError(
            f"could not find an executable for {name!r}. "
            "Try locate_application to see what's available."
        )

    proc = start_windows_process(str(exe_path), arguments, working_dir_path)
    response = _format_started(real, proc, arguments)

    if wait:
        deadline = time.time() + max(1, timeout)
        title_hint = real.replace(".exe", "").lower()
        from .window_control import _wait_for_window  # local import to avoid cycle

        window = _wait_for_window(title_hint, deadline)
        if window:
            response += f" | window appeared: {window}"
        else:
            response += " | window not detected within timeout (process is running)."
    return response


def _format_started(real: str, proc: subprocess.Popen | None, args: str) -> str:
    if proc is None:
        return f"started {real} (process handle unavailable)."
    pid = getattr(proc, "pid", None) or "?"
    suffix = f" with args {args!r}" if args else ""
    return f"started {real}{suffix} (pid={pid})."


@risk(Risk.DESTRUCTIVE)
def close_application(*, name: str, force: bool = False, context: ActionContext) -> str:
    real = _friendly_to_real(name)
    if os.name != "nt":
        # POSIX fallback for unit tests
        return _posix_kill(real, force)
    flag = "/F" if force else ""
    cmd = ["taskkill", "/T", "/IM", f"{real}.exe"]
    if flag:
        cmd.insert(1, flag)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"taskkill failed: {exc}") from exc
    if completed.returncode == 0:
        return f"closed {real} (exit 0): {completed.stdout.strip()}"
    # 'process not found' is also a success for the user — return the info.
    if "not found" in (completed.stdout + completed.stderr).lower():
        return f"no running process named {real!r}; nothing to do."
    raise AssistantError(
        f"taskkill exit {completed.returncode}: "
        f"{(completed.stdout + completed.stderr).strip()[:300]}"
    )


def _posix_kill(name: str, force: bool) -> str:
    if not shutil.which("pkill"):
        raise DependencyMissing(
            "pkill is required to close applications on POSIX",
            install_hint="apt-get install procps",
        )
    flag = "-9" if force else ""
    try:
        subprocess.run(
            ["pkill", flag, name], capture_output=True, text=True, check=False, timeout=10
        )
    except OSError as exc:
        raise AssistantError(f"pkill failed: {exc}") from exc
    return f"pkill {name} dispatched (force={force})."


@risk(Risk.SAFE)
def focus_window(*, title: str, context: ActionContext) -> str:
    from .window_control import _focus_by_title

    window = _focus_by_title(title)
    if window:
        return f"focused window: {window}"
    return f"no window matching {title!r} was found."


@risk(Risk.SAFE)
def list_applications(*, filter: str = "", context: ActionContext) -> str:
    needle = (filter or "").strip().lower()
    apps = list_installed_apps_windows()
    if needle:
        apps = [a for a in apps if needle in a["name"].lower() or needle in a["path"].lower()]
    if not apps:
        return f"no installed apps matched filter {needle!r}."
    lines = [f"found {len(apps)} applications:"]
    for app in apps[:200]:
        lines.append(f"  • {app['name']:30s} {app['path']}")
    if len(apps) > 200:
        lines.append(f"  ... ({len(apps) - 200} more)")
    return "\n".join(lines)


@risk(Risk.SAFE)
def locate_application(*, name: str, context: ActionContext) -> str:
    real = _friendly_to_real(name)
    path = resolve_windows_executable(real)
    if path is None:
        return f"no executable found for {name!r}."
    return str(path)
