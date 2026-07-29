"""Windows + POSIX platform helpers.

Every helper here is a thin wrapper around the OS API that returns
Pythonic values and degrades gracefully on non-Windows hosts (so unit
tests can run on Linux).  The real implementation uses ctypes +
winreg for the registry, PowerShell for the AppX/UWP discovery, and
the standard library everywhere else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from ..core.logging_setup import get_logger


logger = get_logger("utils.platform")


def is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Resolving executables
# ---------------------------------------------------------------------------


def resolve_windows_executable(name: str) -> str | None:
    """Return the absolute path of an executable or None.

    Search order:
      1. PATH
      2. %LOCALAPPDATA%\\Programs
      3. %ProgramFiles%\\<name>\\<name>.exe and friends
      4. %ProgramFiles(x86)%\\...
      5. UWP / AppX (powershell: Get-StartApps)
      6. Shell:AppsFolder (HKCU)
    """
    if not name:
        return None
    clean = name.strip()
    bare = clean.lower().removesuffix(".exe")

    # 1) PATH
    found = shutil.which(clean) or shutil.which(f"{clean}.exe")
    if found:
        return found

    if not is_windows():
        return found  # PATH-only on POSIX

    # 2) LOCALAPPDATA
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local:
        candidates.extend(_walk_for_executables(Path(local) / "Programs", bare))

    # 3-4) Program Files
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(env, "")
        if not root:
            continue
        candidates.extend(_walk_for_executables(Path(root), bare, max_depth=4))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    # 5) UWP / AppX
    uwp = _resolve_uwp_executable(bare)
    if uwp:
        return uwp

    return None


def _walk_for_executables(
    root: Path, bare: str, *, max_depth: int = 3
) -> Iterable[Path]:
    if not root.is_dir():
        return []
    matches: list[Path] = []
    target = f"{bare}.exe"
    try:
        for current, dirs, files in os.walk(root):
            depth = len(Path(current).relative_to(root).parts)
            if depth > max_depth:
                dirs[:] = []
                continue
            for filename in files:
                if filename.lower() == target:
                    matches.append(Path(current) / filename)
    except OSError:
        pass
    return matches


def _resolve_uwp_executable(bare: str) -> str | None:
    """Ask PowerShell to resolve a UWP package by name.

    The result is the AppUserModelID; we then look in
    ``HKCU\\Software\\Classes\\AppUserModelId`` for the InstallLocation
    and use that as the resolved path.  This is best-effort.
    """
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-StartApps | Where-Object {{$_.Name -like '*{bare}*'}} "
                "| Select-Object -First 1 -ExpandProperty AppID",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    appid = (completed.stdout or "").strip()
    if not appid:
        return None
    # Use explorer to launch the AppX; the shell will handle the activation.
    return f"shell:AppsFolder\\{appid}"


# ---------------------------------------------------------------------------
# Listing installed apps
# ---------------------------------------------------------------------------


def list_installed_apps_windows() -> list[dict[str, str]]:
    """Enumerate installed applications using the registry + StartApps.

    The result is a list of dicts with ``name`` and ``path`` keys.  On
    non-Windows hosts the function returns an empty list.
    """
    if not is_windows():
        return []
    apps: list[dict[str, str]] = []
    apps.extend(_registry_uninstall_apps())
    # Deduplicate by path
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for app in apps:
        path = app.get("path", "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        unique.append(app)
    return unique


def _registry_uninstall_apps() -> list[dict[str, str]]:
    try:
        import winreg  # type: ignore
    except ImportError:
        return []
    apps: list[dict[str, str]] = []
    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub_key in hives:
        try:
            with winreg.OpenKey(hive, sub_key) as base:
                index = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(base, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(base, sub_name) as entry:
                            try:
                                name, _ = winreg.QueryValueEx(entry, "DisplayName")
                            except OSError:
                                continue
                            try:
                                exe, _ = winreg.QueryValueEx(entry, "DisplayIcon")
                            except OSError:
                                exe = ""
                            exe = _clean_icon_string(exe)
                            apps.append({"name": str(name), "path": str(exe)})
                    except OSError:
                        continue
        except OSError:
            continue
    return apps


def _clean_icon_string(value: str) -> str:
    """``DisplayIcon`` often contains a comma index; trim it."""
    if not value:
        return ""
    candidate = value.strip().strip('"')
    if "," in candidate:
        candidate = candidate.split(",", 1)[0].strip().strip('"')
    return candidate


# ---------------------------------------------------------------------------
# Process launching
# ---------------------------------------------------------------------------


def start_windows_process(
    executable: str, arguments: str = "", working_dir: Path | None = None
) -> subprocess.Popen | None:
    """Start a Windows process and return the Popen.

    Uses the ``start`` builtin for ``shell:`` URIs (UWP apps) and
    ``subprocess.Popen`` for normal executables.  When the process is
    started via ``start`` the Popen handle is ``None`` because the
    shell detaches immediately.
    """
    if not executable:
        raise ValueError("executable is empty")

    if executable.startswith("shell:"):
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", executable],
                shell=False,
                close_fds=True,
            )
            return None
        except OSError:
            return None

    argv = [executable]
    if arguments:
        import shlex

        try:
            argv.extend(shlex.split(arguments, posix=False))
        except ValueError:
            argv.append(arguments)
    try:
        return subprocess.Popen(
            argv,
            cwd=str(working_dir) if working_dir else None,
            close_fds=True,
        )
    except OSError as exc:
        raise OSError(f"could not start {executable}: {exc}") from exc


# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------


def iter_windows_windows() -> Iterable[str]:
    """Yield the visible top-level window titles on Windows."""
    if not is_windows():
        return
    try:
        import ctypes

        EnumWindows = ctypes.windll.user32.EnumWindows
        GetWindowTextW = ctypes.windll.user32.GetWindowTextW
        IsWindowVisible = ctypes.windll.user32.IsWindowVisible

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        )

        titles: list[str] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            if not IsWindowVisible(hwnd):
                return True
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buff = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buff, length + 1)
            text = buff.value or ""
            if text.strip():
                titles.append(text)
            return True

        EnumWindows(EnumWindowsProc(callback), 0)
        for title in titles:
            yield title
    except (OSError, AttributeError) as exc:
        logger.debug("iter_windows_windows failed: %s", exc)


def move_resize_window(title: str, x: int, y: int, w: int, h: int) -> None:
    if not is_windows():
        raise OSError("move_resize_window is Windows-only")
    import ctypes

    SWP_NOZORDER = 0x0004
    SWP_SHOWWINDOW = 0x0040
    hwnd = _hwnd_for_title(title)
    if hwnd == 0:
        raise OSError(f"window not found: {title!r}")
    flags = SWP_NOZORDER | SWP_SHOWWINDOW
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, int(x), int(y), int(w), int(h), flags
    )


def minimize_window(title: str) -> None:
    if not is_windows():
        raise OSError("minimize_window is Windows-only")
    import ctypes

    hwnd = _hwnd_for_title(title)
    if hwnd == 0:
        raise OSError(f"window not found: {title!r}")
    SW_MINIMIZE = 6
    ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)


def maximize_window(title: str) -> None:
    if not is_windows():
        raise OSError("maximize_window is Windows-only")
    import ctypes

    hwnd = _hwnd_for_title(title)
    if hwnd == 0:
        raise OSError(f"window not found: {title!r}")
    SW_MAXIMIZE = 3
    ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)


def _hwnd_for_title(title: str) -> int:
    import ctypes

    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
    )

    needle = title.lower()
    found = [0]

    def callback(hwnd: int, _lparam: int) -> bool:
        if not IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        GetWindowTextW(hwnd, buff, length + 1)
        text = buff.value or ""
        if needle in text.lower():
            found[0] = hwnd
            return False
        return True

    EnumWindows(EnumWindowsProc(callback), 0)
    return int(found[0])


def windows_desktop_session() -> bool:
    """Return True if the process can reach a Windows desktop session."""
    if not is_windows():
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return bool(user32.GetDesktopWindow())
    except (OSError, AttributeError):
        return False
