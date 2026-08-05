"""Gmail actions for the agent loop (same pattern as telegram.*).

The client lives in ``context.extra["gmail"]`` and is owned by
:class:`BridgeHandlers`.  ``gmail.send`` is Destructive and honours
``gmail.confirm_send`` even in ``confirm_mode="never"``.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk

_NOT_CONNECTED_HINT = (
    "جیمیل وصل نیست. در تنظیمات وب credentials.json یا App Password را "
    "تنظیم کنید و دکمهٔ «اتصال جیمیل» را بزنید."
)


def register_gmail(registry: ActionRegistry, context: ActionContext) -> None:
    confirm_send = lambda _safety, _args: bool(
        context.runtime.settings.gmail.confirm_send
    )
    confirm_skip = lambda _safety, _args: not bool(
        context.runtime.settings.gmail.confirm_send
    )

    registry.decorator(
        name="gmail.list_unread",
        description=(
            "فهرست ایمیل‌های خوانده‌نشدهٔ جیمیل کاربر (حداکثر limit مورد) با موضوع، "
            "فرستنده و تاریخ. SAFE."
        ),
        parameters={"limit": {"type": "integer", "description": "حداکثر تعداد (پیش‌فرض 20)"}},
    )(list_unread)

    registry.decorator(
        name="gmail.search",
        description=(
            "جست‌وجوی ایمیل در جیمیل کاربر بر اساس عبارت query (موضوع/متن/فرستنده). SAFE."
        ),
        parameters={
            "query": {"type": "string", "description": "عبارت جست‌وجو"},
            "limit": {"type": "integer", "description": "حداکثر نتیجه (پیش‌فرض 20)"},
        },
        required=("query",),
    )(search)

    registry.decorator(
        name="gmail.read",
        description=(
            "خواندن کامل یک ایمیل با شناسه (id) — موضوع، فرستنده و متن. SAFE."
        ),
        parameters={"id": {"type": "string", "description": "شناسهٔ ایمیل"}},
        required=("id",),
    )(read)

    registry.decorator(
        name="gmail.send",
        description=(
            "ارسال ایمیل از حساب جیمیل کاربر. DESTRUCTIVE — همیشه تأیید می‌خواهد."
        ),
        parameters={
            "to": {"type": "string", "description": "آدرس گیرنده"},
            "subject": {"type": "string", "description": "موضوع"},
            "body": {"type": "string", "description": "متن ایمیل"},
        },
        required=("to", "subject", "body"),
        risk_level=Risk.DESTRUCTIVE,
        confirm_override=confirm_send,
        confirm_skip=confirm_skip,
    )(send)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(context: ActionContext) -> Any:
    client = context.extra.get("gmail")
    if client is None:
        raise DependencyMissing(
            "gmail client is not configured",
            install_hint="بخش جیمیل هنوز وصل نشده است. " + _NOT_CONNECTED_HINT,
        )
    if not client.is_connected:
        raise DependencyMissing(
            "gmail client is not connected",
            install_hint=_NOT_CONNECTED_HINT,
        )
    return client


def _format_messages(messages: list[Any]) -> str:
    if not messages:
        return "ایمیلی پیدا نشد."
    lines = [f"  {m.to_text()}" for m in messages]
    return f"تعداد {len(messages)} ایمیل:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def list_unread(*, limit: int = 20, context: ActionContext) -> str:
    messages = _client(context).list_unread(limit=max(1, int(limit or 20)))
    return "📬 ایمیل‌های خوانده‌نشده:\n" + _format_messages(messages)


@risk(Risk.SAFE)
def search(*, query: str, limit: int = 20, context: ActionContext) -> str:
    if not isinstance(query, str) or not query.strip():
        raise AssistantError("query must be a non-empty string")
    messages = _client(context).search(query, limit=max(1, int(limit or 20)))
    return f"نتایج جست‌وجوی «{query}»:\n" + _format_messages(messages)


@risk(Risk.SAFE)
def read(*, id: str, context: ActionContext) -> str:
    if not isinstance(id, str) or not id.strip():
        raise AssistantError("id must be a non-empty string")
    message = _client(context).read(id.strip())
    return (
        f"📧 ایمیل [{message.id}]\n"
        f"  موضوع: {message.subject}\n"
        f"  از: {message.sender}\n"
        f"  تاریخ: {message.date}\n\n"
        f"{message.snippet}"
    )


@risk(Risk.DESTRUCTIVE)
def send(*, to: str, subject: str, body: str, context: ActionContext) -> str:
    if not isinstance(to, str) or "@" not in to:
        raise AssistantError("آدرس گیرندهٔ ایمیل نامعتبر است")
    if not isinstance(subject, str) or not subject.strip():
        raise AssistantError("subject must be a non-empty string")
    result = _client(context).send(to.strip(), subject, body)
    return f"✅ ایمیل به «{to}» ارسال شد ({result})"
