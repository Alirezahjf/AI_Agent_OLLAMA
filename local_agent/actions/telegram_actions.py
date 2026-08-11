"""Personal Telegram (Telethon) actions for the agent loop.

These tools talk to the *user's own* Telegram account(s) through the
``PersonalTelegram`` wrapper (``local_agent.telegram.client``), not to
the Telegram bot.  With multiple accounts (F2) every tool accepts an
optional ``account`` name (default: the active account) and each account
honours its own ``confirm_send`` flag.

Risk levels follow the README contract:

* read-only tools (``list_chats``, ``search_messages``, ``get_me``,
  ``search_contacts``, ``get_chat_history``, ``get_profile``,
  ``download_media``, ``mark_read``, ``resolve_username``,
  ``list_accounts``, ``switch_account``) — Safe
* sending tools (``send_message``, ``send_photo``, ``send_file``,
  ``send_video/voice/audio/document/sticker/animation``,
  ``send_location``, ``reply_to``, ``forward_message``) — Destructive

``confirm_send`` is honoured per account even in ``confirm_mode="never"``:
when True (the default) every outgoing message still asks first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk

_NOT_CONNECTED_HINT = (
    "تلگرام شخصی وصل نیست. ابتدا در تنظیمات (دکمهٔ «اتصال تلگرام») یا با دستور "
    "/telegram connect در CLI وصل شوید."
)


def register_telegram(registry: ActionRegistry, context: ActionContext) -> None:
    confirm_send = _telegram_confirm_send(context)

    # ---- Safe / read-only -----------------------------------------------
    registry.decorator(
        name="telegram.list_accounts",
        description=(
            "فهرست اکانت‌های شخصی تلگرام با نام، شماره و وضعیت اتصال هرکدام و نام اکانت فعال. "
            "برای تعویض اکانت از telegram.switch_account استفاده کن. SAFE."
        ),
        parameters={},
    )(list_accounts)

    registry.decorator(
        name="telegram.switch_account",
        description=(
            "تغییر اکانت فعال تلگرام به نام داده‌شده (فقط نام‌ها را می‌بینید/عوض می‌کنید). "
            "بعد از تعویض، اکشن‌های بعدی روی این اکانت اجرا می‌شوند. SAFE."
        ),
        parameters={"name": {"type": "string", "description": "نام اکانت (مثلاً «اصلی» یا «کار»)"}},
        required=("name",),
    )(switch_account)

    registry.decorator(name="telegram.add_account", description="ثبت اکانت تلگرام در تنظیمات؛ رازها را نمایش نمی‌دهد. SAFE.",
                       parameters={"name": {"type": "string"}, "phone": {"type": "string"},
                                   "session_name": {"type": "string"}}, required=("name", "phone"))(add_account)
    registry.decorator(name="telegram.remove_account", description="حذف اکانت و فایل سشن با تأیید دومرحله‌ای. DESTRUCTIVE.",
                       parameters={"name": {"type": "string"}, "confirmed": {"type": "boolean"}}, required=("name",))(remove_account)

    registry.decorator(
        name="telegram.list_chats",
        description=(
            "لیست گفتگوهای اکانت شخصی تلگرام کاربر (حداکثر limit مورد). "
            "هر گفتگو شامل شناسه، عنوان، نام کاربری، گروه بودن، آخرین پیام و تعداد خوانده‌نشده است. SAFE."
        ),
        parameters={
            "limit": {"type": "integer", "description": "حداکثر تعداد گفتگو (پیش‌فرض 30)"},
            "kind": {"type": "string", "enum": ["all", "private", "group", "channel", "bot"], "description": "نوع چت برای فیلتر سمت تلگرام"},
            "query": {"type": "string", "description": "فیلتر نام یا نام کاربری"},
            "sort": {"type": "string", "enum": ["", "unread"], "description": "مرتب‌سازی اختیاری"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
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
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("chat", "query"),
    )(search_messages)

    registry.decorator(
        name="telegram.get_me",
        description=(
            "مشخصات حساب شخصی تلگرام متصل (نام، نام کاربری، شماره). SAFE."
        ),
        parameters={"account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"}},
    )(get_me)

    registry.decorator(
        name="telegram.search_contacts",
        description=(
            "جست‌وجو در دفترچه تلفن/مخاطبین تلگرام بر اساس نام، نام کاربری یا شماره. "
            "نتیجه فهرستی از افراد با شناسه/نام/نام کاربری/شماره است. SAFE."
        ),
        parameters={
            "query": {"type": "string", "description": "عبارت جست‌وجو (نام / نام کاربری / شماره)"},
            "limit": {"type": "integer", "description": "حداکثر نتیجه (پیش‌فرض 30)"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("query",),
    )(search_contacts)

    registry.decorator(
        name="telegram.get_chat_history",
        description=(
            "تاریخچهٔ پیام‌های یک چت تلگرام (جدیدترها اول)؛ با offset_id می‌توانید قدیمی‌تر از یک پیام را بگیرید. "
            "نتیجه فهرستی از پیام‌ها با متن، فرستنده و زمان است. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "limit": {"type": "integer", "description": "حداکثر پیام (پیش‌فرض 30)"},
            "offset_id": {"type": "integer", "description": "پیام‌های قدیمی‌تر از این شناسه (اختیاری)"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("chat",),
    )(get_chat_history)

    registry.decorator(
        name="telegram.get_profile",
        description=(
            "مشخصات یک چت/کاربر تلگرام: نام، نام کاربری، بیو، شماره و مسیر عکس پروفایل (در صورت وجود). "
            "عکس در پوشهٔ media ذخیره و مسیر واقعی برگردانده می‌شود. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("chat",),
    )(get_profile)

    registry.decorator(
        name="telegram.download_media",
        description=(
            "دانلود مدیای یک پیام تلگرام (تصویر/ویدیو/صدا/فایل) به پوشهٔ data_dir/media و برگرداندن مسیر واقعی. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "msg_id": {"type": "integer", "description": "شناسهٔ پیام"},
            "filename": {"type": "string", "description": "نام فایل خروجی (اختیاری)"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("chat", "msg_id"),
    )(download_media)

    registry.decorator(
        name="telegram.mark_read",
        description=(
            "علامت‌گذاری همهٔ پیام‌های یک چت به‌عنوان «خوانده‌شده». SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("chat",),
    )(mark_read)

    registry.decorator(
        name="telegram.resolve_username",
        description=(
            "گرفتن اطلاعات یک نام کاربری تلگرام (چت عمومی یا شخص) مانند @username. SAFE."
        ),
        parameters={
            "username": {"type": "string", "description": "نام کاربری (با یا بدون @)"},
            "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"},
        },
        required=("username",),
    )(resolve_username)

    # ---- Destructive / sending -----------------------------------------
    _register_send(
        registry, "telegram.send_message", confirm_send,
        "ارسال پیام متنی از اکانت شخصی کاربر به یک چت تلگرام (نام یا شناسه). DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string", "description": "نام یا شناسهٔ عددی چت"},
         "text": {"type": "string", "description": "متن پیام"}},
        required=("chat", "text"),
        context=context,
    )(send_message)

    _register_send(
        registry, "telegram.send_photo", confirm_send,
        "ارسال یک تصویر از روی دیسک به چت تلگرام با کپشن اختیاری. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string", "description": "مسیر فایل تصویر"},
         "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_photo)

    _register_send(
        registry, "telegram.send_file", confirm_send,
        "ارسال یک فایل (غیرتصویر) از روی دیسک به چت تلگرام با کپشن اختیاری. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string", "description": "مسیر فایل"},
         "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_file)

    _register_send(
        registry, "telegram.send_video", confirm_send,
        "ارسال یک ویدیو از روی دیسک به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_video)

    _register_send(
        registry, "telegram.send_voice", confirm_send,
        "ارسال یک پیام صوتی (voice note) از روی دیسک به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_voice)

    _register_send(
        registry, "telegram.send_audio", confirm_send,
        "ارسال یک فایل صوتی (آهنگ/پادکست) از روی دیسک به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_audio)

    _register_send(
        registry, "telegram.send_document", confirm_send,
        "ارسال یک فایل به‌صورت سند (بدون پیش‌نمایش) به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_document)

    _register_send(
        registry, "telegram.send_sticker", confirm_send,
        "ارسال یک استیکر (فایل .webp/.tgs) به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_sticker)

    _register_send(
        registry, "telegram.send_animation", confirm_send,
        "ارسال یک انیمیشن/GIF به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
        required=("chat", "path"),
        context=context,
    )(send_animation)

    _register_send(
        registry, "telegram.send_location", confirm_send,
        "ارسال موقعیت مکانی (lat/lng) به چت تلگرام. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"},
         "lat": {"type": "number", "description": "عرض جغرافیایی"},
         "lng": {"type": "number", "description": "طول جغرافیایی"}},
        required=("chat", "lat", "lng"),
        context=context,
    )(send_location)

    _register_send(
        registry, "telegram.reply_to", confirm_send,
        "پاسخ به یک پیام مشخص در چت تلگرام (با شناسهٔ پیام). DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string"}, "msg_id": {"type": "integer"},
         "text": {"type": "string"}},
        required=("chat", "msg_id", "text"),
        context=context,
    )(reply_to)

    _register_send(
        registry, "telegram.forward_message", confirm_send,
        "انتقال/فوروارد یک پیام از یک چت به چت دیگر. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
        {"chat": {"type": "string", "description": "چت مقصد"},
         "from_chat": {"type": "string", "description": "چت مبدأ"},
         "msg_id": {"type": "integer"}},
        required=("chat", "from_chat", "msg_id"),
        context=context,
    )(forward_message)

    # قابلیت‌های مدیریتی پیشرفته (God-Mode)
    for action_name, function, params, required, risk_level in (
        ("telegram.delete_message", delete_message, {"chat": {"type": "string"}, "msg_id": {"type": "integer"}}, ("chat", "msg_id"), Risk.DESTRUCTIVE),
        ("telegram.edit_message", edit_message, {"chat": {"type": "string"}, "msg_id": {"type": "integer"}, "text": {"type": "string"}}, ("chat", "msg_id", "text"), Risk.DESTRUCTIVE),
        ("telegram.list_contacts", list_contacts, {"limit": {"type": "integer"}}, (), Risk.SAFE),
        ("telegram.get_contact_info", get_contact_info, {"contact": {"type": "string"}}, ("contact",), Risk.SAFE),
        ("telegram.add_contact", add_contact, {"phone": {"type": "string"}, "first_name": {"type": "string"}, "last_name": {"type": "string"}}, ("phone", "first_name"), Risk.DESTRUCTIVE),
        ("telegram.delete_contact", delete_contact, {"contact": {"type": "string"}}, ("contact",), Risk.DESTRUCTIVE),
        ("telegram.block_user", block_user, {"contact": {"type": "string"}}, ("contact",), Risk.DESTRUCTIVE),
        ("telegram.unblock_user", unblock_user, {"contact": {"type": "string"}}, ("contact",), Risk.DESTRUCTIVE),
        ("telegram.join_channel", join_channel, {"channel": {"type": "string"}}, ("channel",), Risk.DESTRUCTIVE),
        ("telegram.leave_channel", leave_channel, {"channel": {"type": "string"}}, ("channel",), Risk.DESTRUCTIVE),
        ("telegram.list_members", list_members, {"chat": {"type": "string"}, "limit": {"type": "integer"}}, ("chat",), Risk.SAFE),
        ("telegram.list_admins", list_admins, {"chat": {"type": "string"}, "limit": {"type": "integer"}}, ("chat",), Risk.SAFE),
        ("telegram.update_profile", update_profile, {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "about": {"type": "string"}}, (), Risk.DESTRUCTIVE),
        ("telegram.update_username", update_username, {"username": {"type": "string"}}, ("username",), Risk.DESTRUCTIVE),
        ("telegram.set_profile_photo", set_profile_photo, {"path": {"type": "string"}}, ("path",), Risk.DESTRUCTIVE),
        ("telegram.set_online_status", set_online_status, {"online": {"type": "boolean"}}, (), Risk.DESTRUCTIVE),
        # بخش سشن و امنیت
        ("telegram.get_sessions", get_sessions, {}, (), Risk.SAFE),
        ("telegram.terminate_session", terminate_session, {"hash": {"type": "integer"}}, ("hash",), Risk.SYSTEM),
        ("telegram.get_privacy", get_privacy_settings, {}, (), Risk.SAFE),
        # بخش اتوماسیون و اکسپورت
        ("telegram.export_chat", export_chat, {"chat": {"type": "string"}, "limit": {"type": "integer"}, "fmt": {"type": "string", "enum": ["json", "txt"], "description": "فرمت خروجی (پیش‌فرض json)"}}, ("chat",), Risk.SAFE),
        ("telegram.bulk_send", bulk_send, {"targets": {"type": "array", "items": {"type": "string"}}, "text": {"type": "string"}}, ("targets", "text"), Risk.DESTRUCTIVE),
        ("telegram.bulk_forward", bulk_forward, {"from_chat": {"type": "string"}, "to_chats": {"type": "array", "items": {"type": "string"}}, "msg_id": {"type": "integer"}}, ("from_chat", "to_chats", "msg_id"), Risk.DESTRUCTIVE),
        # آمار و تحلیل
        ("telegram.statistics", get_statistics, {}, (), Risk.SAFE),
        ("telegram.chat_statistics", get_chat_statistics, {"chat": {"type": "string"}, "limit": {"type": "integer"}}, ("chat",), Risk.SAFE),
        # استخراج رسانه
        ("telegram.download_all_media", download_all_media, {"chat": {"type": "string"}, "limit": {"type": "integer"}, "types": {"type": "array", "items": {"type": "string"}, "description": "فقط این انواع: photo/video/audio/document/gif"}}, ("chat",), Risk.SAFE),
        ("telegram.download_profile_photo", download_profile_photo, {"target": {"type": "string"}}, ("target",), Risk.SAFE),
        # ابزارهای فوق‌پیشرفته (Super Tools)
        ("telegram.global_search", global_search, {"query": {"type": "string"}, "limit": {"type": "integer"}}, ("query",), Risk.SAFE),
        ("telegram.get_full_details", get_full_chat_details, {"chat": {"type": "string"}}, ("chat",), Risk.SAFE),
        ("telegram.get_pinned", get_pinned_messages, {"chat": {"type": "string"}}, ("chat",), Risk.SAFE),
    ):
        registry.decorator(name=action_name, description="ابزار مدیریت حرفه‌ای تلگرام (Telethon Super Pro).",
                           parameters={**params, "account": {"type": "string"}}, required=required,
                           risk_level=risk_level)(function)


# ---------------------------------------------------------------------------
# Implementations for new Super Tools
# ---------------------------------------------------------------------------

@risk(Risk.SAFE)
def global_search(*, query: str, limit: int = 50, account: str | None = None, context: ActionContext) -> str:
    results = _client(context, account).global_search(query, limit=int(limit))
    if not results:
        return "موردی در جستجوی جهانی یافت نشد."
    lines = [f"  • {r['name'] if 'name' in r else r['title']} (@{r['username']}) [{r['type']}] (id={r['id']})" for r in results]
    return f"نتایج جستجوی جهانی برای «{query}»:\n" + "\n".join(lines)

@risk(Risk.SAFE)
def get_full_chat_details(*, chat: str, account: str | None = None, context: ActionContext) -> str:
    details = _client(context, account).get_full_chat_details(chat)
    return "جزئیات کامل گفتگو:\n" + "\n".join(f"  {k}: {v}" for k, v in details.items())

@risk(Risk.SAFE)
def get_pinned_messages(*, chat: str, account: str | None = None, context: ActionContext) -> str:
    messages = _client(context, account).get_pinned_messages(chat)
    if not messages:
        return "هیچ پیام پین شده‌ای یافت نشد."
    lines = [f"  • [ID: {m['id']}] {m['text'][:100]}" for m in messages]
    return f"پیام‌های پین شده در «{chat}»:\n" + "\n".join(lines)


def _register_send(registry, name, confirm_send, description, parameters, *, required, context):
    """Register a destructive send action with an optional ``account`` arg."""
    params = dict(parameters)
    params["account"] = {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"}
    return registry.decorator(
        name=name,
        description=description,
        parameters=params,
        required=required,
        risk_level=Risk.DESTRUCTIVE,
        confirm_override=confirm_send,
        confirm_skip=_telegram_confirm_skip(context),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _telegram_confirm_send(context: ActionContext):
    """Per-account ``confirm_send`` override (ask when True)."""
    def override(_safety, arguments=None) -> bool:
        account = (arguments or {}).get("account")
        return bool(context.runtime.settings.telegram.account(account).confirm_send)
    return override


def _telegram_confirm_skip(context: ActionContext):
    """Per-account ``confirm_send`` skip (never ask when False).

    ``confirm_send=False`` on the target account disables confirmation even
    in ``confirm_mode=destructive`` (F1).
    """
    def skip(_safety, arguments=None) -> bool:
        account = (arguments or {}).get("account")
        return not bool(context.runtime.settings.telegram.account(account).confirm_send)
    return skip


def _client(context: ActionContext, account: str | None = None) -> Any:
    owner = context.extra.get("settings_owner")
    injected = context.extra.get("telegram")  # test fake / single-client fallback
    if owner is not None:
        tg = context.runtime.settings.telegram
        name = account or tg.active_account or "اصلی"
        # An explicitly-named account that does not exist → clear Persian error.
        if account and not any(a.name == account for a in tg.accounts):
            raise AssistantError(f"اکانت تلگرام «{account}» وجود ندارد")
        client = owner._telegram_accounts.get(name)
        if client is None and account is None:
            client = injected
    else:
        client = injected
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


def _media_dir(context: ActionContext):
    media = context.runtime.settings.data_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    return media


def _work_path(context: ActionContext, value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    return str(path.resolve())


def _format_chats(chats: list[Any]) -> str:
    def label(chat: Any) -> str:
        if getattr(chat, "is_bot", False):
            return "ربات"
        if getattr(chat, "is_channel", False):
            return "کانال"
        if getattr(chat, "is_group", False):
            return "گروه"
        return "شخصی"
    lines = [f"  • {c.title} [نوع: {label(c)}] (id={c.id})" for c in chats]
    head = f"تعداد {len(chats)} گفتگو:\n"
    return head + "\n".join(lines) if lines else "هیچ گفتگویی یافت نشد؛ در این فهرست گفتگویی نیست."


def _format_messages(messages: list[Any]) -> str:
    lines = []
    for msg in messages:
        direction = "من" if msg.is_outgoing else msg.sender
        lines.append(f"  [{msg.date:%Y-%m-%d %H:%M}] {direction}: {msg.text[:200]}")
    return "\n".join(lines) if lines else "پیامی یافت نشد."


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def add_account(*, name: str, phone: str, session_name: str = "", context: ActionContext) -> str:
    owner = context.extra.get("settings_owner")
    if owner is None:
        raise AssistantError("مدیریت اکانت در این حالت در دسترس نیست")
    owner.add_telegram_account(name, phone, session_name or None)
    return f"✅ اکانت «{name}» ثبت شد؛ برای ورود اتصال را شروع کنید."


@risk(Risk.DESTRUCTIVE)
def remove_account(*, name: str, confirmed: bool = False, context: ActionContext) -> str:
    owner = context.extra.get("settings_owner")
    if owner is None:
        raise AssistantError("مدیریت اکانت در این حالت در دسترس نیست")
    owner.remove_telegram_account(name, confirmed=bool(confirmed))
    return f"✅ اکانت «{name}» و سشن آن حذف شد."


@risk(Risk.SAFE)
def list_accounts(*, context: ActionContext) -> str:
    owner = context.extra.get("settings_owner")
    tg = context.runtime.settings.telegram
    if owner is not None:
        data = owner.telegram_accounts_status()
        lines = [
            f"  • {a['account']} — {a['phone'] or 'شماره ندارد'} — {a['state']}"
            f" — {'فعال' if a['enabled'] else 'غیرفعال'}"
            for a in data["accounts"]
        ]
        active = data["active_account"]
        return (
            f"اکانت فعال: {active}\n"
            + ("\n".join(lines) if lines else "  (هیچ اکانتی ثبت نشده)")
        )
    return f"اکانت فعال: {tg.active_account} (تعداد: {len(tg.accounts)})"


@risk(Risk.SAFE)
def switch_account(*, name: str, context: ActionContext) -> str:
    owner = context.extra.get("settings_owner")
    if owner is None:
        raise AssistantError("تعویض اکانت در این حالت در دسترس نیست")
    owner.switch_telegram_account(str(name))
    return f"✅ اکانت فعال تلگرام به «{name}» تغییر کرد."


@risk(Risk.SAFE)
def list_chats(*, limit: int = 30, kind: str = "all", query: str = "",
               sort: str = "", account: str | None = None, context: ActionContext) -> str:
    client = _client(context, account)
    if kind == "all" and not query and not sort:
        chats = client.list_chats(limit=max(1, int(limit or 30)))
    else:
        chats = client.list_chats(limit=max(1, int(limit or 30)), kind=kind,
                                  query=query, sort=sort)
    return _format_chats(chats)


@risk(Risk.SAFE)
def search_messages(
    *, chat: str, query: str, limit: int = 30, account: str | None = None, context: ActionContext
) -> str:
    if not isinstance(query, str) or not query.strip():
        raise AssistantError("query must be a non-empty string")
    messages = _client(context, account).search_messages(
        chat, query, limit=max(1, int(limit or 30))
    )
    return f"نتایج جست‌وجوی «{query}» در «{chat}»:\n" + _format_messages(messages)


@risk(Risk.SAFE)
def get_me(*, account: str | None = None, context: ActionContext) -> str:
    me = _client(context, account).get_me()
    parts = [
        f"  شناسه: {me.get('id')}",
        f"  نام: {' '.join(p for p in (me.get('first_name') or '', me.get('last_name') or '') if p).strip()}",
        f"  نام کاربری: @{str(me.get('username') or '').lstrip('@')}" if me.get("username") else "",
        f"  شماره: {me.get('phone', '')}",
    ]
    return "حساب تلگرام متصل:\n" + "\n".join(p for p in parts if p)


@risk(Risk.SAFE)
def search_contacts(*, query: str, limit: int = 30, account: str | None = None, context: ActionContext) -> str:
    results = _client(context, account).search_contacts(query, limit=max(1, int(limit or 30)))
    if not results:
        return "مخاطبی مطابق عبارت یافت نشد."
    lines = [f"  • {r['name'] or '?'}" + (f" (@{r['username']})" if r['username'] else "")
             + (f" — {r['phone']}" if r['phone'] else "") for r in results]
    return f"تعداد {len(results)} مخاطب:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def get_chat_history(*, chat: str, limit: int = 30, offset_id: int = 0,
                     account: str | None = None, context: ActionContext) -> str:
    messages = _client(context, account).get_chat_history(
        chat, limit=max(1, int(limit or 30)), offset_id=int(offset_id or 0)
    )
    chat_id = messages[0].chat_id if messages else chat
    kind = "کانال" if messages and any(m.sender == "کانال" for m in messages) else "گفتگو"
    return f"تاریخچهٔ «{chat}» (id={chat_id}، نوع: {kind}):\n" + (
        _format_messages(messages) if messages else "در این گفتگو پیامی نیست (نوع: " + kind + ")."
    )


@risk(Risk.SAFE)
def get_profile(*, chat: str, account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).get_profile(chat, _media_dir(context))
    lines = [
        f"  نام: {info['name']}",
        f"  نام کاربری: @{info['username']}" if info["username"] else "",
        f"  شماره: {info['phone']}" if info.get("phone") else "",
        f"  بیو: {info['bio']}" if info.get("bio") else "",
        f"  عکس پروفایل: {info['photo_path']}" if info.get("photo_path") else "",
    ]
    kind = "گروه" if info["is_group"] else "کاربر"
    return f"پروفایل «{chat}» ({kind}):\n" + "\n".join(p for p in lines if p)


@risk(Risk.SAFE)
def download_media(*, chat: str, msg_id: int, filename: str = "",
                   account: str | None = None, context: ActionContext) -> str:
    path = _client(context, account).download_media(
        chat, int(msg_id), filename, _media_dir(context)
    )
    return f"✅ مدیا دانلود شد: {path}"


@risk(Risk.SAFE)
def mark_read(*, chat: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).mark_read(chat)
    return f"✅ پیام‌های «{chat}» خوانده‌شده علامت خوردند."


@risk(Risk.SAFE)
def resolve_username(*, username: str, account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).resolve_username(username)
    kind = "گروه" if info["is_group"] else "کاربر"
    return (f"«{username}» ({kind}): {info['name']}"
            + (f" (id={info['id']})" if info.get("id") else ""))


@risk(Risk.DESTRUCTIVE)
def send_message(*, chat: str, text: str, account: str | None = None, context: ActionContext) -> str:
    if not isinstance(text, str) or not text.strip():
        raise AssistantError("text must be a non-empty string")
    msg = _client(context, account).send_message(chat, text)
    return f"✅ پیام به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_photo(*, chat: str, path: str, caption: str = "",
               account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_media(chat, _work_path(context, path), caption=caption or "", kind="photo")
    return f"✅ تصویر به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_file(*, chat: str, path: str, caption: str = "",
              account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_media(chat, _work_path(context, path), caption=caption or "", kind="document")
    return f"✅ فایل به «{chat}» ارسال شد (id={msg.id})"


def _send_media_kind(kind: str, *, chat: str, path: str, caption: str = "",
                     account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_media(chat, _work_path(context, path), caption=caption or "", kind=kind)
    return f"✅ {kind} به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_video(*, chat: str, path: str, caption: str = "",
               account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("video", chat=chat, path=path, caption=caption, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_voice(*, chat: str, path: str, caption: str = "",
               account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("voice", chat=chat, path=path, caption=caption, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_audio(*, chat: str, path: str, caption: str = "",
               account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("audio", chat=chat, path=path, caption=caption, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_document(*, chat: str, path: str, caption: str = "",
                  account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("document", chat=chat, path=path, caption=caption, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_sticker(*, chat: str, path: str,
                 account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("sticker", chat=chat, path=path, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_animation(*, chat: str, path: str, caption: str = "",
                   account: str | None = None, context: ActionContext) -> str:
    return _send_media_kind("animation", chat=chat, path=path, caption=caption, account=account, context=context)


@risk(Risk.DESTRUCTIVE)
def send_location(*, chat: str, lat: float, lng: float,
                  account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_location(chat, float(lat), float(lng))
    return f"✅ موقعیت مکانی به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def reply_to(*, chat: str, msg_id: int, text: str,
             account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).reply_to(chat, int(msg_id), text)
    return f"✅ پاسخ به پیام {msg_id} در «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def forward_message(*, chat: str, from_chat: str, msg_id: int,
                    account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).forward_message(chat, from_chat, int(msg_id))
    return f"✅ پیام {msg_id} از «{from_chat}» به «{chat}» انتقال یافت (id={msg.id})"

@risk(Risk.DESTRUCTIVE)
def delete_message(*, chat: str, msg_id: int, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).delete_message(chat, int(msg_id))
    return f"✅ پیام {msg_id} از «{chat}» حذف شد."

@risk(Risk.DESTRUCTIVE)
def edit_message(*, chat: str, msg_id: int, text: str, account: str | None = None, context: ActionContext) -> str:
    message = _client(context, account).edit_message(chat, int(msg_id), text)
    return f"✅ پیام {message.id} ویرایش شد."

@risk(Risk.SAFE)
def list_contacts(*, limit: int = 100, account: str | None = None, context: ActionContext) -> str:
    rows = _client(context, account).list_contacts(max(1, int(limit)))
    if not rows:
        return "مخاطبی یافت نشد."
    return "مخاطبین ({}):\n{}".format(len(rows), "\n".join(
        f"• {r['first_name']} {r['last_name']} (id={r['id']})" for r in rows))

@risk(Risk.SAFE)
def get_contact_info(*, contact: str, account: str | None = None, context: ActionContext) -> str:
    row = _client(context, account).get_contact_info(contact)
    return "اطلاعات مخاطب:\n" + "\n".join(f"  {k}: {v}" for k, v in row.items() if v not in ("", None))

@risk(Risk.DESTRUCTIVE)
def add_contact(*, phone: str, first_name: str, last_name: str = "", account: str | None = None, context: ActionContext) -> str:
    row = _client(context, account).add_contact(phone, first_name, last_name)
    return f"✅ مخاطب اضافه شد (id={row.get('id', '?')})."

@risk(Risk.DESTRUCTIVE)
def delete_contact(*, contact: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).delete_contact(contact)
    return "✅ مخاطب حذف شد."

@risk(Risk.DESTRUCTIVE)
def block_user(*, contact: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).block_user(contact)
    return "✅ کاربر مسدود شد."

@risk(Risk.DESTRUCTIVE)
def unblock_user(*, contact: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).unblock_user(contact)
    return "✅ مسدودی کاربر برداشته شد."

@risk(Risk.DESTRUCTIVE)
def join_channel(*, channel: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).join_channel(channel)
    return "✅ به کانال/گروه پیوستید."

@risk(Risk.DESTRUCTIVE)
def leave_channel(*, channel: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).leave_channel(channel)
    return "✅ از کانال/گروه خارج شدید."

@risk(Risk.SAFE)
def list_members(*, chat: str, limit: int = 100, account: str | None = None, context: ActionContext) -> str:
    rows = _client(context, account).list_members(chat, max(1, int(limit)), False)
    return "اعضای چت ({}):\n{}".format(len(rows), "\n".join(f"• {r['name']} (id={r['id']})" for r in rows))

@risk(Risk.SAFE)
def list_admins(*, chat: str, limit: int = 100, account: str | None = None, context: ActionContext) -> str:
    rows = _client(context, account).list_members(chat, max(1, int(limit)), True)
    return "مدیران چت ({}):\n{}".format(len(rows), "\n".join(f"• {r['name']} (id={r['id']})" for r in rows))

@risk(Risk.DESTRUCTIVE)
def update_profile(*, first_name: str = "", last_name: str = "", about: str = "", account: str | None = None, context: ActionContext) -> str:
    _client(context, account).update_profile(first_name, last_name, about)
    return "✅ پروفایل به‌روزرسانی شد."

@risk(Risk.DESTRUCTIVE)
def update_username(*, username: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).update_username(username)
    return "✅ نام کاربری به‌روزرسانی شد."

@risk(Risk.DESTRUCTIVE)
def set_profile_photo(*, path: str, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).set_profile_photo(path)
    return "✅ عکس پروفایل تغییر کرد."

@risk(Risk.DESTRUCTIVE)
def set_online_status(*, online: bool = True, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).set_online_status(bool(online))
    return "✅ وضعیت آنلاین به‌روزرسانی شد."


# --------------------------------------------------------------------------- #
# Sessions / privacy / export / bulk  (God-Mode — were referenced but missing)
# --------------------------------------------------------------------------- #


@risk(Risk.SAFE)
def get_sessions(*, account: str | None = None, context: ActionContext) -> str:
    sessions = _client(context, account).get_sessions()
    if not sessions:
        return "هیچ سشن/دستگاه دیگری یافت نشد."
    lines = [
        f"  • [{s.get('device_model', '?')}] {s.get('platform', '')} {s.get('system_version', '')}"
        f" — {s.get('app_name', '')}"
        f"\n     ip={s.get('ip', '?')} ({s.get('country', '')}/{s.get('region', '')})"
        f" | آخرین فعالیت: {s.get('date_active', '?')}"
        f" | hash={s.get('hash')}"
        for s in sessions
    ]
    return f"دستگاه‌ها/سشن‌های متصل ({len(sessions)}):\n" + "\n".join(lines)


@risk(Risk.SYSTEM)
def terminate_session(*, hash: int, account: str | None = None, context: ActionContext) -> str:
    _client(context, account).terminate_session(int(hash))
    return f"✅ سشن با hash={hash} قطع/خارج شد."


@risk(Risk.SAFE)
def get_privacy_settings(*, account: str | None = None, context: ActionContext) -> str:
    settings = _client(context, account).get_privacy_settings()
    return "تنظیمات حریم خصوصی:\n" + "\n".join(f"  • {k}: {v}" for k, v in settings.items())


@risk(Risk.SAFE)
def export_chat(*, chat: str, limit: int = 1000, fmt: str = "json",
                account: str | None = None, context: ActionContext) -> str:
    path = _client(context, account).export_chat(chat, limit=max(1, int(limit or 1000)), fmt=fmt)
    return f"✅ خروجی «{chat}» ذخیره شد: {path}"


@risk(Risk.DESTRUCTIVE)
def bulk_send(*, targets: list, text: str, account: str | None = None, context: ActionContext) -> str:
    if not isinstance(targets, list) or not targets:
        raise AssistantError("targets باید یک آرایهٔ غیرخالی باشد.")
    if not isinstance(text, str) or not text.strip():
        raise AssistantError("text نباید خالی باشد.")
    results = _client(context, account).bulk_send(list(targets), text)
    ok = sum(1 for v in results.values() if v)
    fail = len(results) - ok
    return f"✅ ارسال انبوه انجام شد: {ok} موفق، {fail} ناموفق از {len(results)} گیرنده."


# --------------------------------------------------------------------------- #
# Advanced analytics / mass operations / media harvesting (new capabilities)
# --------------------------------------------------------------------------- #


@risk(Risk.SAFE)
def get_statistics(*, account: str | None = None, context: ActionContext) -> str:
    stats = _client(context, account).get_statistics()
    return "آمار حساب تلگرام:\n" + "\n".join(f"  • {k}: {v}" for k, v in stats.items())


@risk(Risk.SAFE)
def get_chat_statistics(*, chat: str, limit: int = 500,
                        account: str | None = None, context: ActionContext) -> str:
    stats = _client(context, account).get_chat_statistics(chat, limit=max(1, int(limit or 500)))
    breakdown = ", ".join(f"{k}={v}" for k, v in stats.get("type_breakdown", {}).items()) or "—"
    top = ", ".join(f"{name}({n})" for name, n in stats.get("top_senders", [])[:5]) or "—"
    return (
        f"آمار «{chat}»:\n"
        f"  • مجموع پیام‌ها: {stats.get('total_messages', 0)}\n"
        f"  • تفکیک: {breakdown}\n"
        f"  • پرپیام‌ترین‌ها: {top}"
    )


@risk(Risk.DESTRUCTIVE)
def bulk_forward(*, from_chat: str, to_chats: list, msg_id: int,
                 account: str | None = None, context: ActionContext) -> str:
    if not isinstance(to_chats, list) or not to_chats:
        raise AssistantError("to_chats باید یک آرایهٔ غیرخالی باشد.")
    results = _client(context, account).bulk_forward(from_chat, list(to_chats), int(msg_id))
    ok = sum(1 for v in results.values() if v)
    fail = len(results) - ok
    return f"✅ فوروارد انبوه: {ok} موفق، {fail} ناموفق از {len(results)} مقصد."


@risk(Risk.SAFE)
def download_all_media(*, chat: str, limit: int = 50, types: list | None = None,
                       account: str | None = None, context: ActionContext) -> str:
    files = _client(context, account).download_all_media(
        chat, limit=max(1, int(limit or 50)), media_types=list(types) if types else None,
        media_dir=_media_dir(context),
    )
    if not files:
        return "مدیایی برای دانلود یافت نشد."
    return "✅ مدیاها دانلود شدند:\n" + "\n".join(f"  • {f}" for f in files[:50]) + (
        f"\n… (و {len(files) - 50} مورد دیگر)" if len(files) > 50 else ""
    )


@risk(Risk.SAFE)
def download_profile_photo(*, target: str, account: str | None = None, context: ActionContext) -> str:
    path = _client(context, account).download_profile_photo(target, media_dir=_media_dir(context))
    return f"✅ عکس پروفایل ذخیره شد: {path}"
