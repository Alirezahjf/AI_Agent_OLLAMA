"""``python -m local_agent.desktop`` launches the native desktop app."""

from __future__ import annotations

if __package__ in (None, ""):
    # Running directly as a script: ``python local_agent/desktop/__main__.py``
    # Add the repo root so the absolute import works.
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from local_agent.desktop.app import run
else:
    from .app import run


if __name__ == "__main__":
    raise SystemExit(run())
