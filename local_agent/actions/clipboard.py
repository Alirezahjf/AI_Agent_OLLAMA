"""Clipboard helpers (get / set / clear).

We don't bundle pyperclip by default; the implementation prefers
Windows-native APIs (ctypes) so no extra dependency is required.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
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
        # POSIX fallback for tests.
        try:
            from tkinter import Tk  # type: ignore

            root = Tk()
            root.withdraw()
            content = root.clipboard_get()
            root.destroy()
            return str(content)
        except Exception:  # noqa: BLE001
            return ""


def _write_clipboard(text: str) -> None:
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
        # POSIX fallback
        try:
            from tkinter import Tk  # type: ignore

            root = Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
        except Exception as exc:  # noqa: BLE001
            raise AssistantError(f"clipboard write failed: {exc}") from exc
