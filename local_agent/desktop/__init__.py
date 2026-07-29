"""Native Windows desktop app for the local assistant.

A ``pywebview`` shell that embeds the very same web UI served by
:mod:`local_agent.web`, plus the native bits a real desktop app needs:

* single 1200x800 resizable window (minimum 800x600)
* system tray icon with a right-click menu
* global hotkey (``Ctrl+Alt+A`` by default) to summon the window
* Windows toast notifications for approvals, completion, and errors
* minimise-to-tray instead of quitting on the X button
* single-instance enforcement (a second launch raises the first window)
* auto-start with Windows, toggleable from the settings modal
* update checks against GitHub releases

Run it with::

    python local_agent_setup.py desktop
    # or
    persian-local-desktop

Every native feature degrades gracefully: on Linux/macOS, or when the
optional dependencies are missing, the app still starts and simply
reports the feature as unavailable.
"""

from .app import (
    APP_NAME,
    APP_NAME_EN,
    APP_VERSION,
    DesktopApi,
    DesktopApp,
    DesktopConfig,
    is_pywebview_available,
    run,
)

__all__ = [
    "APP_NAME",
    "APP_NAME_EN",
    "APP_VERSION",
    "DesktopApi",
    "DesktopApp",
    "DesktopConfig",
    "is_pywebview_available",
    "run",
]
