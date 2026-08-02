"""Centralized logging for the local assistant.

Logs go to:
  - <DATA_DIR>/logs/assistant.log  (rotated, kept forever)
  - console (only WARN+ unless --verbose)

A single call to ``setup_logging()`` configures everything; subsequent
calls are no-ops. Tests can pass ``level="DEBUG"`` to override.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_CONFIGURED = False
_LOGGER_NAME = "local_assistant"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the assistant's root logger."""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def shutdown_logging() -> None:
    """Flush, close and detach every handler of the assistant's root logger.

    Only the ``local_assistant`` logger is touched — pytest's capture
    handlers and third-party loggers are left alone.  The full-purge flow
    calls this before deleting ``data_dir`` so the running process releases
    the log files (Windows refuses to delete open files).
    """
    root = logging.getLogger(_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass


def setup_logging(
    data_dir: Path,
    *,
    level: str = "INFO",
    console_level: str | None = None,
    verbose: bool = False,
) -> None:
    """Configure root + console handlers exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "assistant.log"

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(
        getattr(logging, (console_level or level).upper(), logging.INFO)
    )
    if verbose:
        console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Quiet noisy third-party loggers unless the user asked for verbose.
    if not verbose:
        for noisy in ("telethon", "pywinauto", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    get_logger("init").info("logging initialised; file=%s", log_file)
    if os.name != "nt":
        get_logger("init").warning(
            "this agent is built for Windows; some actions (pywinauto, "
            "taskkill, etc.) may not behave correctly on %s",
            os.name,
        )
