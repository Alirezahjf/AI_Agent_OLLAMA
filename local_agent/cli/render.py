"""Rich terminal renderer used by the local assistant CLI.

The renderer is intentionally thin: it owns the colour palette, prints
panels, and renders multi-line messages.  It never reads from stdin; the
REPL handles that.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from typing import Any

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except Exception:  # noqa: BLE001 - rich is optional
    RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """A minimal, dependency-light terminal renderer.

    Falls back to plain print() when rich is missing or when the
    output is being piped to a file.
    """

    BANNER_COLOR = "cyan"
    USER_COLOR = "green"
    ASSISTANT_COLOR = "blue"
    THINKING_COLOR = "magenta"
    WARN_COLOR = "yellow"
    ERROR_COLOR = "red"
    INFO_COLOR = "white"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._console = self._build_console()
        self._live: Any | None = None

    # ----------------------------------------------------------------- I/O

    def _build_console(self):
        if not RICH_AVAILABLE:
            return None
        if not sys.stdout.isatty():
            return None
        try:
            width = shutil.get_terminal_size((100, 30)).columns
        except OSError:
            width = 100
        return Console(width=width, color_system="auto", force_terminal=True)

    # ----------------------------------------------------------- primitives

    def _print(self, renderable: Any) -> None:
        with self._lock:
            if self._console is not None and RICH_AVAILABLE:
                self._console.print(renderable)
            else:
                if hasattr(renderable, "__rich_console__"):
                    try:
                        from rich.console import Console as _C

                        buffer_console = _C(file=sys.stdout, color_system=None, force_terminal=False)
                        buffer_console.print(renderable)
                        return
                    except Exception:  # noqa: BLE001
                        pass
                print(str(renderable))

    def print(self, text: str) -> None:
        self._print(text)

    def info(self, text: str) -> None:
        self._print(self._styled(text, self.INFO_COLOR))

    def warn(self, text: str) -> None:
        self._print(self._styled(text, self.WARN_COLOR))

    def error(self, text: str) -> None:
        self._print(self._styled(text, self.ERROR_COLOR))

    def _styled(self, text: str, color: str) -> Any:
        if RICH_AVAILABLE:
            return Text(text, style=color)
        return f"[{color}] {text}" if color else text

    # ---------------------------------------------------------- components

    def banner(self, *, title: str, subtitle: str, extra: str = "") -> None:
        if RICH_AVAILABLE:
            body = Text()
            body.append(title, style=f"bold {self.BANNER_COLOR}")
            if subtitle:
                body.append(f"\n{subtitle}", style="dim")
            if extra:
                body.append(f"\n{extra}", style="dim")
            self._print(Panel(body, border_style=self.BANNER_COLOR, padding=(1, 2)))
        else:
            self._print("=" * 60)
            self._print(title)
            if subtitle:
                self._print(subtitle)
            if extra:
                self._print(extra)
            self._print("=" * 60)

    def section(self, title: str) -> None:
        self._print(self._styled(f"── {title} ──", self.BANNER_COLOR))

    def prompt(self, label: str) -> str:
        with self._lock:
            if RICH_AVAILABLE and self._console is not None:
                self._console.print()
                self._console.print(f"[bold {self.USER_COLOR}]{label} ▸[/] ", end="")
                sys.stdout.flush()
                return input().strip()
            print()
            return input(f"{label} > ").strip()

    def thinking(self, message: str) -> None:
        if RICH_AVAILABLE and self._console is not None:
            self._console.print(f"[{self.THINKING_COLOR}]⟳ {message}[/]")
        else:
            print(f"... {message}")

    def assistant(self, text: str) -> None:
        if RICH_AVAILABLE and self._console is not None:
            try:
                self._console.print(Panel(Text(text), border_style=self.ASSISTANT_COLOR, title="assistant"))
            except Exception:  # noqa: BLE001
                self._print(text)
        else:
            self._print(f"assistant:\n{text}")

    def action_result(self, name: str, preview: str) -> None:
        if RICH_AVAILABLE and self._console is not None:
            try:
                body = Text()
                body.append(name, style="bold")
                body.append(" → ")
                body.append(preview)
                self._console.print(Panel(body, border_style="green", title="action"))
            except Exception:  # noqa: BLE001
                self._print(f"action {name}: {preview}")
        else:
            self._print(f"action {name}: {preview}")

    def confirm_request(self, action, arguments) -> None:
        if RICH_AVAILABLE and self._console is not None:
            table = Table.grid(padding=(0, 1))
            table.add_column(style="bold")
            table.add_column()
            table.add_row("action", action.name)
            table.add_row("risk", str(action.risk_level.value))
            for key, value in arguments.items():
                rendered = str(value)
                if len(rendered) > 200:
                    rendered = rendered[:200] + "..."
                table.add_row(key, rendered)
            self._console.print(Panel(table, border_style="yellow", title="approval required"))
        else:
            self._print(f"approval required: {action.name}  args={arguments}")

    def show_action_details(self, action, arguments) -> None:
        if RICH_AVAILABLE and self._console is not None:
            try:
                import json

                rendered = json.dumps(arguments, indent=2, ensure_ascii=False)
                self._console.print(Syntax(rendered, "json", theme="monokai"))
            except Exception:  # noqa: BLE001
                self._print(str(arguments))
        else:
            self._print(str(arguments))
