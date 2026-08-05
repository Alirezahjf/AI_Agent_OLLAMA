"""Actions that use the advanced GUI layer (UI Automation + Telegram Desktop).

These actions are registered alongside the basic ones.  They degrade
gracefully when the optional dependencies (``uiautomation``,
``pyperclip``) are not installed.
"""

from __future__ import annotations

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from .registry import ActionContext, ActionRegistry, Risk, risk

logger = get_logger("actions.gui_advanced")


def register_gui_advanced(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="list_windows_advanced",
        description=(
            "Enumerate every visible top-level window with its class name and "
            "control type using UI Automation. More detailed than list_windows. "
            "Safe (read-only)."
        ),
        parameters={
            "filter": {"type": "string"},
        },
    )(list_windows_advanced)

    registry.decorator(
        name="focus_window_advanced",
        description=(
            "Bring a window to the foreground by partial title. Uses UIA + "
            "Win32 SetForegroundWindow with the Alt-key trick to bypass the "
            "Windows foreground lock."
        ),
        parameters={"title": {"type": "string"}},
        required=("title",),
    )(focus_window_advanced)

    registry.decorator(
        name="find_controls",
        description=(
            "Find UI Automation controls matching name, class_name, automation_id, "
            "or control_type. Returns their bounding rectangles. Safe."
        ),
        parameters={
            "name": {"type": "string"},
            "class_name": {"type": "string"},
            "automation_id": {"type": "string"},
            "control_type": {"type": "string"},
            "max_results": {"type": "integer"},
        },
    )(find_controls)

    registry.decorator(
        name="send_telegram_desktop",
        description=(
            "Send a message via the official Telegram Desktop client (not the "
            "Bot API, not a user session). Opens Telegram, searches for the "
            "chat by name, clicks it, types the message, presses Enter, and "
            "verifies the message actually appeared. DESTRUCTIVE: always asks "
            "for confirmation."
        ),
        parameters={
            "chat_name": {"type": "string", "description": "Name or @username of the chat."},
            "message": {"type": "string", "description": "The message body."},
            "verify": {"type": "boolean", "description": "Read back the last message to confirm (default true)."},
        },
        required=("chat_name", "message"),
    )(send_telegram_desktop)

    registry.decorator(
        name="send_telegram_desktop_batch",
        description=(
            "Send the same message to several chats in one call. Each send is "
            "verified independently. Returns a JSON list of per-chat reports. "
            "DESTRUCTIVE: always asks for confirmation."
        ),
        parameters={
            "chat_names": {"type": "array", "items": {"type": "string"}},
            "message": {"type": "string"},
            "verify": {"type": "boolean"},
        },
        required=("chat_names", "message"),
    )(send_telegram_desktop_batch)

    # B4 — when the personal Telethon account is enabled, the model must use
    # the reliable telegram.* tools instead of driving the fragile Telegram
    # Desktop GUI.  Hide these two GUI actions so they are never chosen.
    if context.runtime.settings.telegram.enabled:
        for _name in ("send_telegram_desktop", "send_telegram_desktop_batch"):
            _action = registry.get(_name)
            _action.unavailable = True
            _action.unavailable_reason = (
                "اکانت شخصی تلگرام (Telethon) فعال است؛ برای ارسال پیام فقط از "
                "ابزارهای telegram.send_message / telegram.send_photo و... استفاده کنید."
            )


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_windows_advanced(*, filter: str = "", context: ActionContext) -> str:
    from ..gui_advanced import AdvancedGUI

    needle = (filter or "").strip().lower()
    gui = AdvancedGUI()
    infos = gui.list_windows(max_depth=6)
    if needle:
        infos = [i for i in infos if needle in i.name.lower() or needle in i.class_name.lower()]
    if not infos:
        return f"no windows matched filter {needle!r}"
    lines = [f"found {len(infos)} windows:"]
    for info in infos[:120]:
        marker = f" ({info.class_name})" if info.class_name else ""
        lines.append(f"  • {info.name}{marker}")
    if len(infos) > 120:
        lines.append(f"  ... ({len(infos) - 120} more)")
    return "\n".join(lines)


@risk(Risk.SAFE)
def focus_window_advanced(*, title: str, context: ActionContext) -> str:
    from ..gui_advanced import AdvancedGUI

    gui = AdvancedGUI()
    if gui.focus_window(title):
        return f"focused window: {title}"
    return f"window not found: {title!r}"


@risk(Risk.SAFE)
def find_controls(
    *,
    name: str = "",
    class_name: str = "",
    automation_id: str = "",
    control_type: str = "",
    max_results: int = 50,
    context: ActionContext,
) -> str:
    from ..gui_advanced import AdvancedGUI

    gui = AdvancedGUI()
    if not (name or class_name or automation_id or control_type):
        return "specify at least one of: name, class_name, automation_id, control_type"
    limit = max(1, min(int(max_results or 50), 500))
    controls = gui.find_controls(
        name=name or None,
        class_name=class_name or None,
        automation_id=automation_id or None,
        control_type=control_type or None,
        max_depth=10,
    )
    if not controls:
        return "no controls matched"
    lines = [f"found {len(controls)} controls:"]
    for control in controls[:limit]:
        x, y, w, h = control.bounding_rect
        lines.append(
            f"  • {control.name!r} [{control.control_type}] "
            f"class={control.class_name} id={control.automation_id} "
            f"rect=({x},{y},{w},{h})"
        )
    if len(controls) > limit:
        lines.append(f"  ... ({len(controls) - limit} more)")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def send_telegram_desktop(
    *,
    chat_name: str,
    message: str,
    verify: bool = True,
    context: ActionContext,
) -> str:
    from ..gui_advanced import send_message_via_telegram_desktop

    report = send_message_via_telegram_desktop(chat_name, message)
    return _format_report(report, verify=verify)


@risk(Risk.DESTRUCTIVE)
def send_telegram_desktop_batch(
    *,
    chat_names: list[str],
    message: str,
    verify: bool = True,
    context: ActionContext,
) -> str:
    if not isinstance(chat_names, list) or not chat_names:
        raise AssistantError("chat_names must be a non-empty list")
    from ..gui_advanced import TelegramDesktop

    reports = []
    try:
        with TelegramDesktop() as tg:
            for name in chat_names:
                try:
                    report = tg.send_message(name, message, verify=verify)
                except AssistantError as exc:
                    report = type("R", (), {
                        "chat_name": name, "message": message,
                        "sent": False, "verified": False,
                        "error": str(exc), "actual_last_message": "",
                        "to_dict": lambda self: {
                            "chat_name": self.chat_name,
                            "message": self.message,
                            "sent": self.sent,
                            "verified": self.verified,
                            "error": self.error,
                            "actual_last_message": self.actual_last_message,
                        },
                    })()
                reports.append(report.to_dict())
    except AssistantError as exc:
        return f"could not open Telegram: {exc}"

    import json
    summary = f"sent to {len(reports)} chats; {sum(1 for r in reports if r.get('verified'))} verified."
    return summary + "\n" + json.dumps(reports, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_report(report, *, verify: bool) -> str:
    parts = [
        f"chat: {report.chat_name}",
        f"message: {report.message}",
        f"sent: {report.sent}",
    ]
    if verify:
        parts.append(f"verified: {report.verified}")
        if report.actual_last_message:
            parts.append(f"actual_last_message: {report.actual_last_message!r}")
    if report.error:
        parts.append(f"error: {report.error}")
    return "\n".join(parts)
