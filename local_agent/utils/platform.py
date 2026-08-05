"""Cross-platform detection and capability layer.

Every helper here is a thin wrapper around the OS API that returns
Pythonic values and degrades gracefully on any host.  The real
implementation uses ctypes + winreg for the registry, PowerShell for
the AppX/UWP discovery, and the standard library everywhere else.

The module also exposes a :class:`Platform` enum and a
:func:`capabilities` function that the action registry consults
to decide which tools to register.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

from ..core.logging_setup import get_logger

logger = get_logger("utils.platform")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class Platform(str, Enum):
    """The operating system we are running on."""

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


def current_platform() -> Platform:
    """Detect the current OS, with an optional testing override."""
    forced = os.environ.get("LOCAL_AGENT_FORCE_PLATFORM", "").strip().lower()
    if forced:
        mapping = {"windows": Platform.WINDOWS, "linux": Platform.LINUX, "macos": Platform.MACOS}
        return mapping.get(forced, Platform.LINUX)
    if os.name == "nt":
        return Platform.WINDOWS
    if sys_platform() == "darwin":
        return Platform.MACOS
    return Platform.LINUX


def is_windows() -> bool:
    return current_platform() == Platform.WINDOWS


def is_linux() -> bool:
    return current_platform() == Platform.LINUX


def is_macos() -> bool:
    return current_platform() == Platform.MACOS


def is_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux."""
    try:
        version_text = Path("/proc/version").read_text(encoding="utf-8").lower()
        return "microsoft" in version_text
    except OSError:
        return False


def elevation_level() -> str:
    """Report the process privilege level: ``admin`` | ``root`` | ``user``.

    Windows answers through ``IsUserAnAdmin()`` (guarded with
    ``getattr`` so the sandbox/CI never crashes), POSIX through
    ``os.geteuid()``.  This tells the UI whether the assistant really
    runs with administrator/root rights when ``full_system_access`` is
    enabled — or whether the user still needs to restart it elevated.
    """
    if current_platform() == Platform.WINDOWS:
        try:
            import ctypes

            shell32 = getattr(ctypes, "windll", None)
            if shell32 is not None and hasattr(shell32, "shell32"):
                return "admin" if bool(shell32.shell32.IsUserAnAdmin()) else "user"
        except (OSError, AttributeError, ImportError):
            pass
        return "user"
    try:
        return "root" if os.geteuid() == 0 else "user"
    except (AttributeError, OSError):
        return "user"


def has_display() -> bool:
    """True when a graphical display is available.

    On Windows/macOS this is always True (the desktop session *is* the
    display).  On Linux we check ``DISPLAY`` or ``WAYLAND_DISPLAY``.
    """
    if current_platform() == Platform.LINUX:
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def is_headless_server() -> bool:
    """True when running on Linux with no display (e.g. a cloud server)."""
    return is_linux() and not has_display()


def is_container() -> bool:
    """Best-effort detection of container environments."""
    if Path("/.dockerenv").is_file():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
        if "docker" in cgroup or "kubepods" in cgroup:
            return True
    except OSError:
        pass
    return False


def sys_platform() -> str:
    """Return ``sys.platform`` (utility so we can monkeypatch in tests)."""
    import sys

    return sys.platform


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def capabilities() -> dict[str, bool]:
    """Return a dict of what the current environment can do.

    The action registry uses this to decide which tools to register.
    """
    plat = current_platform()
    headless = is_headless_server()
    display = has_display()

    # GUI: can we show a window?
    gui = display and (plat == Platform.WINDOWS or plat == Platform.MACOS or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))

    # Tray: pystray needs a display backend
    tray = gui and _pystray_available()

    # Global hotkey: only Windows via RegisterHotKey
    hotkey = plat == Platform.WINDOWS

    # Clipboard: on Linux needs xclip/xsel or Wayland equiv
    clipboard = _clipboard_available()

    # Notifications: tray balloon or native toast
    notifications = tray or (plat == Platform.WINDOWS)

    # Shell: always available
    shell = True

    return {
        "gui": gui,
        "tray": tray,
        "hotkey": hotkey,
        "clipboard": clipboard,
        "notifications": notifications,
        "shell": shell,
        "headless": headless,
        "container": is_container(),
        "wsl": is_wsl(),
    }


def log_platform_summary() -> None:
    """Log a one-line summary of the platform at startup."""
    caps = capabilities()
    plat = current_platform().value
    display = "yes" if has_display() else "no"
    mode = "server" if caps["headless"] else "desktop"
    gui = "on" if caps["gui"] else "off"
    tray = "on" if caps["tray"] else "off"
    hotkey = "on" if caps["hotkey"] else "off"
    logger.info(
        "platform=%s display=%s mode=%s gui=%s tray=%s hotkey=%s",
        plat, display, mode, gui, tray, hotkey,
    )


# ---------------------------------------------------------------------------
# Internal probes
# ---------------------------------------------------------------------------


def _pystray_available() -> bool:
    try:
        import pystray  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _clipboard_available() -> bool:
    """True when we can read/write the system clipboard."""
    if is_windows():
        return True
    if is_macos():
        return True
    # Linux: need xclip, xsel, or Wayland clipboard
    if shutil.which("xclip") or shutil.which("xsel"):
        return True
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return True
    try:
        import pyperclip  # noqa: F401
        return True
    except ImportError:
        return False


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
    """Ask PowerShell to resolve a UWP package by name."""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    f"Get-StartApps | Where-Object {{$_.Name -like '*{bare}*'}} "
                    "| Select-Object -First 1 -ExpandProperty AppID"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    appid = (completed.stdout or "").strip()
    if not appid:
        return None
    return f"shell:AppsFolder\\{appid}"


# ---------------------------------------------------------------------------
# Listing installed apps
# ---------------------------------------------------------------------------


def list_installed_apps_windows() -> list[dict[str, str]]:
    """Enumerate installed applications using the registry + StartApps.

    On non-Windows hosts the function returns an empty list.
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
    ``subprocess.Popen`` for normal executables.
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
        yield from titles
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
