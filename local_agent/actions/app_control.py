"""Application launching: open, focus, install, and locate programs.

These tools are SAFE; they don't change the system in a way that
requires confirmation. Opening a program you ask for is exactly what
the human intended.

On Linux, the agent resolves aliases and uses ``xdg-open`` or the
executable on PATH.  On Windows, the full resolution chain (PATH,
registry, UWP) is used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.platform import (
    Platform,
    capabilities,
    current_platform,
    is_linux,
    is_windows,
    list_installed_apps_windows,
    resolve_windows_executable,
    start_windows_process,
    windows_desktop_session,
)
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.app_control")


# ---------------------------------------------------------------------------
# Linux alias table
# ---------------------------------------------------------------------------

_LINUX_ALIASES: dict[str, str] = {
    "chrome": "google-chrome",
    "google chrome": "google-chrome",
    "chromium": "chromium-browser",
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "edge": "microsoft-edge",
    "microsoft edge": "microsoft-edge",
    "telegram": "telegram-desktop",
    "telegram desktop": "telegram-desktop",
    "whatsapp": "whatsapp-nativefier",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "code": "code",
    "notepad": "gedit",
    "notepad++": "notepadpp",
    "calculator": "gnome-calculator",
    "calc": "gnome-calculator",
    "explorer": "nautilus",
    "file explorer": "nautilus",
    "files": "nautilus",
    "task manager": "gnome-system-monitor",
    "taskmanager": "gnome-system-monitor",
    "terminal": "gnome-terminal",
    "cmd": "gnome-terminal",
    "command prompt": "gnome-terminal",
    "powershell": "pwsh",
    "paint": "gimp",
    "word": "libreoffice --writer",
    "excel": "libreoffice --calc",
    "powerpoint": "libreoffice --impress",
    "spotify": "spotify",
    "discord": "discord",
    "slack": "slack",
    "vlc": "vlc",
    "sublime text": "subl",
    "sublime": "subl",
    "pycharm": "pycharm-community",
    "intellij": "idea",
    "docker desktop": "docker",
}


def register_app_control(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="open_application",
        description=(
            "Open an application by its common name (e.g. 'chrome', 'telegram', "
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
            "'Telegram', 'Photoshop'). Uses taskkill /T /IM on Windows, pkill on "
            "Linux. If force is true, the process is killed. This is DESTRUCTIVE — "
            "will be confirmed."
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
            "Return a list of common applications installed on this machine. "
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
    """Map a friendly app name to a known executable."""
    cleaned = name.strip().lower()

    # Windows aliases
    win_aliases = {
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

    linux_aliases = _LINUX_ALIASES

    if is_windows():
        return win_aliases.get(cleaned, cleaned)
    return linux_aliases.get(cleaned, cleaned)


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

    if is_windows():
        return _open_windows(real, arguments, working_dir_path, name, wait, timeout)
    return _open_linux(real, arguments, working_dir_path, name, wait, timeout)


def _open_windows(
    real: str, arguments: str, working_dir_path: Path | None,
    name: str, wait: bool, timeout: int,
) -> str:
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
        from .window_control import _wait_for_window

        window = _wait_for_window(title_hint, deadline)
        if window:
            response += f" | window appeared: {window}"
        else:
            response += " | window not detected within timeout (process is running)."
    return response


def _open_linux(
    real: str, arguments: str, working_dir_path: Path | None,
    name: str, wait: bool, timeout: int,
) -> str:
    # Try to find the executable on PATH
    exe = shutil.which(real)
    if exe is None:
        # Try xdg-open as a last resort for URLs / desktop files
        raise AssistantError(
            f"برنامه‌ای با نام {name!r} یافت نشد. "
            "مطمئن شوید نصب شده و در PATH قرار دارد. "
            f"نام بسته روی لینوکس احتمالاً «{real}» است."
        )
    try:
        cmd = [exe]
        if arguments:
            import shlex
            cmd.extend(shlex.split(arguments))
        proc = subprocess.Popen(
            cmd,
            cwd=str(working_dir_path) if working_dir_path else None,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise AssistantError(f"could not start {real!r}: {exc}") from exc
    return _format_started(real, proc, arguments)


def _format_started(real: str, proc: subprocess.Popen | None, args: str) -> str:
    if proc is None:
        return f"started {real} (process handle unavailable)."
    pid = getattr(proc, "pid", None) or "?"
    suffix = f" with args {args!r}" if args else ""
    return f"started {real}{suffix} (pid={pid})."


@risk(Risk.DESTRUCTIVE)
def close_application(*, name: str, force: bool = False, context: ActionContext) -> str:
    real = _friendly_to_real(name)
    if is_windows():
        return _close_windows(real, force)
    return _close_linux(real, name, force)


def _close_windows(real: str, force: bool) -> str:
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
    if "not found" in (completed.stdout + completed.stderr).lower():
        return f"no running process named {real!r}; nothing to do."
    raise AssistantError(
        f"taskkill exit {completed.returncode}: "
        f"{(completed.stdout + completed.stderr).strip()[:300]}"
    )


def _close_linux(real: str, name: str, force: bool) -> str:
    """Close an application on Linux using psutil or pkill."""
    try:
        import psutil
        return _close_linux_psutil(real, name, force, psutil)
    except ImportError:
        pass

    # Fallback to pkill
    if not shutil.which("pkill"):
        raise DependencyMissing(
            "برای بستن برنامه روی لینوکس، ابزار pkill یا بستهٔ psutil لازم است. "
            "نصب کنید: sudo apt install procps  یا  pip install psutil",
            install_hint="apt-get install procps  یا  pip install psutil",
        )
    flag = "-9" if force else ""
    try:
        cmd = ["pkill"]
        if flag:
            cmd.append(flag)
        cmd.append(real)
        subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    except OSError as exc:
        raise AssistantError(f"pkill failed: {exc}") from exc
    return f"pkill {real} dispatched (force={force})."


def _close_linux_psutil(real: str, name: str, force: bool, psutil: Any) -> str:
    """Close an application using psutil for process matching."""
    import signal

    found = False
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            proc_name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if real.lower() in proc_name or real.lower() in cmdline:
                found = True
                sig = signal.SIGKILL if force else signal.SIGTERM
                proc.send_signal(sig)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if not found:
        return f"no running process named {name!r}; nothing to do."
    verb = "killed" if force else "terminated"
    return f"{verb} processes matching {real!r}."


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
    if is_windows():
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

    # Linux: list executables on PATH
    return _list_linux_applications(needle)


def _list_linux_applications(needle: str) -> str:
    """List common desktop applications on Linux."""
    common_apps = [
        "firefox", "google-chrome", "chromium-browser", "code", "gedit",
        "nautilus", "gnome-terminal", "gnome-calculator", "vlc", "spotify",
        "discord", "slack", "telegram-desktop", "gimp", "libreoffice",
        "subl", "pycharm-community", "idea", "steam", "obs-studio",
    ]
    found = []
    for app in common_apps:
        if shutil.which(app):
            if not needle or needle in app.lower():
                found.append(app)
    if not found:
        return "هیچ برنامهٔ شناخته‌شده‌ای یافت نشد." if needle else f"no apps matched filter {needle!r}."
    lines = [f"found {len(found)} applications on PATH:"]
    for app in found:
        path = shutil.which(app) or app
        lines.append(f"  • {app:30s} {path}")
    return "\n".join(lines)


@risk(Risk.SAFE)
def locate_application(*, name: str, context: ActionContext) -> str:
    real = _friendly_to_real(name)
    if is_windows():
        path = resolve_windows_executable(real)
        if path is None:
            return f"no executable found for {name!r}."
        return str(path)
    # Linux: check PATH
    found = shutil.which(real)
    if found:
        return found
    return f"no executable found for {name!r} on PATH."
