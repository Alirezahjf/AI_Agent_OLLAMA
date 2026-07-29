"""Shared platform helpers."""

from .platform import (
    is_windows,
    resolve_windows_executable,
    start_windows_process,
    list_installed_apps_windows,
    iter_windows_windows,
    move_resize_window,
    minimize_window,
    maximize_window,
    windows_desktop_session,
)

__all__ = [
    "is_windows",
    "resolve_windows_executable",
    "start_windows_process",
    "list_installed_apps_windows",
    "iter_windows_windows",
    "move_resize_window",
    "minimize_window",
    "maximize_window",
    "windows_desktop_session",
]
