"""UTF-8 hardening for Windows consoles and pipes.

Windows Python defaults to the legacy ANSI/OEM codepage (CP1252, CP720, ...)
for ``sys.stdout``/``sys.stderr`` and for text-mode ``subprocess`` calls.
Persian / Arabic text that is valid UTF-8 then gets decoded with the wrong
codepage and shows up as mojibake (``Ø­Ø§Ù...``).

Every CLI / web / desktop entry point should call :func:`ensure_utf8_stdio`
before printing anything, so the standard streams always speak UTF-8 and any
child process we spawn inherits ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8``.
"""

from __future__ import annotations

import os
import sys


def ensure_utf8_stdio() -> None:
    """Force the standard streams and the process environment to UTF-8.

    * Sets ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8`` in the environment
      so that **subprocesses** launched from this process decode text as
      UTF-8 (important for ``subprocess.run(..., text=True)`` on Windows).
    * Reconfigures ``sys.stdout`` / ``sys.stderr`` to emit UTF-8 regardless
      of the console codepage.
    """
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a real stream
            pass
