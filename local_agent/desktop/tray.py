"""System tray icon for the desktop app.

Uses ``pystray`` + ``Pillow`` when available.  Both are optional: if
``pystray`` is missing (or the platform has no tray, e.g. a headless
Linux box) the manager reports ``available = False`` and the app simply
runs without a tray icon instead of crashing.

The icon itself is drawn at runtime with Pillow so there is no binary
asset to ship or keep in sync with the UI theme.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.logging_setup import get_logger


logger = get_logger("desktop.tray")


TOOLTIP = "دستیار محلی ویندوز"


# ---------------------------------------------------------------------------
# Icon artwork
# ---------------------------------------------------------------------------


def build_icon_image(size: int = 64):
    """Draw the tray/app icon: a violet gradient tile with a white spark.

    Returns a ``PIL.Image``.  Raises ``ImportError`` when Pillow is not
    installed so callers can decide how to degrade.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Rounded gradient background (#6d8bff -> #a06bff)
    start = (109, 139, 255)
    end = (160, 107, 255)
    for y in range(size):
        ratio = y / max(1, size - 1)
        colour = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=colour + (255,))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=int(size * 0.26), fill=255
    )
    image.putalpha(mask)

    # Four-point spark, matching the web UI's brand mark.
    c = size / 2
    r_out = size * 0.34
    r_in = size * 0.12
    points = []
    for index in range(8):
        radius = r_out if index % 2 == 0 else r_in
        angle = (index * 45 - 90) * 3.14159265 / 180
        points.append((c + radius * _cos(angle), c + radius * _sin(angle)))
    draw.polygon(points, fill=(255, 255, 255, 235))
    return image


def _sin(x: float) -> float:
    import math

    return math.sin(x)


def _cos(x: float) -> float:
    import math

    return math.cos(x)


def save_icon(path: Path, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> Path:
    """Render the icon to ``path``.  ``.ico`` gets every size embedded."""
    image = build_icon_image(max(sizes))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".ico":
        image.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    else:
        image.save(path)
    return path


# ---------------------------------------------------------------------------
# Tray manager
# ---------------------------------------------------------------------------


@dataclass
class TrayCallbacks:
    """Hooks the tray menu invokes.  Every hook is optional."""

    on_show: Callable[[], None] | None = None
    on_hide: Callable[[], None] | None = None
    on_toggle: Callable[[], None] | None = None
    on_open_workspace: Callable[[], None] | None = None
    on_settings: Callable[[], None] | None = None
    on_check_updates: Callable[[], None] | None = None
    on_doctor: Callable[[], None] | None = None
    on_about: Callable[[], None] | None = None
    on_quit: Callable[[], None] | None = None


def is_available() -> bool:
    """True when a tray icon can actually be created on this machine.

    Importing ``pystray`` does more than load a module: on Linux it
    resolves an X11/AppIndicator backend and raises when ``DISPLAY`` is
    unset.  Catching only ``ImportError`` would therefore crash headless
    runs, so every exception is treated as "no tray here".
    """
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001 - backend probing can raise anything
        return False
    return True


class TrayManager:
    """Owns the tray icon lifecycle on a background thread."""

    def __init__(self, callbacks: TrayCallbacks | None = None, *, tooltip: str = TOOLTIP) -> None:
        self.callbacks = callbacks or TrayCallbacks()
        self.tooltip = tooltip
        self._icon = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    # ------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        return is_available()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> str | None:
        return self._error

    # ------------------------------------------------------------- menu

    def build_menu(self):
        """Build the right-click menu.  Kept public so tests can inspect it."""
        import pystray

        cb = self.callbacks

        def wrap(handler: Callable[[], None] | None):
            def run(icon=None, item=None) -> None:  # pystray passes both
                if handler is None:
                    return
                try:
                    handler()
                except Exception:  # noqa: BLE001
                    logger.exception("tray action failed")

            return run

        return pystray.Menu(
            pystray.MenuItem("نمایش پنجره", wrap(cb.on_show), default=True),
            pystray.MenuItem("پنهان کردن", wrap(cb.on_hide)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("باز کردن پوشهٔ کاری", wrap(cb.on_open_workspace)),
            pystray.MenuItem("تنظیمات", wrap(cb.on_settings)),
            pystray.MenuItem("بررسی سلامت", wrap(cb.on_doctor)),
            pystray.MenuItem("بررسی به‌روزرسانی", wrap(cb.on_check_updates)),
            pystray.MenuItem("درباره", wrap(cb.on_about)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("خروج", wrap(cb.on_quit)),
        )

    # -------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Start the tray icon.  Returns False when unavailable."""
        if not self.available:
            self._error = "no usable tray backend; running without a tray icon"
            logger.info(self._error)
            return False
        if self.running:
            return True
        try:
            import pystray

            self._icon = pystray.Icon(
                "local-agent",
                icon=build_icon_image(64),
                title=self.tooltip,
                menu=self.build_menu(),
            )
        except Exception as exc:  # noqa: BLE001
            self._error = f"could not create the tray icon: {exc}"
            logger.warning(self._error)
            return False

        self._thread = threading.Thread(target=self._run, name="desktop-tray", daemon=True)
        self._thread.start()
        logger.info("tray icon started")
        return True

    def _run(self) -> None:
        try:
            self._icon.run()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            logger.warning("tray icon stopped: %s", exc)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ---------------------------------------------------- notifications

    def notify(self, title: str, message: str) -> bool:
        """Show a balloon notification through the tray icon."""
        if self._icon is None:
            return False
        try:
            self._icon.notify(message, title)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("tray notification failed: %s", exc)
            return False

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._icon is not None:
            try:
                self._icon.title = text
            except Exception:  # noqa: BLE001
                pass
