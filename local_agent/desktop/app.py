"""The Windows desktop application.

A thin native shell around the web UI:

    ┌─ pywebview window (Edge WebView2) ─────────────┐
    │  http://127.0.0.1:7824  ← the Task-1 web UI    │
    └────────────────────────────────────────────────┘
                     │  JS bridge (window.pywebview.api)
    ┌────────────────▼───────────────────────────────┐
    │  DesktopApp: tray · hotkey · notifications ·   │
    │  single instance · auto-start · updates        │
    └────────────────┬───────────────────────────────┘
                     │  in-process
    ┌────────────────▼───────────────────────────────┐
    │  WebServer (FastAPI) → BridgeClient → Bridge   │
    └────────────────────────────────────────────────┘

Nothing about the front-end is duplicated: the desktop app serves the
very same HTML/CSS/JS the browser gets.  Everything native is optional —
if ``pystray`` is missing you lose the tray icon, not the app.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import AssistantSettings, load_settings
from ..core.logging_setup import get_logger, setup_logging
from ..utils.platform import is_headless_server, log_platform_summary
from . import autostart
from .hotkey import DEFAULT_HOTKEY, HotkeyError, HotkeyManager
from .single_instance import AlreadyRunning, SingleInstance
from .tray import TrayCallbacks, TrayManager
from .updater import Updater


logger = get_logger("desktop")


APP_NAME = "دستیار محلی ویندوز"
APP_NAME_EN = "Persian Local Assistant"
APP_VERSION = "2.0.0"

DEFAULT_WIDTH = 1200
DEFAULT_HEIGHT = 800
MIN_WIDTH = 800
MIN_HEIGHT = 600


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DesktopConfig:
    """Runtime knobs for the desktop shell (env-overridable)."""

    host: str = "127.0.0.1"
    port: int = 7824
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    min_width: int = MIN_WIDTH
    min_height: int = MIN_HEIGHT
    hotkey: str = DEFAULT_HOTKEY
    minimize_to_tray: bool = True
    start_hidden: bool = False
    check_updates: bool = True
    debug: bool = False

    @classmethod
    def from_env(cls) -> "DesktopConfig":
        def flag(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            host=os.environ.get("LOCAL_AGENT_WEB_HOST", "127.0.0.1"),
            port=int(os.environ.get("LOCAL_AGENT_WEB_PORT", "7824") or 7824),
            width=int(os.environ.get("LOCAL_AGENT_WINDOW_WIDTH", DEFAULT_WIDTH)),
            height=int(os.environ.get("LOCAL_AGENT_WINDOW_HEIGHT", DEFAULT_HEIGHT)),
            hotkey=os.environ.get("LOCAL_AGENT_HOTKEY", DEFAULT_HOTKEY),
            minimize_to_tray=flag("LOCAL_AGENT_MINIMIZE_TO_TRAY", True),
            start_hidden=flag("LOCAL_AGENT_START_HIDDEN", False),
            check_updates=flag("LOCAL_AGENT_CHECK_UPDATES", True),
            debug=flag("LOCAL_AGENT_DESKTOP_DEBUG", False),
        )


def find_free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` when free, otherwise an OS-assigned port."""
    with socket.socket() as probe:
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def is_pywebview_available() -> bool:
    """True when ``pywebview`` can be imported."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# JS API exposed to the front-end as window.pywebview.api
# ---------------------------------------------------------------------------


class DesktopApi:
    """Methods callable from JavaScript inside the window.

    Every method is defensive: the same front-end runs in a plain
    browser where ``window.pywebview`` does not exist at all, so these
    are strictly additive niceties.
    """

    def __init__(self, app: "DesktopApp") -> None:
        self._app = app

    # ---- window ------------------------------------------------------

    def show(self) -> bool:
        return self._app.show_window()

    def hide(self) -> bool:
        return self._app.hide_window()

    def minimize(self) -> bool:
        return self._app.minimize_window()

    def quit(self) -> bool:
        self._app.quit()
        return True

    # ---- integrations -------------------------------------------------

    def notify(self, title: str, message: str = "") -> bool:
        return self._app.notify(str(title), str(message))

    def set_progress(self, value: float) -> bool:
        return self._app.set_taskbar_progress(float(value))

    def open_workspace(self) -> bool:
        return self._app.open_workspace()

    def pick_file(self, multiple: bool = False) -> list[str]:
        return self._app.pick_file(multiple=bool(multiple))

    def pick_folder(self) -> str:
        return self._app.pick_folder()

    # ---- settings ------------------------------------------------------

    def get_autostart(self) -> bool:
        return autostart.is_enabled()

    def set_autostart(self, enabled: bool) -> bool:
        return autostart.set_enabled(bool(enabled))

    def get_info(self) -> dict[str, Any]:
        return self._app.info()

    def check_updates(self) -> dict[str, Any]:
        return self._app.check_updates(force=True)


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


@dataclass
class DesktopApp:
    """Owns the window, the tray, the hotkey, and the embedded web server."""

    settings: AssistantSettings
    config: DesktopConfig = field(default_factory=DesktopConfig.from_env)

    window: Any = None
    server: Any = None
    client: Any = None
    tray: TrayManager | None = None
    hotkey: HotkeyManager | None = None
    lock: SingleInstance | None = None
    updater: Updater | None = None

    _visible: bool = True
    _quitting: bool = False

    # ----------------------------------------------------------- backend

    def start_backend(self) -> str:
        """Boot the Bridge + web server in-process and return the URL."""
        from ..bridge import BridgeClient
        from ..web.app import WebServer

        self.config.port = find_free_port(self.config.host, self.config.port)
        self.client = BridgeClient.start_in_process(self.settings)
        self.server = WebServer(
            self.settings, self.client, host=self.config.host, port=self.config.port
        )
        self.server.start_in_thread()
        if not self.server.wait_until_ready(timeout=20):
            logger.warning("web server did not report ready in time; continuing anyway")
        logger.info("desktop backend ready at %s", self.server.url)
        return self.server.url

    # ------------------------------------------------------------ window

    def create_window(self, url: str) -> Any:
        """Create the pywebview window (without starting the GUI loop)."""
        import webview

        title = f"{APP_NAME} — {self.settings.work_dir}"
        self.window = webview.create_window(
            title,
            url,
            width=self.config.width,
            height=self.config.height,
            min_size=(self.config.min_width, self.config.min_height),
            resizable=True,
            background_color="#070B18",
            text_select=True,
            confirm_close=False,
            hidden=self.config.start_hidden,
            js_api=DesktopApi(self),
        )
        self._visible = not self.config.start_hidden
        # Route the X button to the tray instead of exiting.
        try:
            self.window.events.closing += self._on_closing
        except Exception:  # noqa: BLE001 - older pywebview builds
            logger.debug("this pywebview build has no closing event")
        try:
            self.window.events.loaded += self._on_loaded
        except Exception:  # noqa: BLE001
            pass
        return self.window

    def _on_closing(self) -> bool:
        """Return False to veto the close and hide to tray instead."""
        if self._quitting or not self.config.minimize_to_tray:
            return True
        if self.tray is not None and self.tray.running:
            self.hide_window()
            self.notify(
                APP_NAME,
                "برنامه در نوار وظیفه اجرا می‌ماند. برای بازگشت روی آیکون کلیک کنید.",
            )
            return False
        return True

    def _on_loaded(self) -> None:
        logger.info("window loaded")
        self.update_title()

    def show_window(self) -> bool:
        if self.window is None:
            return False
        try:
            self.window.show()
            try:
                self.window.restore()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.window.on_top = True
                self.window.on_top = False
            except Exception:  # noqa: BLE001
                pass
            self._visible = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not show the window: %s", exc)
            return False

    def hide_window(self) -> bool:
        if self.window is None:
            return False
        try:
            self.window.hide()
            self._visible = False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not hide the window: %s", exc)
            return False

    def minimize_window(self) -> bool:
        if self.window is None:
            return False
        try:
            self.window.minimize()
            return True
        except Exception:  # noqa: BLE001
            return self.hide_window()

    def toggle_window(self) -> bool:
        """Hotkey / tray-click behaviour: bring forward, or tuck away."""
        return self.hide_window() if self._visible else self.show_window()

    def update_title(self, suffix: str = "") -> None:
        if self.window is None:
            return
        title = f"{APP_NAME} — {self.settings.work_dir}"
        if suffix:
            title = f"{title}  ·  {suffix}"
        try:
            self.window.set_title(title)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------- notifications

    def notify(self, title: str, message: str = "") -> bool:
        """Native toast, falling back to the tray balloon then the log."""
        if sys.platform == "win32":
            try:
                from win10toast import ToastNotifier  # type: ignore[import-not-found]

                ToastNotifier().show_toast(title, message, threaded=True, duration=6)
                return True
            except Exception:  # noqa: BLE001
                pass
        if self.tray is not None and self.tray.notify(title, message):
            return True
        logger.info("notification: %s — %s", title, message)
        return False

    def set_taskbar_progress(self, value: float) -> bool:
        """Show progress on the taskbar button.

        ``value`` is 0..1 for a determinate bar, ``-1`` for indeterminate
        ("the agent is working"), and ``0`` to clear it.  Implemented via
        the Windows ``ITaskbarList3`` COM interface when ``comtypes`` is
        present; a no-op otherwise.
        """
        if sys.platform != "win32":
            return False
        try:  # pragma: no cover - Windows/COM only
            import comtypes.client as cc  # type: ignore[import-not-found]

            taskbar = cc.CreateObject(
                "{56FDF344-FD6D-11d0-958A-006097C9A090}", interface=None
            )
            hwnd = self._hwnd()
            if hwnd is None:
                return False
            if value < 0:
                taskbar.SetProgressState(hwnd, 0x1)  # TBPF_INDETERMINATE
            elif value == 0:
                taskbar.SetProgressState(hwnd, 0x0)  # TBPF_NOPROGRESS
            else:
                taskbar.SetProgressState(hwnd, 0x2)  # TBPF_NORMAL
                taskbar.SetProgressValue(hwnd, int(value * 100), 100)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("taskbar progress unavailable: %s", exc)
            return False

    def _hwnd(self) -> int | None:  # pragma: no cover - Windows only
        try:
            import ctypes

            return int(
                ctypes.windll.user32.FindWindowW(  # type: ignore[attr-defined]
                    None, self.window.title
                )
            )
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ dialogs

    def pick_file(self, *, multiple: bool = False) -> list[str]:
        if self.window is None:
            return []
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=multiple
            )
            return list(result or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("file dialog failed: %s", exc)
            return []

    def pick_folder(self) -> str:
        if self.window is None:
            return ""
        try:
            import webview

            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            return str(result[0]) if result else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("folder dialog failed: %s", exc)
            return ""

    def open_workspace(self) -> bool:
        return autostart.open_in_file_manager(self.settings.work_dir)

    # ------------------------------------------------------------ updates

    def check_updates(self, *, force: bool = False) -> dict[str, Any]:
        if self.updater is None:
            self.updater = Updater(APP_VERSION, data_dir=self.settings.data_dir)
        result = self.updater.check(force=force)
        if result.available and result.release is not None:
            self.notify(
                "به‌روزرسانی موجود است",
                f"نسخهٔ {result.release.version} منتشر شده است.",
            )
        elif force:
            self.notify(APP_NAME, "شما آخرین نسخه را دارید.")
        return result.to_dict()

    def _check_updates_async(self) -> None:
        if not self.config.check_updates:
            return

        def worker() -> None:
            time.sleep(6)  # let the UI settle first
            try:
                self.check_updates(force=False)
            except Exception:  # noqa: BLE001
                logger.debug("background update check failed", exc_info=True)

        threading.Thread(target=worker, name="desktop-updates", daemon=True).start()

    # --------------------------------------------------------------- info

    def info(self) -> dict[str, Any]:
        return {
            "app": APP_NAME_EN,
            "version": APP_VERSION,
            "platform": sys.platform,
            "url": self.server.url if self.server else "",
            "work_dir": str(self.settings.work_dir),
            "data_dir": str(self.settings.data_dir),
            "hotkey": self.config.hotkey,
            "hotkey_active": bool(self.hotkey and self.hotkey.active),
            "tray_active": bool(self.tray and self.tray.running),
            "autostart": autostart.is_enabled(),
        }

    def show_about(self) -> None:
        self.notify(
            f"{APP_NAME} نسخهٔ {APP_VERSION}",
            f"پوشهٔ کاری: {self.settings.work_dir}\nکلید میان‌بر: {self.config.hotkey}",
        )

    def open_settings(self) -> None:
        """Show the window and pop the settings modal in the UI."""
        self.show_window()
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                "window.Alpine && Alpine.$data(document.getElementById('app')).openSettings()"
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------ native pieces

    def start_tray(self) -> bool:
        self.tray = TrayManager(
            TrayCallbacks(
                on_show=self.show_window,
                on_hide=self.hide_window,
                on_toggle=self.toggle_window,
                on_open_workspace=self.open_workspace,
                on_settings=self.open_settings,
                on_check_updates=lambda: self.check_updates(force=True),
                on_about=self.show_about,
                on_quit=self.quit,
            )
        )
        return self.tray.start()

    def start_hotkey(self) -> bool:
        try:
            self.hotkey = HotkeyManager(self.config.hotkey, self.toggle_window)
        except HotkeyError as exc:
            logger.warning("invalid hotkey %r: %s", self.config.hotkey, exc)
            self.hotkey = HotkeyManager(DEFAULT_HOTKEY, self.toggle_window)
        return self.hotkey.start()

    # --------------------------------------------------------- lifecycle

    def quit(self) -> None:
        """Tear everything down and close the GUI loop."""
        if self._quitting:
            return
        self._quitting = True
        logger.info("shutting down the desktop app")
        for closer in (
            lambda: self.hotkey and self.hotkey.stop(),
            lambda: self.tray and self.tray.stop(),
            lambda: self.server and self.server.stop(),
            lambda: self.lock and self.lock.release(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                logger.debug("shutdown step failed", exc_info=True)
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> int:
        """Start everything and block on the native GUI loop."""
        import webview

        url = self.start_backend()
        self.create_window(url)
        self.start_tray()
        self.start_hotkey()
        self._check_updates_async()

        state = self.info()
        logger.info(
            "desktop ready — tray=%s hotkey=%s (%s)",
            state["tray_active"],
            state["hotkey_active"],
            self.config.hotkey,
        )
        print(f"{APP_NAME_EN} v{APP_VERSION}")
        print(f"  • UI:       {url}")
        print(f"  • workspace: {self.settings.work_dir}")
        print(f"  • tray:      {'on' if state['tray_active'] else 'unavailable'}")
        print(f"  • hotkey:    {self.config.hotkey if state['hotkey_active'] else 'unavailable'}")

        try:
            webview.start(debug=self.config.debug)
        finally:
            self.quit()
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    """``persian-local-desktop`` entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="persian-local-desktop", description=f"{APP_NAME_EN} — native desktop app"
    )
    parser.add_argument("--port", type=int, help="port for the embedded web UI")
    parser.add_argument("--hotkey", help=f"global hotkey (default: {DEFAULT_HOTKEY})")
    parser.add_argument("--hidden", action="store_true", help="start minimised to the tray")
    parser.add_argument("--no-tray", action="store_true", help="do not create a tray icon")
    parser.add_argument("--no-updates", action="store_true", help="skip the update check")
    parser.add_argument("--debug", action="store_true", help="open the webview devtools")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="serve the UI and open the system browser instead of a native window",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = load_settings()
    setup_logging(settings.data_dir)
    log_platform_summary()

    config = DesktopConfig.from_env()

    # Headless server detection
    if is_headless_server():
        print(
            "⚠️  نمایشگر یافت نشد — حالت سرور فعال می‌شود.\n"
            "رابط وب در مرورگر قابل دسترسی خواهد بود.",
            file=sys.stderr,
        )
        # Fall back to web server mode
        from ..web.app import run_web
        return run_web(["--host", "0.0.0.0", "--port", str(config.port)])
    if args.port:
        config.port = args.port
    if args.hotkey:
        config.hotkey = args.hotkey
    if args.hidden:
        config.start_hidden = True
    if args.no_updates:
        config.check_updates = False
    if args.debug:
        config.debug = True

    # --- single instance ------------------------------------------------
    lock = SingleInstance(settings.data_dir)
    app = DesktopApp(settings=settings, config=config)
    app.lock = lock
    try:
        lock.acquire(on_activate=app.show_window)
    except AlreadyRunning:
        print("دستیار از قبل در حال اجراست — پنجرهٔ موجود نمایش داده می‌شود.")
        lock.signal_existing()
        return 0

    # --- browser fallback ------------------------------------------------
    if args.browser or not is_pywebview_available():
        if not args.browser:
            msg = (
                "pywebview یافت نشد؛ رابط در مرورگر باز می‌شود.\n"
                "برای پنجرهٔ بومی: pip install pywebview pystray"
            )
            if sys.platform.startswith("linux"):
                msg += (
                    "\n\nروی لینوکس، pywebview به GTK یا Qt نیاز دارد. "
                    "نصب کنید:\n"
                    "  sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1\n"
                    "  pip install pywebview[gtk]"
                )
            print(msg, file=sys.stderr)
        url = app.start_backend()
        if not args.no_tray:
            app.start_tray()
        app.start_hotkey()
        print(f"UI: {url}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            app.quit()
        return 0

    if args.no_tray:
        config.minimize_to_tray = False
    try:
        return app.run()
    except KeyboardInterrupt:
        app.quit()
        return 0
