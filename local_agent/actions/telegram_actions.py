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

    registry.decorator(
        name="telegram.list_chats",
        description=(
            "لیست گفتگوهای اکانت شخصی تلگرام کاربر (حداکثر limit مورد). "
            "هر گفتگو شامل شناسه، عنوان، نام کاربری، گروه بودن، آخرین پیام و تعداد خوانده‌نشده است. SAFE."
        ),
        parameters={
            "limit": {"type": "integer", "description": "حداکثر تعداد گفتگو (پیش‌فرض 30)"},
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


def _format_chats(chats: list[Any]) -> str:
    lines = [f"  • {c.title} (id={c.id}){' [گروه]' if c.is_group else ''}" for c in chats]
    head = f"تعداد {len(chats)} گفتگو:\n"
    return head + "\n".join(lines) if lines else "هیچ گفتگویی یافت نشد."


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
def list_chats(*, limit: int = 30, account: str | None = None, context: ActionContext) -> str:
    chats = _client(context, account).list_chats(limit=max(1, int(limit or 30)))
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
        f"  نام: {me.get('first_name', '')} {me.get('last_name', '')}".rstrip(),
        f"  نام کاربری: @{me.get('username', '')}" if me.get("username") else "",
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
    return f"تاریخچهٔ «{chat}»:\n" + _format_messages(messages)


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
    msg = _client(context, account).send_media(chat, path, caption=caption or "", kind="photo")
    return f"✅ تصویر به «{chat}» ارسال شد (id={msg.id})"


@risk(Risk.DESTRUCTIVE)
def send_file(*, chat: str, path: str, caption: str = "",
              account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_media(chat, path, caption=caption or "", kind="document")
    return f"✅ فایل به «{chat}» ارسال شد (id={msg.id})"


def _send_media_kind(kind: str, *, chat: str, path: str, caption: str = "",
                     account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).send_media(chat, path, caption=caption or "", kind=kind)
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
