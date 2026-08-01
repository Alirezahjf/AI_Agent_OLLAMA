"""Path helpers for bundled (PyInstaller) and source layouts.

When the app is frozen via PyInstaller, data files are unpacked to
``sys._MEIPASS`` at runtime.  In source mode they live relative to
the package directory.  This module provides a single entry point
that both modes can use.
"""

from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Base directory for bundled read-only assets.

    In a PyInstaller one-file build this returns ``sys._MEIPASS``.
    In a source checkout it returns the ``local_agent/`` package root.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    # Source layout: this file is local_agent/utils/paths.py
    return Path(__file__).resolve().parents[1]


def web_templates_dir() -> Path:
    """Return the ``local_agent/web/templates`` directory."""
    return resource_root() / "web" / "templates"


def web_static_dir() -> Path:
    """Return the ``local_agent/web/static`` directory."""
    return resource_root() / "web" / "static"
