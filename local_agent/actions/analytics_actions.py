"""Analytics actions — analyze people, chats, and communications.

Provides deep analysis of:
  * Telegram chats (private conversations, groups, channels)
  * Gmail conversations
  * Individual person profiles

All actions are SAFE (read-only analysis).
"""

from __future__ import annotations

from typing import Any

from ..core.analytics import (
    analyze_messages, format_chat_report, format_person_report,
)
from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_analytics(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="analytics.analyze_chat",
        description=(
            "تحلیل عمیق یک چت تلگرام: فعال‌ترین اعضا، ساعات اوج، موضوعات پرتکرار، "
            "توزیع هفتگی/ساعتی، نسبت پیام‌ها. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا ID چت"},
            "limit": {"type": "integer", "description": "تعداد پیام برای تحلیل (پیش‌فرض 500)"},
            "account": {"type": "string"},
        },
        required=("chat",),
    )(analyze_chat)

    registry.decorator(
        name="analytics.analyze_person",
        description=(
            "تحلیل پروفایل یک شخص در تلگرام: تعداد پیام، کلمات پرتکرار، "
            "فعال‌ترین ساعات، سبک ارتباطی. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا ID چت"},
            "person": {"type": "string", "description": "نام یا ID شخص (خالی = تحلیل همه)"},
            "limit": {"type": "integer"},
            "account": {"type": "string"},
        },
        required=("chat",),
    )(analyze_person)

    registry.decorator(
        name="analytics.analyze_group_members",
        description=(
            "تحلیل اعضای یک گروه/کانال تلگرام: رتبه‌بندی فعالیت، "
            "موضوعات هر فرد، ساعات اوج. SAFE."
        ),
        parameters={
            "chat": {"type": "string", "description": "نام یا ID گروه/کانال"},
            "limit": {"type": "integer", "description": "تعداد پیام (پیش‌فرض 1000)"},
            "top_n": {"type": "integer", "description": "تعداد اعضای برتر (پیش‌فرض 10)"},
            "account": {"type": "string"},
        },
        required=("chat",),
    )(analyze_group_members)

    registry.decorator(
        name="analytics.analyze_gmail",
        description=(
            "تحلیل ایمیل‌های Gmail: فرستنده‌های برتر، موضوعات پرتکرار، "
            "توزیع زمانی. SAFE."
        ),
        parameters={
            "query": {"type": "string", "description": "فیلتر Gmail search (اختیاری)"},
            "limit": {"type": "integer", "description": "تعداد ایمیل (پیش‌فرض 100)"},
        },
    )(analyze_gmail)

    registry.decorator(
        name="analytics.compare_chats",
        description=(
            "مقایسه فعالیت بین چند چت تلگرام. SAFE."
        ),
        parameters={
            "chats": {"type": "array", "items": {"type": "string"},
                      "description": "لیست نام/ID چت‌ها"},
            "limit": {"type": "integer"},
            "account": {"type": "string"},
        },
        required=("chats",),
    )(compare_chats)

    registry.decorator(
        name="analytics.schedule_report",
        description=(
            "زمان‌بندی گزارش تحلیل (analytics) برای اجرا در زمان مشخص. "
            "مثلاً «هر روز ساعت ۹ صبح چت X را تحلیل کن». DESTRUCTIVE."
        ),
        parameters={
            "at": {"type": "string", "description": "زمان اجرا (ISO, «تا ۵ دقیقه دیگر», «هر روز 09:00»)"},
            "action": {"type": "string",
                       "enum": ["analytics.analyze_chat", "analytics.analyze_group_members",
                                "analytics.analyze_gmail", "analytics.compare_chats"],
                       "description": "اکشن تحلیل برای اجرا"},
            "arguments": {"type": "object", "description": "آرگومان‌های اکشن (chat, limit, ...)"},
        },
        required=("at", "action", "arguments"),
        risk_level=Risk.DESTRUCTIVE,
    )(schedule_analytics_report)

    registry.decorator(
        name="analytics.detect_language",
        description="تشخیص زبان متن (فارسی/انگلیسی/عربی/...). SAFE.",
        parameters={
            "text": {"type": "string"},
        },
        required=("text",),
    )(detect_language)

    registry.decorator(
        name="analytics.data_analyze",
        description=(
            "تحلیل عمومی داده با Python (pandas). یک فایل CSV/JSON/Excel را "
            "می‌خواند و آمار توصیفی، groupby، یا query سفارشی اجرا می‌کند. SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر فایل داده"},
            "operation": {"type": "string",
                          "enum": ["describe", "head", "info", "value_counts",
                                   "groupby", "query", "correlation", "plot"]},
            "column": {"type": "string", "description": "نام ستون (برای value_counts/groupby)"},
            "query_str": {"type": "string", "description": "عبارت query برای فیلتر"},
            "group_column": {"type": "string", "description": "ستون groupby"},
            "limit": {"type": "integer"},
        },
        required=("path", "operation"),
    )(data_analyze)


# ===========================================================================
# Helpers
# ===========================================================================


def _get_telegram(context: ActionContext, account: str | None = None):
    owner = context.extra.get("settings_owner")
    if owner is not None:
        tg = context.runtime.settings.telegram
        name = account or tg.active_account or "اصلی"
        client = owner._telegram_accounts.get(name)
        if client is None and account is None:
            client = context.extra.get("telegram")
    else:
        client = context.extra.get("telegram")
    if client is None or not client.is_connected:
        raise DependencyMissing(
            "telegram not connected",
            install_hint="ابتدا تلگرام را وصل کنید.",
        )
    return client


def _get_my_id(client) -> str:
    try:
        me = client.get_me()
        return str(me.get("id", ""))
    except Exception:
        return ""


def _messages_to_dicts(messages) -> list[dict[str, Any]]:
    """Convert Message objects to dicts for the analytics engine."""
    result = []
    for m in messages:
        if hasattr(m, "to_dict"):
            d = m.to_dict()
        elif isinstance(m, dict):
            d = m
        else:
            d = {
                "id": getattr(m, "id", 0),
                "text": getattr(m, "text", ""),
                "date": getattr(m, "date", ""),
                "sender": getattr(m, "sender", "?"),
                "sender_id": getattr(m, "sender_id", ""),
                "is_outgoing": getattr(m, "is_outgoing", False),
                "media_type": getattr(m, "media_type", ""),
                "is_reply": getattr(m, "is_reply", False),
            }
        # Ensure date is string
        date = d.get("date", "")
        if hasattr(date, "isoformat"):
            d["date"] = date.isoformat()
        elif hasattr(date, "strftime"):
            d["date"] = date.strftime("%Y-%m-%d %H:%M:%S")
        result.append(d)
    return result


# ===========================================================================
# Implementations
# ===========================================================================


@risk(Risk.SAFE)
def analyze_chat(*, chat: str, limit: int = 500,
                 account: str | None = None, context: ActionContext) -> str:
    client = _get_telegram(context, account)
    lim = max(50, min(int(limit or 500), 5000))
    my_id = _get_my_id(client)

    messages = client.get_chat_history(chat, limit=lim)
    if not messages:
        raise AssistantError(f"پیامی در «{chat}» یافت نشد.")

    # Determine chat type
    chat_obj = messages[0]
    chat_type = "private"
    if hasattr(chat_obj, "chat_id"):
        pass  # will be set from messages

    dicts = _messages_to_dicts(messages)
    analytics = analyze_messages(
        dicts, chat_name=str(chat), chat_type="chat",
        my_user_id=my_id,
    )
    return format_chat_report(analytics)


@risk(Risk.SAFE)
def analyze_person(*, chat: str, person: str = "",
                   limit: int = 500, account: str | None = None,
                   context: ActionContext) -> str:
    client = _get_telegram(context, account)
    lim = max(50, min(int(limit or 500), 5000))
    my_id = _get_my_id(client)

    messages = client.get_chat_history(chat, limit=lim)
    if not messages:
        raise AssistantError(f"پیامی در «{chat}» یافت نشد.")

    dicts = _messages_to_dicts(messages)
    analytics = analyze_messages(
        dicts, chat_name=str(chat), chat_type="chat",
        my_user_id=my_id,
    )

    if not person:
        # Return all profiles
        if not analytics.top_members:
            return "تحلیلی موجود نیست."
        reports = [format_person_report(m) for m in analytics.top_members[:5]]
        return "\n\n---\n\n".join(reports)

    # Find specific person
    person_lower = str(person).lower()
    for member in analytics.top_members:
        if (person_lower in member.name.lower() or
            person_lower == str(member.user_id) or
            person_lower in member.username.lower()):
            return format_person_report(member)

    names = ", ".join(m.name for m in analytics.top_members[:10])
    raise AssistantError(f"شخص «{person}» یافت نشد. اعضای موجود: {names}")


@risk(Risk.SAFE)
def analyze_group_members(*, chat: str, limit: int = 1000,
                          top_n: int = 10, account: str | None = None,
                          context: ActionContext) -> str:
    client = _get_telegram(context, account)
    lim = max(100, min(int(limit or 1000), 5000))
    my_id = _get_my_id(client)

    messages = client.get_chat_history(chat, limit=lim)
    if not messages:
        raise AssistantError(f"پیامی در «{chat}» یافت نشد.")

    dicts = _messages_to_dicts(messages)
    analytics = analyze_messages(
        dicts, chat_name=str(chat), chat_type="group",
        my_user_id=my_id,
    )

    n = max(1, min(int(top_n or 10), 50))

    lines = [
        f"📊 تحلیل اعضای «{chat}» (از {analytics.total_messages} پیام):",
        f"  بازه: {analytics.date_range}",
        f"\n  🏆 رتبه‌بندی {n} عضو فعال:",
    ]

    for i, m in enumerate(analytics.top_members[:n], 1):
        peak_h = max(m.active_hours, key=m.active_hours.get) if m.active_hours else "?"
        peak_d = max(m.active_days, key=m.active_days.get) if m.active_days else "?"
        pct = (m.message_count / max(analytics.total_messages, 1)) * 100
        words = ", ".join(w for w, _ in m.top_words[:3]) if m.top_words else "—"

        lines.append(f"\n  {i}. {m.name} ({pct:.1f}%)")
        lines.append(f"     {m.message_count} پیام | {m.avg_message_length:.0f} حرف/پیام")
        lines.append(f"     فعال: {peak_d} {peak_h}:00 | ایموجی: {m.emoji_usage} | سؤال: {m.question_count}")
        lines.append(f"     موضوعات: {words}")

    return "\n".join(lines)


@risk(Risk.SAFE)
def analyze_gmail(*, query: str = "", limit: int = 100,
                  context: ActionContext) -> str:
    gmail = context.extra.get("gmail")
    if gmail is None:
        raise DependencyMissing(
            "Gmail not connected",
            install_hint="ابتدا Gmail را وصل کنید.",
        )

    lim = max(10, min(int(limit or 100), 500))
    q = str(query or "").strip()

    try:
        if q:
            emails = gmail.search(q, limit=lim)
        else:
            emails = gmail.list_unread(limit=lim)
    except Exception as exc:
        raise AssistantError(f"خواندن ایمیل ناموفق بود: {exc}")

    if not emails:
        return "ایمیلی یافت نشد."

    # Analyze
    from collections import Counter
    senders: Counter = Counter()
    subjects: Counter = Counter()
    dates: Counter = Counter()
    hours: Counter = Counter()
    total_chars = 0

    for email in emails:
        if isinstance(email, dict):
            sender = email.get("from", email.get("sender", "?"))
            subject = email.get("subject", "")
            date = str(email.get("date", ""))
            body = str(email.get("body", email.get("snippet", "")))
        else:
            sender = getattr(email, "sender", getattr(email, "from_", "?"))
            subject = getattr(email, "subject", "")
            date = str(getattr(email, "date", ""))
            body = str(getattr(email, "body", getattr(email, "snippet", "")))

        # Clean sender (extract name/email)
        sender_clean = str(sender).split("<")[0].strip().strip('"') or str(sender)[:50]
        senders[sender_clean] += 1

        if subject:
            # Extract key words from subject
            from ..core.analytics import _tokenize
            for word in _tokenize(subject):
                subjects[word] += 1

        if date:
            dates[date[:10]] += 1
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(date.replace("Z", "").replace("+00:00", "")[:19])
                hours[dt.hour] += 1
            except (ValueError, TypeError):
                pass

        total_chars += len(body)

    lines = [
        f"📧 تحلیل {len(emails)} ایمیل{' (فیلتر: ' + q + ')' if q else ''}:",
        f"  مجموع کاراکتر: {total_chars:,}",
        f"\n  👤 فرستنده‌های برتر:",
    ]
    for sender, count in senders.most_common(10):
        lines.append(f"    {sender}: {count} ایمیل")

    if subjects:
        lines.append(f"\n  📌 موضوعات پرتکرار:")
        for word, count in subjects.most_common(10):
            lines.append(f"    {word}: {count}")

    if hours:
        lines.append(f"\n  ⏰ توزیع ساعتی:")
        max_h = max(hours.values()) if hours else 1
        for h in sorted(hours.keys()):
            count = hours[h]
            bar = "▓" * int(count / max_h * 15)
            lines.append(f"    {h:02d}:00 {bar} ({count})")

    if dates:
        lines.append(f"\n  📅 توزیع تاریخ:")
        for date, count in sorted(dates.items())[-7:]:
            lines.append(f"    {date}: {count} ایمیل")

    return "\n".join(lines)


@risk(Risk.SAFE)
def compare_chats(*, chats: list[str], limit: int = 300,
                  account: str | None = None,
                  context: ActionContext) -> str:
    client = _get_telegram(context, account)
    lim = max(50, min(int(limit or 300), 2000))
    my_id = _get_my_id(client)

    results = []
    for chat in chats[:10]:
        try:
            messages = client.get_chat_history(chat, limit=lim)
            dicts = _messages_to_dicts(messages)
            analytics = analyze_messages(
                dicts, chat_name=str(chat), my_user_id=my_id,
            )
            results.append(analytics)
        except Exception as exc:
            results.append(None)

    if not any(results):
        return "هیچ چتی قابل تحلیل نبود."

    lines = ["📊 مقایسه چت‌ها:\n"]
    lines.append(f"  {'چت':25s} {'پیام':>8s} {'روزانه':>8s} {'رسانه':>6s} {'سؤال':>6s} {'فعال‌ترین ساعت':>12s}")
    lines.append(f"  {'-'*70}")

    for i, analytics in enumerate(results):
        chat_name = str(chats[i])[:25]
        if analytics is None:
            lines.append(f"  {chat_name:25s} (خطا)")
            continue
        peak_h = f"{analytics.most_active_hour}:00" if analytics.hourly_distribution else "—"
        lines.append(
            f"  {chat_name:25s} {analytics.total_messages:>8,} "
            f"{analytics.avg_messages_per_day:>8.1f} "
            f"{analytics.total_media:>6} "
            f"{analytics.total_questions:>6} "
            f"{peak_h:>12s}"
        )

    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def schedule_analytics_report(*, at: str, action: str, arguments: dict,
                              context: ActionContext) -> str:
    """Schedule an analytics action to run at a specific time."""
    scheduler = context.extra.get("scheduler")
    if scheduler is None:
        raise AssistantError("Scheduler در دسترس نیست.")

    # Validate action name
    valid_actions = {
        "analytics.analyze_chat", "analytics.analyze_group_members",
        "analytics.analyze_gmail", "analytics.compare_chats",
    }
    if action not in valid_actions:
        raise AssistantError(f"اکشن {action} معتبر نیست. مجازها: {', '.join(valid_actions)}")

    try:
        job = scheduler.schedule_task(
            at=str(at),
            action_name=str(action),
            arguments=dict(arguments),
        )
    except Exception as exc:
        raise AssistantError(f"زمان‌بندی ناموفق بود: {exc}")

    return (
        f"📅 گزارش تحلیل زمان‌بندی شد:\n"
        f"  اکشن: {action}\n"
        f"  زمان: {at}\n"
        f"  ID: {job.id if hasattr(job, 'id') else '?'}\n"
        f"  آرگومان‌ها: {arguments}"
    )


@risk(Risk.SAFE)
def detect_language(*, text: str, context: ActionContext) -> str:
    """Detect the language of a text using character frequency analysis."""
    content = str(text or "").strip()
    if not content:
        raise AssistantError("متن خالی است.")

    # Character-based language detection
    persian_range = sum(1 for c in content if '\u0600' <= c <= '\u06FF' or '\uFB50' <= c <= '\uFDFF')
    arabic_range = sum(1 for c in content if '\u0600' <= c <= '\u06FF')
    latin_range = sum(1 for c in content if c.isascii() and c.isalpha())
    cyrillic_range = sum(1 for c in content if '\u0400' <= c <= '\u04FF')
    cjk_range = sum(1 for c in content if '\u4E00' <= c <= '\u9FFF')
    total_alpha = max(1, sum(1 for c in content if c.isalpha()))

    scores = {
        "فارسی (Persian)": persian_range / total_alpha,
        "انگلیسی (English)": latin_range / total_alpha,
        "روسی (Russian)": cyrillic_range / total_alpha,
        "چینی (Chinese)": cjk_range / total_alpha,
    }

    # Distinguish Persian from Arabic (Persian has extra characters)
    persian_specific = sum(1 for c in content if c in "پچژگی‌ک")
    if persian_specific > 0 and persian_range > latin_range:
        scores["فارسی (Persian)"] += 0.1
        scores["عربی (Arabic)"] = (arabic_range - persian_specific) / total_alpha
    elif arabic_range > persian_specific and arabic_range > latin_range:
        scores["عربی (Arabic)"] = arabic_range / total_alpha

    # Turkish detection (Latin + special chars)
    turkish_chars = sum(1 for c in content if c in "çğıöşüÇĞİÖŞÜ")
    if turkish_chars > 0 and latin_range > total_alpha * 0.5:
        scores["ترکی (Turkish)"] = (latin_range + turkish_chars) / total_alpha

    # Sort by score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    detected = ranked[0][0] if ranked[0][1] > 0.1 else "نامشخص"

    lines = [f"🌐 تشخیص زبان ({len(content)} کاراکتر):"]
    lines.append(f"  زبان: {detected}")
    lines.append(f"  امتیازها:")
    for lang, score in ranked[:5]:
        if score > 0.01:
            bar = "█" * int(score * 30)
            lines.append(f"    {lang:25s} {bar} ({score:.0%})")

    return "\n".join(lines)


@risk(Risk.SAFE)
def data_analyze(*, path: str, operation: str, column: str = "",
                 query_str: str = "", group_column: str = "",
                 limit: int = 50, context: ActionContext) -> str:
    """Analyze data files (CSV, JSON, Excel) with pandas."""
    try:
        import pandas as pd
    except ImportError:
        raise DependencyMissing(
            "pandas is not installed",
            install_hint="pip install pandas openpyxl",
        )

    from pathlib import Path
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = context.work_dir / p
    if not p.is_file():
        raise AssistantError(f"فایل پیدا نشد: {p}")

    # Load data
    suffix = p.suffix.lower()
    try:
        if suffix == ".csv":
            df = pd.read_csv(str(p))
        elif suffix == ".json":
            df = pd.read_json(str(p))
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(str(p))
        elif suffix == ".parquet":
            df = pd.read_parquet(str(p))
        else:
            raise AssistantError(f"فرمت {suffix} پشتیبانی نمی‌شود. CSV/JSON/Excel/Parquet مجاز است.")
    except Exception as exc:
        raise AssistantError(f"خواندن فایل ناموفق بود: {exc}")

    op = str(operation).lower().strip()
    lim = max(1, min(int(limit or 50), 500))

    if op == "describe":
        desc = df.describe(include="all")
        return f"📊 آمار توصیفی ({len(df)} ردیف × {len(df.columns)} ستون):\n\n{desc.to_string()}"

    if op == "head":
        return f"📊 {lim} ردیف اول ({len(df)} ردیف × {len(df.columns)} ستون):\n\n{df.head(lim).to_string()}"

    if op == "info":
        lines = [f"📊 اطلاعات فایل ({len(df)} ردیف × {len(df.columns)} ستون):"]
        lines.append(f"  فایل: {p.name}")
        for col in df.columns:
            dtype = str(df[col].dtype)
            nulls = int(df[col].isnull().sum())
            unique = int(df[col].nunique())
            lines.append(f"  • {col} ({dtype}) — {unique} مقدار یکتا، {nulls} خالی")
        return "\n".join(lines)

    if op == "value_counts":
        col = str(column).strip()
        if col not in df.columns:
            raise AssistantError(f"ستون «{col}» وجود ندارد. ستون‌ها: {', '.join(df.columns)}")
        vc = df[col].value_counts().head(lim)
        return f"📊 توزیع «{col}»:\n\n{vc.to_string()}"

    if op == "groupby":
        gcol = str(group_column or column).strip()
        if gcol not in df.columns:
            raise AssistantError(f"ستون «{gcol}» وجود ندارد.")
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if not numeric_cols:
            grouped = df.groupby(gcol).size().sort_values(ascending=False).head(lim)
            return f"📊 GroupBy «{gcol}» (تعداد):\n\n{grouped.to_string()}"
        grouped = df.groupby(gcol)[numeric_cols[:3]].mean().head(lim)
        return f"📊 GroupBy «{gcol}» (میانگین):\n\n{grouped.to_string()}"

    if op == "query":
        q = str(query_str).strip()
        if not q:
            raise AssistantError("query_str خالی است.")
        try:
            result = df.query(q)
        except Exception as exc:
            raise AssistantError(f"خطای query: {exc}")
        return f"📊 نتیجه query ({len(result)} ردیف از {len(df)}):\n\n{result.head(lim).to_string()}"

    if op == "correlation":
        numeric = df.select_dtypes(include="number")
        if len(numeric.columns) < 2:
            return "حداقل ۲ ستون عددی لازم است."
        corr = numeric.corr()
        return f"📊 ماتریس همبستگی:\n\n{corr.to_string()}"

    if op == "plot":
        col = str(column).strip()
        if not col or col not in df.columns:
            return "ستون مشخص کنید (parameter: column)."
        data = df[col].dropna()
        if not pd.api.types.is_numeric_dtype(data):
            return f"ستون «{col}» عددی نیست."

        # Try matplotlib PNG first, fall back to text histogram
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(1, 3, figsize=(15, 4))

            # Histogram
            axes[0].hist(data, bins=min(30, max(10, len(data) // 5)),
                         color="#6c63ff", alpha=0.7, edgecolor="white")
            axes[0].set_title(f"Histogram: {col}")
            axes[0].axvline(data.mean(), color="red", linestyle="--", label=f"Mean: {data.mean():.2f}")
            axes[0].legend()

            # Box plot
            axes[1].boxplot(data, vert=True)
            axes[1].set_title(f"Box Plot: {col}")

            # Line/index plot
            axes[2].plot(data.values, color="#6c63ff", linewidth=0.8)
            axes[2].set_title(f"Values: {col}")
            axes[2].set_xlabel("Index")

            plt.tight_layout()
            out_path = p.parent / f"{p.stem}_plot_{col}.png"
            counter = 1
            while out_path.exists():
                out_path = p.parent / f"{p.stem}_plot_{col}_{counter}.png"
                counter += 1
            fig.savefig(str(out_path), dpi=120, bbox_inches="tight")
            plt.close(fig)

            return (
                f"📊 نمودار ذخیره شد: {out_path}\n"
                f"  ستون: {col} ({len(data)} مقدار)\n"
                f"  میانگین: {data.mean():.2f} | میانه: {data.median():.2f}\n"
                f"  انحراف معیار: {data.std():.2f} | min: {data.min():.2f} | max: {data.max():.2f}"
            )
        except ImportError:
            # Fallback: text-based histogram
            import numpy as np
            hist, edges = np.histogram(data, bins=20)
            max_bar = max(hist) if len(hist) > 0 else 1
            lines = [f"📊 هیستوگرام «{col}» ({len(data)} مقدار) — matplotlib نصب نیست:"]
            for i, count in enumerate(hist):
                bar = "█" * int(count / max_bar * 40)
                lines.append(f"  {edges[i]:>10.1f}-{edges[i+1]:>10.1f} | {bar} ({count})")
            lines.append(f"\n  میانگین: {data.mean():.2f} | میانه: {data.median():.2f} | انحراف: {data.std():.2f}")
            return "\n".join(lines)

    raise AssistantError(f"operation نامعتبر: {op}")
