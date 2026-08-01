"""Clipboard helpers (get / set / clear).

Windows: uses native ctypes APIs (no extra dependency).
macOS: uses pbcopy/pbpaste.
Linux: uses xclip/xsel or pyperclip (which wraps them). Detects
       missing tools and gives a clear Persian message instead of
       raising a raw FileNotFoundError.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.encoding import TEXT_IO, decode_output
from ..utils.platform import is_linux, is_macos, is_windows
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.clipboard")


def register_clipboard(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="clipboard_read",
        description="Read the current contents of the system clipboard as text.",
        parameters={},
    )(clipboard_read)

    registry.decorator(
        name="clipboard_write",
        description=(
            "Write text to the system clipboard. Useful for staging content before "
            "pasting it into a focused app. This is SAFE because it doesn't transmit "
            "the text anywhere."
        ),
        parameters={"text": {"type": "string"}},
        required=("text",),
    )(clipboard_write)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def clipboard_read(*, context: ActionContext) -> str:
    return _read_clipboard()


@risk(Risk.SAFE)
def clipboard_write(*, text: str, context: ActionContext) -> str:
    _write_clipboard(text)
    return f"wrote {len(text)} characters to clipboard."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_clipboard() -> str:
    if is_windows():
        return _read_clipboard_windows()

    if is_macos():
        return _read_clipboard_macos()

    return _read_clipboard_linux()


def _read_clipboard_windows() -> str:
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            raise AssistantError("OpenClipboard failed")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_wchar_p
            text_ptr = kernel32.GlobalLock(handle)
            try:
                return str(text_ptr or "")
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError):
        return ""


def _read_clipboard_macos() -> str:
    try:
        completed = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            **TEXT_IO,
            timeout=5,
            check=False,
        )
        return decode_output(completed.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read_clipboard_linux() -> str:
    # Try xclip
    if shutil.which("xclip"):
        try:
            completed = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                **TEXT_IO,
                timeout=5,
                check=False,
            )
            return decode_output(completed.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Try xsel
    if shutil.which("xsel"):
        try:
            completed = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True,
                **TEXT_IO,
                timeout=5,
                check=False,
            )
            return decode_output(completed.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Try pyperclip
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        pass

    # No clipboard tool available
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display:
        return "کلیپ‌بورد در دسترس نیست (بدون نمایشگر)."
    raise DependencyMissing(
        "برای خواندن کلیپ‌بورد روی لینوکس، xclip یا xsel لازم است. "
        "نصب کنید: sudo apt install xclip",
        install_hint="sudo apt install xclip",
    )


def _write_clipboard(text: str) -> None:
    if is_windows():
        _write_clipboard_windows(text)
        return

    if is_macos():
        _write_clipboard_macos(text)
        return

    _write_clipboard_linux(text)


def _write_clipboard_windows(text: str) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GHND = 0x0042  # GMEM_MOVEABLE | GMEM_ZEROINIT
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            raise AssistantError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            data = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(data)
            handle = kernel32.GlobalAlloc(GHND, size)
            if not handle:
                raise AssistantError("GlobalAlloc failed")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                raise AssistantError("GlobalLock failed")
            ctypes.memmove(ptr, ctypes.addressof(data), size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise AssistantError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError):
        # Fallback
        try:
            from tkinter import Tk

            root = Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
        except Exception as exc:
            raise AssistantError(f"clipboard write failed: {exc}") from exc


def _write_clipboard_macos(text: str) -> None:
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            **TEXT_IO,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"clipboard write failed: {exc}") from exc


def _write_clipboard_linux(text: str) -> None:
    # Try xclip
    if shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                **TEXT_IO,
                timeout=5,
                check=True,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Try xsel
    if shutil.which("xsel"):
        try:
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text.encode("utf-8"),
                **TEXT_IO,
                timeout=5,
                check=True,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Try pyperclip
    try:
        import pyperclip
        pyperclip.copy(text)
        return
    except Exception:
        pass

    # No clipboard tool available
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display:
        raise AssistantError(
            "کلیپ‌بورد در دسترس نیست (بدون نمایشگر). "
            "در محیط سرور، امکان نوشتن در کلیپ‌بورد وجود ندارد."
        )
    raise DependencyMissing(
        "برای نوشتن در کلیپ‌بورد روی لینوکس، xclip یا xsel لازم است. "
        "نصب کنید: sudo apt install xclip",
        install_hint="sudo apt install xclip",
    )
