"""Start-with-Windows registration.

Windows: writes a value under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.  This is the
per-user Run key, so it needs no administrator rights and never touches
another account's settings.

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
    return sys.platform == "win32"


def launch_command() -> str:
    """The command Windows should run at logon.

    Frozen builds point at the ``.exe`` directly; source checkouts go
    through ``pythonw.exe`` so no console window flashes at logon.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else executable
    return f'"{runner}" -m local_agent.desktop'


def is_enabled() -> bool:
    """True when the Run key currently holds our value."""
    if not supported():
        return False
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - registry quirks
        logger.debug("autostart lookup failed: %s", exc)
        return False


def enable(command: str | None = None) -> bool:
    """Register the app to start at logon.  Returns True on success."""
    if not supported():
        logger.info("auto-start is only implemented on Windows")
        return False
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


def disable() -> bool:
    """Remove the Run key value.  Returns True when it is gone afterwards."""
    if not supported():
        return False
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
