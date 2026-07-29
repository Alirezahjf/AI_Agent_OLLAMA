"""``python -m local_agent.desktop`` launches the native desktop app."""

from __future__ import annotations

from .app import run


if __name__ == "__main__":
    raise SystemExit(run())
