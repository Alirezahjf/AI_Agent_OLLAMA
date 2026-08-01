"""Start-with-system registration.

Windows: writes a value under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.  This is the
per-user Run key, so it needs no administrator rights and never touches
another account's settings.

Linux: writes a ``.desktop`` file in ``~/.config/autostart/``.

Other platforms: every function is a safe no-op that reports
``supported() is False``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..core.logging_setup import get_logger


logger = get_logger("desktop.autostart")


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "PersianLocalAssistant"


def supported() -> bool:
    """True when auto-start can be configured on this platform."""
    return sys.platform == "win32" or sys.platform.startswith("linux")


def launch_command() -> str:
    """The command the system should run at logon."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    executable = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = executable.with_name("pythonw.exe")
        runner = pythonw if pythonw.is_file() else executable
        return f'"{runner}" -m local_agent.desktop'
    return f'"{executable}" -m local_agent.desktop'


def is_enabled() -> bool:
    """True when auto-start is currently configured."""
    if sys.platform == "win32":
        return _is_enabled_windows()
    if sys.platform.startswith("linux"):
        return _is_enabled_linux()
    return False


def _is_enabled_windows() -> bool:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.debug("autostart lookup failed: %s", exc)
        return False


def _is_enabled_linux() -> bool:
    desktop_file = _linux_autostart_path()
    return desktop_file.is_file()


def _linux_autostart_path() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
    config_dir = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return config_dir / "autostart" / "persian-local-assistant.desktop"


def enable(command: str | None = None) -> bool:
    """Register the app to start at logon.  Returns True on success."""
    if sys.platform == "win32":
        return _enable_windows(command)
    if sys.platform.startswith("linux"):
        return _enable_linux(command)
    logger.info("auto-start is not implemented on this platform")
    return False


def _enable_windows(command: str | None = None) -> bool:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key, VALUE_NAME, 0, winreg.REG_SZ, command or launch_command()
            )
        logger.info("auto-start enabled")
        return True
    except OSError as exc:
        logger.warning("could not enable auto-start: %s", exc)
        return False


def _enable_linux(command: str | None = None) -> bool:
    """Write a .desktop file to ~/.config/autostart/."""
    desktop_file = _linux_autostart_path()
    desktop_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = command or launch_command()
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Persian Local Assistant\n"
        f"Exec={cmd}\n"
        "Comment=دستیار محلی ویندوز\n"
        "Hidden=false\n"
        "NoDisplay=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    try:
        desktop_file.write_text(content, encoding="utf-8")
        logger.info("auto-start enabled (Linux .desktop)")
        return True
    except OSError as exc:
        logger.warning("could not enable auto-start: %s", exc)
        return False


def disable() -> bool:
    """Remove the auto-start entry.  Returns True when it is gone afterwards."""
    if sys.platform == "win32":
        return _disable_windows()
    if sys.platform.startswith("linux"):
        return _disable_linux()
    return False


def _disable_windows() -> bool:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        logger.info("auto-start disabled")
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("could not disable auto-start: %s", exc)
        return False


def _disable_linux() -> bool:
    desktop_file = _linux_autostart_path()
    if not desktop_file.is_file():
        return True
    try:
        desktop_file.unlink()
        logger.info("auto-start disabled (Linux .desktop removed)")
        return True
    except OSError as exc:
        logger.warning("could not disable auto-start: %s", exc)
        return False


def set_enabled(enabled: bool) -> bool:
    """Convenience wrapper used by the settings UI."""
    return enable() if enabled else disable()


def open_in_file_manager(path: Path | str) -> bool:
    """Reveal ``path`` in Explorer / Finder / the Linux file manager."""
    target = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", str(target)])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not open %s: %s", target, exc)
        return False
