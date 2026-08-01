"""``python -m local_agent.web`` launches the browser UI."""

from __future__ import annotations

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from local_agent.web.app import run_web
else:
    from .app import run_web


if __name__ == "__main__":
    raise SystemExit(run_web())
