"""``python -m local_agent`` entry point."""

from .cli import run_cli


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
