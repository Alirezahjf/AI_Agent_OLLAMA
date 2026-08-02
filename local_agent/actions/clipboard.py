"""Clipboard helpers (get / set / clear).

High-level improvements:
- Windows: native ctypes with Tk fallback + retry
- macOS: pbcopy/pbpaste with error handling
- Linux: xclip/xsel/wl-copy/wl-paste/pyperclip chain, Wayland support,
         clear Persian messages, size limits.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from ..core.logging_setup import get_logger
from ..utils.platform import is_linux, is_macos, is_windows
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.clipboard")

MAX_CLIPBOARD_CHARS = 100_000


def register_clipboard(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="clipboard_read",
        description="Read the current contents of the system clipboard as text. Wayland/X11/Windows/macOS supported.",
        parameters={},
    )(clipboard_read)

    registry.decorator(
        name="clipboard_write",
        description=(
            "Write text to the system clipboard. Supports Persian/Unicode. "
            "Useful for staging content before pasting. SAFE, max 100k chars."
        ),
        parameters={"text": {"type": "string"}},
        required=("text",),
    )(clipboard_write)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def clipboard_read(*, context: ActionContext) -> str:
    text = _read_clipboard()
    if len(text) > MAX_CLIPBOARD_CHARS:
        return text[:MAX_CLIPBOARD_CHARS] + f"\n... (کلیپ‌بورد {len(text)} کاراکتر، کوتاه شد)"
    return text or "(کلیپ‌بورد خالی است)"


@risk(Risk.SAFE)
def clipboard_write(*, text: str, context: ActionContext) -> str:
    if not isinstance(text, str):
        raise AssistantError("text must be a string")
    if len(text) > MAX_CLIPBOARD_CHARS:
        raise AssistantError(f"متن کلیپ‌بورد خیلی بزرگ است ({len(text)} > {MAX_CLIPBOARD_CHARS})")
    _write_clipboard(text)
    return f"✅ {len(text)} کاراکتر در کلیپ‌بورد نوشته شد"


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
    except (OSError, AttributeError) as exc:
        logger.debug("win clipboard read failed: %s", exc)
        # Fallback Tk
        try:
            from tkinter import Tk

            root = Tk()
            root.withdraw()
            data = root.clipboard_get()
            root.destroy()
            return str(data)
        except Exception:
            return ""


def _read_clipboard_macos() -> str:
    try:
        completed = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
                encoding="utf-8",
                errors="replace",
            timeout=5,
            check=False,
        )
        return completed.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read_clipboard_linux() -> str:
    # Wayland first: wl-paste
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        try:
            completed = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            pass

    # X11: xclip
    if shutil.which("xclip"):
        try:
            completed = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            pass

    # xsel
    if shutil.which("xsel"):
        try:
            completed = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            pass

    # pyperclip
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception:
        pass

    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display:
        return "کلیپ‌بورد در دسترس نیست (بدون نمایشگر)."
    raise DependencyMissing(
        "برای خواندن کلیپ‌بورد روی لینوکس، یکی از این‌ها لازم است: wl-clipboard (Wayland) یا xclip/xsel (X11). "
        "نصب: sudo apt install wl-clipboard xclip",
        install_hint="sudo apt install wl-clipboard xclip",
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

        CF_UNICODETEXT = 13
        GHND = 0x0042
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            raise AssistantError("OpenClipboard failed (ممکن است قفل باشد)")
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
        # Fallback Tk
        try:
            from tkinter import Tk

            root = Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
        except Exception as exc:
            raise AssistantError(f"نوشتن کلیپ‌بورد ناموفق بود: {exc}") from exc


def _write_clipboard_macos(text: str) -> None:
    try:
        subprocess.run(
            ["pbcopy"],
            input=text,
            text=True,
                encoding="utf-8",
                errors="replace",
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssistantError(f"نوشتن کلیپ‌بورد ناموفق بود: {exc}") from exc


def _write_clipboard_linux(text: str) -> None:
    # Wayland: wl-copy
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        try:
            subprocess.run(
                ["wl-copy"],
                input=text,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=True,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    # X11: xclip
    if shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=True,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    # xsel
    if shutil.which("xsel"):
        try:
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=True,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    # pyperclip
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception:
        pass

    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display:
        raise AssistantError(
            "کلیپ‌بورد در دسترس نیست (بدون نمایشگر). "
            "در محیط سرور امکان نوشتن در کلیپ‌بورد وجود ندارد."
        )
    raise DependencyMissing(
        "برای نوشتن در کلیپ‌بورد روی لینوکس، wl-clipboard (Wayland) یا xclip/xsel (X11) لازم است. "
        "نصب: sudo apt install wl-clipboard xclip",
        install_hint="sudo apt install wl-clipboard xclip",
    )
