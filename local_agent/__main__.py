"""``python -m local_agent`` entry point."""

from __future__ import annotations

if __package__ in (None, ""):
    # Running directly as a script: ``python local_agent/__main__.py``
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from local_agent.cli import run_cli
else:
    from .cli import run_cli


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
