"""Encoding helpers for robust subprocess output decoding on Persian Windows.

Windows with Persian locale often uses cp720 / cp1256 which breaks
UTF-8 output from subprocess when text=True is used.  We always
capture bytes and decode ourselves with a smart fallback chain.
"""

from __future__ import annotations

import ctypes
import locale
import sys
from typing import Any

__all__ = [
    "TEXT_IO",
    "decode_output",
    "looks_like_mojibake",
    "repair_mojibake",
]


# Pass to subprocess.run / Popen so we always get bytes
TEXT_IO: dict[str, Any] = {"text": False, "encoding": None}


def _get_windows_console_codepage() -> str | None:
    """Return the best available Windows console codepage name, or None."""
    if sys.platform != "win32":
        return None
    try:
        # Try console CP first (most relevant for subprocess)
        cp = ctypes.windll.kernel32.GetConsoleCP()
        if cp and cp != 0:
            return f"cp{cp}"
    except Exception:  # pragma: no cover
        pass
    try:
        cp = ctypes.windll.kernel32.GetOEMCP()
        if cp and cp != 0:
            return f"cp{cp}"
    except Exception:  # pragma: no cover
        pass
    try:
        cp = ctypes.windll.kernel32.GetACP()
        if cp and cp != 0:
            return f"cp{cp}"
    except Exception:  # pragma: no cover
        pass
    return None


def decode_output(raw: bytes | str | None) -> str:
    """Decode subprocess output bytes using a robust fallback chain.

    Never raises.  Accepts str / None / bytes and returns str.
    Order of attempts:
      1. utf-8-sig
      2. utf-8
      3. Windows console codepage (GetConsoleCP / GetOEMCP / GetACP)
      4. cp1256 (Persian)
      5. cp1252
      6. locale.getpreferredencoding()
      7. utf-8 with errors='replace'
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""

    candidates: list[str] = ["utf-8-sig", "utf-8"]

    win_cp = _get_windows_console_codepage()
    if win_cp:
        candidates.append(win_cp)

    candidates.extend(["cp1256", "cp1252"])

    try:
        pref = locale.getpreferredencoding(False)
        if pref and pref.lower() not in [c.lower() for c in candidates]:
            candidates.append(pref)
    except Exception:  # pragma: no cover
        pass

    candidates.append("utf-8")  # final fallback with replace

    for enc in candidates:
        try:
            return raw.decode(enc, errors="strict")
        except UnicodeDecodeError:
            continue
        except Exception:  # pragma: no cover
            continue

    # Last resort
    return raw.decode("utf-8", errors="replace")


def looks_like_mojibake(text: str) -> bool:
    """Heuristic to detect mojibake caused by UTF-8 bytes read as latin-1 / cp1252.

    Returns True only if density of mojibake characters is high enough
    and at least 3 such characters appear.
    """
    if not text or len(text) < 5:
        return False

    mojibake_chars = set("ÙØÚÃÂ¯±©")
    count = sum(1 for ch in text if ch in mojibake_chars)
    if count < 3:
        return False

    density = count / len(text)
    return density > 0.08


def repair_mojibake(text: str) -> str:
    """Attempt to repair mojibake by re-encoding as cp1252 and decoding as utf-8."""
    if not text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except Exception:
        return text
