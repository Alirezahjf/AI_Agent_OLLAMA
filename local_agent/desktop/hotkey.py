"""Global hotkey registration (default ``Ctrl+Alt+A``).

Windows gets a real system-wide hotkey through ``RegisterHotKey`` in
``user32.dll`` — no third-party dependency, just ``ctypes``.  A dedicated
thread owns the registration because ``RegisterHotKey`` binds the hotkey
to the calling thread's message queue.

On other platforms the manager degrades to a no-op that reports
``supported = False`` so the desktop app still runs (useful for
development on Linux/macOS and for the test suite).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

from ..core.logging_setup import get_logger


logger = get_logger("desktop.hotkey")


DEFAULT_HOTKEY = "ctrl+alt+a"

# --- Win32 modifier flags (winuser.h) --------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "cmd": MOD_WIN,
}

# Virtual-key codes for the keys we support as the trigger.
_VK_NAMES: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}
for _i in range(1, 25):  # F1..F24
    _VK_NAMES[f"f{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    _VK_NAMES[_c.lower()] = ord(_c)


class HotkeyError(Exception):
    """The hotkey string is malformed or the OS refused the registration."""


@dataclass(frozen=True)
class ParsedHotkey:
    """A hotkey broken into Win32 modifier flags plus a virtual-key code."""

    modifiers: int
    vk: int
    text: str

    @property
    def has_modifier(self) -> bool:
        return self.modifiers != 0


def parse_hotkey(spec: str) -> ParsedHotkey:
    """Parse ``"ctrl+alt+a"`` into modifier flags and a virtual-key code.

    Raises :class:`HotkeyError` for empty specs, unknown key names, or
    specs that contain no non-modifier key.
    """
    text = str(spec or "").strip().lower()
    if not text:
        raise HotkeyError("empty hotkey")
    parts = [p.strip() for p in text.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"could not parse hotkey: {spec!r}")

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[part]
            continue
        if key is not None:
            raise HotkeyError(f"hotkey {spec!r} has more than one trigger key")
        key = part

    if key is None:
        raise HotkeyError(f"hotkey {spec!r} needs a key besides modifiers")
    vk = _VK_NAMES.get(key)
    if vk is None:
        raise HotkeyError(f"unknown key in hotkey: {key!r}")
    return ParsedHotkey(modifiers=modifiers | MOD_NOREPEAT, vk=vk, text="+".join(parts))


def is_supported() -> bool:
    """True when the running platform can register a global hotkey."""
    return sys.platform == "win32"


class HotkeyManager:
    """Registers one global hotkey and invokes a callback when pressed.

    The manager is safe to construct anywhere; ``start`` is a no-op that
    returns ``False`` on unsupported platforms.
    """

    def __init__(self, hotkey: str = DEFAULT_HOTKEY, callback: Callable[[], None] | None = None):
        self.hotkey = hotkey or DEFAULT_HOTKEY
        self.callback = callback
        self.parsed = parse_hotkey(self.hotkey)
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop = threading.Event()
        self._registered = threading.Event()
        self._error: str | None = None

    # ------------------------------------------------------------ state

    @property
    def supported(self) -> bool:
        return is_supported()

    @property
    def active(self) -> bool:
        return self._registered.is_set()

    @property
    def error(self) -> str | None:
        return self._error

    # -------------------------------------------------------- lifecycle

    def start(self, *, timeout: float = 3.0) -> bool:
        """Register the hotkey.  Returns True when it is live."""
        if not self.supported:
            self._error = f"global hotkeys are not supported on {sys.platform}"
            logger.info(self._error)
            return False
        if self._thread is not None and self._thread.is_alive():
            return self.active
        self._stop.clear()
        self._registered.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="desktop-hotkey", daemon=True)
        self._thread.start()
        self._registered.wait(timeout=timeout)
        return self.active

    def stop(self) -> None:
        """Unregister the hotkey and stop the message loop."""
        self._stop.set()
        if self._thread_id is not None:
            try:
                import ctypes

                ctypes.windll.user32.PostThreadMessageW(  # type: ignore[attr-defined]
                    self._thread_id, WM_QUIT, 0, 0
                )
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._registered.clear()

    def rebind(self, hotkey: str) -> bool:
        """Swap to a different hotkey at runtime."""
        parsed = parse_hotkey(hotkey)
        self.stop()
        self.hotkey = parsed.text
        self.parsed = parsed
        return self.start()

    # ------------------------------------------------------ message loop

    def _run(self) -> None:  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())  # type: ignore[attr-defined]
        hotkey_id = 1

        if not user32.RegisterHotKey(None, hotkey_id, self.parsed.modifiers, self.parsed.vk):
            self._error = (
                f"Windows refused the hotkey {self.hotkey!r} "
                "(another application probably owns it)"
            )
            logger.warning(self._error)
            self._registered.clear()
            return

        logger.info("global hotkey registered: %s", self.hotkey)
        self._registered.set()
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result in (0, -1):
                    break
                if message.message == WM_HOTKEY and self.callback is not None:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001
                        logger.exception("hotkey callback failed")
        finally:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:  # noqa: BLE001
                pass
            self._registered.clear()
            logger.info("global hotkey released: %s", self.hotkey)
