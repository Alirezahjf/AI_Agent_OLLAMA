"""PyInstaller entry point.

Must use absolute imports: PyInstaller runs this as a top-level script,
so relative imports have no parent package.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()  # required for frozen Windows apps
    from local_agent.desktop.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
