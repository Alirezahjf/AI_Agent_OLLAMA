"""GUI automation: mouse, keyboard, screenshots, drag-and-drop.

These tools drive the actual desktop.  They are SAFE by themselves
(mouse moves are easy to undo), but ``type_text`` may include pasting
arbitrary content.  Tools here are wrapped in :func:`register_gui` so
the CLI can decide whether to enable them.
"""

from .gui import register_gui, is_gui_available
from .screenshot import take_screenshot, Screenshot

__all__ = [
    "register_gui",
    "is_gui_available",
    "take_screenshot",
    "Screenshot",
]
