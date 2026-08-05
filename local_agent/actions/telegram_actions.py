"""Personal Telegram (Telethon) actions for the agent loop.

These tools talk to the *user's own* Telegram account through the
``PersonalTelegram`` wrapper (``local_agent.telegram.client``), not to
the Telegram bot.  The client instance lives in ``context.extra`` and is
owned by :class:`BridgeHandlers`; actions merely use it.

Risk levels follow the README contract:

* ``telegram.list_chats`` / ``telegram.search_messages`` / ``telegram.get_me`` — Safe
* ``telegram.send_message`` / ``telegram.send_photo`` / ``telegram.send_file`` — Destructive

``telegram.confirm_send`` is honoured even in ``confirm_mode="never"``:
when it is True (the default) every outgoing message still asks for
confirmation first.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk

_NOT_CONNECTED_HINT = (
    "تلگرام شخصی وصل نیست. ابتدا در تنظیمات (دکمهٔ «اتصال تلگرام») یا با دستور "
    "/telegram connect در CLI وصل شوید."
)


def register_telegram(registry: ActionRegistry, context: ActionContext) -> None:
    confirm_send = lambda _safety: bool(
        context.runtime.settings.telegram.confirm_send
    )

    registry.decorator(
        name="telegram.list_chats",
        description=(
            "لیست گفتگوهای اکانت شخصی تلگرام کاربر (حداکثر limit مورد). "
            "هر گفتگو شامل شناسه، عنوان، نام کاربری، گروه بودن، آخرین پیام و تعداد خوانده‌نشده است. SAFE."
        ),
        parameters={"limit": {"type": "integer", "description": "حداکثر تعداد گفتگو (پیش‌فرض 30)"}},
    )(list_chats)

    registry.decorator(
        name="telegram.search_messages",
        description=(
            "جست‌وجوی پیام در یک چت تلگرام (با نام یا شناسه). "
            "نتیجه فهرستی از پیام‌ها با متن، فرستنده و زمان است. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "query": {"type": "string", "description": "عبارت جست‌وجو"},
            "limit": {"type": "integer", "description": "حداکثر نتیجه (پیش‌فرض 30)"},
        },
        required=("chat", "query"),
    )(search_messages)

    registry.decorator(
        name="telegram.get_me",
        description=(
            "مشخصات حساب شخصی تلگرام متصل (نام، نام کاربری، شماره). SAFE."
        ),
        parameters={},
    )(get_me)

    registry.decorator(
        name="telegram.send_message",
        description=(
            "ارسال پیام متنی از اکانت شخصی کاربر به یک چت تلگرام (نام یا شناسه). "
            "DESTRUCTIVE — همیشه تأیید می‌خواهد."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "text": {"type": "string", "description": "متن پیام"},
        },
        required=("chat", "text"),
        risk_level=Risk.DESTRUCTIVE,
        confirm_override=confirm_send,
    )(send_message)

    registry.decorator(
        name="telegram.send_photo",
        description=(
            "ارسال یک تصویر از روی دیسک به چت تلگرام (نام یا شناسه) با کپشن اختیاری. "
            "DESTRUCTIVE — همیشه تأیید می‌خواهد."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "path": {"type": "string", "description": "مسیر فایل تصویر"},
            "caption": {"type": "string", "description": "متن زیر تصویر (اختیاری)"},
        },
        required=("chat", "path"),
        risk_level=Risk.DESTRUCTIVE,
        confirm_override=confirm_send,
    )(send_photo)

    registry.decorator(
        name="telegram.send_file",
        description=(
            "ارسال یک فایل (غیرتصویر) از روی دیسک به چت تلگرام (نام یا شناسه) با کپشن اختیاری. "
            "DESTRUCTIVE — همیشه تأیید می‌خواهد."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "path": {"type": "string", "description": "مسیر فایل"},
            "caption": {"type": "string", "description": "متن زیر فایل (اختیاری)"},
        },
        required=("chat", "path"),
        risk_level=Risk.DESTRUCTIVE,
        confirm_override=confirm_send,
    )(send_file)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(context: ActionContext) -> Any:
    client = context.extra.get("telegram")
    if client is None:
        raise DependencyMissing(
            "telegram client is not configured",
            install_hint="اکانت شخصی تلگرام هنوز وصل نشده است. " + _NOT_CONNECTED_HINT,
        )
    if not client.is_connected:
        raise DependencyMissing(
            "telegram client is not connected",
            install_hint=_NOT_CONNECTED_HINT,
        )
    return client


def _format_chats(chats: list[Any]) -> str:
    lines = [f"  • {c.title} (id={c.id}){' [گروه]' if c.is_group else ''}" for c in chats]
    head = f"تعداد {len(chats)} گفتگو:\n"
    return head + "\n".join(lines) if lines else "هیچ گفتگویی یافت نشد."


def _format_messages(messages: list[Any]) -> str:
    lines = []
    for msg in messages:
        direction = "من" if msg.is_outgoing else msg.sender
        lines.append(
            f"  [{msg.date:%Y-%m-%d %H:%M}] {direction}: {msg.text[:200]}"
        )
    return "\n".join(lines) if lines else "پیامی یافت نشد."


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_chats(*, limit: int = 30, context: ActionContext) -> str:
    chats = _client(context).list_chats(limit=max(1, int(limit or 30)))
    return _format_chats(chats)


@risk(Risk.SAFE)
def search_messages(
    *, chat: str, query: str, limit: int = 30, context: ActionContext
) -> str:
    if not isinstance(query, str) or not query.strip():
        raise AssistantError("query must be a non-empty string")
    messages = _client(context).search_messages(
        chat, query, limit=max(1, int(limit or 30))
    )
    return f"نتایج جست‌وجوی «{query}» در «{chat}»:\n" + _format_messages(messages)


@risk(Risk.SAFE)
def get_me(*, context: ActionContext) -> str:
    me = _client(context).get_me()
    parts = [
        f"  شناسه: {me.get('id')}",
        f"  نام: {me.get('first_name', '')} {me.get('last_name', '')}".rstrip(),
        f"  نام کاربری: @{me.get('username', '')}" if me.get("username") else "",
        f"  شماره: {me.get('phone', '')}",
    ]
    return "حساب تلگرام متصل:\n" + "\n".join(p for p in parts if p)


@risk(Risk.DESTRUCTIVE)
def send_message(*, chat: str, text: str, context: ActionContext) -> str:
    if not isinstance(text, str) or not text.strip():
        raise AssistantError("text must be a non-empty string")
    msg = _client(context).send_message(chat, text)
    return f"✅ پیام به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_photo(
    *, chat: str, path: str, caption: str = "", context: ActionContext
) -> str:
    msg = _client(context).send_photo(chat, path, caption=caption or "")
    return f"✅ تصویر به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_file(
    *, chat: str, path: str, caption: str = "", context: ActionContext
) -> str:
    msg = _client(context).send_file(chat, path, caption=caption or "")
    return f"✅ فایل به «{chat}» ارسال شد (id={msg.id})"
