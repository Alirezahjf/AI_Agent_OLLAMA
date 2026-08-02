"""Screenshot capture.

Uses ``mss`` when available (much faster than pyautogui's screenshot
on multi-monitor setups) with a graceful PIL.ImageGrab fallback.
Returns a :class:`Screenshot` that knows its native size, which the
agent can use to compute coordinates for ``mouse_click`` and friends.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image


@dataclass
class Screenshot:
    """A captured frame.

    The :attr:`image` attribute is a :class:`PIL.Image.Image`. The
    helper ``path`` is provided for callers that want to write the
    PNG to disk immediately.
    """

    image: Image.Image
    taken_at: float
    backend: str = "unknown"  # mss | imagegrab | placeholder

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    def save(self, target: Any, format: str = "PNG", **kwargs) -> None:
        # Accept an explicit format for backward compatibility with
        # callers that did `screenshot.save(path, "PNG")`. PIL's save
        # signature is (fp, format, **params).
        self.image.save(target, format, **kwargs)

    def to_bytes(self) -> bytes:
        buffer = BytesIO()
        self.image.save(buffer, "PNG")
        return buffer.getvalue()


def take_screenshot(monitor: int = 0) -> Screenshot:
    """Capture the primary screen and return a :class:`Screenshot`.

    Tries ``mss`` first, then ``PIL.ImageGrab``, then a Pillow
    full-screen ``Image`` for headless test runs.
    """
    try:
        import mss  # type: ignore

        with mss.mss() as grabber:
            if monitor >= len(grabber.monitors):
                monitor = 0
            raw = grabber.grab(grabber.monitors[monitor])
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        return Screenshot(image=image, taken_at=time.time(), backend="mss")
    except Exception:  # noqa: BLE001
        pass

    if os.name == "nt":
        from PIL import ImageGrab

        image = ImageGrab.grab()
        return Screenshot(image=image, taken_at=time.time(), backend="imagegrab")

    # POSIX fallback (headless / tests): build a small grey image.
    image = Image.new("RGB", (1280, 720), (24, 24, 24))
    return Screenshot(image=image, taken_at=time.time(), backend="placeholder")
