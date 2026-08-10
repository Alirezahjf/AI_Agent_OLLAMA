"""Universal analytics engine for people, chats, and emails.

Analyzes patterns in:
  * Telegram chats (private, groups, channels)
  * Gmail conversations
  * Any text-based communication data

Produces structured reports with:
  * Activity patterns (time of day, day of week)
  * Top contacts / most active members
  * Keyword/topic analysis
  * Response time analysis
  * Sentiment hints
  * Engagement scoring
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class PersonProfile:
    """Analytics profile for a single person."""
    name: str
    user_id: str | int = ""
    username: str = ""
    message_count: int = 0
    total_chars: int = 0
    avg_message_length: float = 0
    first_seen: str = ""
    last_seen: str = ""
    active_hours: dict[int, int] = field(default_factory=dict)
    active_days: dict[str, int] = field(default_factory=dict)
    top_words: list[tuple[str, int]] = field(default_factory=list)
    reply_count: int = 0
    media_count: int = 0
    avg_response_time_minutes: float = 0
    emoji_usage: int = 0
    question_count: int = 0
    is_outgoing_ratio: float = 0  # how much YOU message them vs they message you

    def to_dict(self) -> dict[str, Any]:
        peak_hour = max(self.active_hours, key=self.active_hours.get) if self.active_hours else 0
        peak_day = max(self.active_days, key=self.active_days.get) if self.active_days else "?"
        return {
            "name": self.name,
            "user_id": self.user_id,
            "username": self.username,
            "message_count": self.message_count,
            "total_chars": self.total_chars,
            "avg_message_length": round(self.avg_message_length, 1),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "peak_hour": f"{peak_hour}:00",
            "peak_day": peak_day,
            "top_words": self.top_words[:10],
            "reply_count": self.reply_count,
            "media_count": self.media_count,
            "avg_response_time_min": round(self.avg_response_time_minutes, 1),
            "emoji_usage": self.emoji_usage,
            "question_count": self.question_count,
            "outgoing_ratio": round(self.is_outgoing_ratio, 2),
        }


@dataclass
class ChatAnalytics:
    """Analytics for a single chat/group/channel."""
    chat_name: str
    chat_id: str | int = ""
    chat_type: str = ""  # private, group, channel
    total_messages: int = 0
    date_range: str = ""
    top_members: list[PersonProfile] = field(default_factory=list)
    hourly_distribution: dict[int, int] = field(default_factory=dict)
    daily_distribution: dict[str, int] = field(default_factory=dict)
    top_topics: list[tuple[str, int]] = field(default_factory=list)
    avg_messages_per_day: float = 0
    most_active_day: str = ""
    most_active_hour: int = 0
    total_media: int = 0
    total_links: int = 0
    total_questions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_name": self.chat_name,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "total_messages": self.total_messages,
            "date_range": self.date_range,
            "top_members": [m.to_dict() for m in self.top_members[:15]],
            "hourly_distribution": dict(sorted(self.hourly_distribution.items())),
            "daily_distribution": dict(sorted(self.daily_distribution.items())),
            "top_topics": self.top_topics[:15],
            "avg_messages_per_day": round(self.avg_messages_per_day, 1),
            "most_active_day": self.most_active_day,
            "most_active_hour": self.most_active_hour,
            "total_media": self.total_media,
            "total_links": self.total_links,
            "total_questions": self.total_questions,
        }


# ===========================================================================
# Persian / common stop words
# ===========================================================================

_PERSIAN_STOP = {
    "من", "تو", "او", "ما", "شما", "آن‌ها", "این", "آن", "که", "چه",
    "را", "از", "به", "با", "در", "بر", "برای", "تا", "هم", "خیلی",
    "یه", "یک", "هیچ", "هر", "ولی", "اما", "یا", "و", "اگه", "اگر",
    "باشه", "باش", "بود", "هست", "نیست", "بوده", "شده", "شه", "می",
    "نه", "آره", "اره", "بله", "خوب", "خب", "یعنی", "پس", "دیگه",
    "چیز", "چیزی", "کجا", "کی", "چطور", "چرا", "اینجا", "اونجا",
    "الان", "بعد", "قبل", "بالا", "پایین", "همه", "بعضی",
}

_ENGLISH_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "nor", "not", "so", "yet", "both", "either", "neither", "each",
    "every", "all", "any", "few", "more", "most", "other", "some",
    "such", "no", "only", "own", "same", "than", "too", "very",
    "just", "because", "if", "when", "while", "that", "this", "it",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "what", "which", "who", "how", "where", "there", "here",
}

_STOP_WORDS = _PERSIAN_STOP | _ENGLISH_STOP

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U00002700-\U000027BF\U00002300-\U000023FF]",
    flags=re.UNICODE,
)

_URL_RE = re.compile(r"https?://\S+")
_QUESTION_RE = re.compile(r"[?؟]")


# ===========================================================================
# Core analytics engine
# ===========================================================================


def _tokenize(text: str) -> list[str]:
    """Split text into meaningful words, filtering stop words."""
    # Remove URLs
    text = _URL_RE.sub("", text)
    # Split on whitespace and punctuation
    tokens = re.findall(r"[\w\u0600-\u06FF]+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


def _count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def _has_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(text))


_DAY_NAMES_FA = {
    0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنج‌شنبه",
    4: "جمعه", 5: "شنبه", 6: "یکشنبه",
}


def _day_name(dt: datetime) -> str:
    return _DAY_NAMES_FA.get(dt.weekday(), str(dt.weekday()))


def analyze_messages(
    messages: list[dict[str, Any]],
    *,
    chat_name: str = "",
    chat_id: str | int = "",
    chat_type: str = "",
    my_user_id: str | int = "",
) -> ChatAnalytics:
    """Analyze a list of messages and produce a ChatAnalytics report.

    Each message dict should have:
      - id, text, date (ISO string), sender (name or id),
        sender_id (optional), is_outgoing (bool), media_type (str)
    """
    if not messages:
        return ChatAnalytics(chat_name=chat_name, chat_id=chat_id, chat_type=chat_type)

    # Parse dates
    parsed: list[dict[str, Any]] = []
    for m in messages:
        date_str = str(m.get("date", ""))
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00").replace("+00:00", ""))
        except (ValueError, TypeError):
            try:
                dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                dt = None
        parsed.append({**m, "_dt": dt})

    dates = [p["_dt"] for p in parsed if p["_dt"]]
    first_date = min(dates).strftime("%Y-%m-%d") if dates else "?"
    last_date = max(dates).strftime("%Y-%m-%d") if dates else "?"
    day_count = max(1, (max(dates) - min(dates)).days + 1) if len(dates) > 1 else 1

    # Per-sender tracking
    sender_data: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "name": "", "user_id": "", "username": "",
        "messages": [], "texts": [], "hours": Counter(),
        "days": Counter(), "words": Counter(), "replies": 0,
        "media": 0, "emojis": 0, "questions": 0, "outgoing": 0,
    })

    # Global counters
    global_hours: Counter = Counter()
    global_days: Counter = Counter()
    global_words: Counter = Counter()
    total_media = 0
    total_links = 0
    total_questions = 0

    # Response time tracking: last message time per sender
    last_msg_time: dict[str, datetime] = {}
    response_times: list[float] = []

    for msg in parsed:
        sender = str(msg.get("sender", "?"))
        sender_id = msg.get("sender_id", sender)
        text = str(msg.get("text", ""))
        dt: datetime | None = msg.get("_dt")
        is_out = bool(msg.get("is_outgoing", False))
        media = str(msg.get("media_type", ""))
        is_reply = bool(msg.get("is_reply", False))

        sd = sender_data[str(sender_id)]
        sd["name"] = sender
        sd["user_id"] = sender_id
        sd["username"] = msg.get("username", sd.get("username", ""))
        sd["messages"].append(msg)
        sd["texts"].append(text)

        if dt:
            hour = dt.hour
            day = _day_name(dt)
            sd["hours"][hour] += 1
            sd["days"][day] += 1
            global_hours[hour] += 1
            global_days[day] += 1

            # Response time: time between this message and the previous one
            # from a DIFFERENT sender
            for other_id, other_time in last_msg_time.items():
                if other_id != str(sender_id):
                    diff = (dt - other_time).total_seconds() / 60
                    if 0 < diff < 1440:  # within 24 hours
                        response_times.append(diff)
                        break

            last_msg_time[str(sender_id)] = dt

        if media and media != "text":
            sd["media"] += 1
            total_media += 1

        urls = _URL_RE.findall(text)
        total_links += len(urls)

        if _has_question(text):
            sd["questions"] += 1
            total_questions += 1

        sd["emojis"] += _count_emojis(text)

        if is_reply:
            sd["replies"] += 1

        if is_out:
            sd["outgoing"] += 1

        # Tokenize
        tokens = _tokenize(text)
        for t in tokens:
            sd["words"][t] += 1
            global_words[t] += 1

    # Build person profiles
    profiles: list[PersonProfile] = []
    my_id_str = str(my_user_id) if my_user_id else ""
    my_count = 0

    for sid, sd in sender_data.items():
        texts = sd["texts"]
        total_chars = sum(len(t) for t in texts)
        msg_count = len(sd["messages"])
        if my_id_str and sid == my_id_str:
            my_count = msg_count

        profile = PersonProfile(
            name=sd["name"],
            user_id=sd["user_id"],
            username=sd.get("username", ""),
            message_count=msg_count,
            total_chars=total_chars,
            avg_message_length=total_chars / max(msg_count, 1),
            first_seen=first_date,
            last_seen=last_date,
            active_hours=dict(sd["hours"]),
            active_days=dict(sd["days"]),
            top_words=sd["words"].most_common(15),
            reply_count=sd["replies"],
            media_count=sd["media"],
            emoji_usage=sd["emojis"],
            question_count=sd["questions"],
        )
        profiles.append(profile)

    # Sort by message count
    profiles.sort(key=lambda p: p.message_count, reverse=True)

    # Calculate outgoing ratio for each person
    for p in profiles:
        if my_count > 0 and str(p.user_id) != my_id_str:
            p.is_outgoing_ratio = my_count / max(p.message_count, 1)

    # Average response time
    avg_response = sum(response_times) / len(response_times) if response_times else 0
    for p in profiles:
        p.avg_response_time_minutes = avg_response  # simplified global avg

    # Peak activity
    most_active_hour = global_hours.most_common(1)[0][0] if global_hours else 0
    most_active_day = global_days.most_common(1)[0][0] if global_days else "?"

    return ChatAnalytics(
        chat_name=chat_name,
        chat_id=chat_id,
        chat_type=chat_type,
        total_messages=len(messages),
        date_range=f"{first_date} تا {last_date} ({day_count} روز)",
        top_members=profiles,
        hourly_distribution=dict(global_hours),
        daily_distribution=dict(global_days),
        top_topics=global_words.most_common(20),
        avg_messages_per_day=len(messages) / day_count,
        most_active_day=most_active_day,
        most_active_hour=most_active_hour,
        total_media=total_media,
        total_links=total_links,
        total_questions=total_questions,
    )


# ===========================================================================
# Report formatters
# ===========================================================================


def format_chat_report(analytics: ChatAnalytics) -> str:
    """Format a ChatAnalytics into a readable Persian report."""
    if analytics.total_messages == 0:
        return f"📊 تحلیلی برای «{analytics.chat_name}» موجود نیست (پیامی نیست)."

    lines = [
        f"📊 تحلیل «{analytics.chat_name}» ({analytics.chat_type})",
        f"  تعداد پیام: {analytics.total_messages:,}",
        f"  بازه: {analytics.date_range}",
        f"  میانگین روزانه: {analytics.avg_messages_per_day:.1f} پیام",
        f"  فعال‌ترین ساعت: {analytics.most_active_hour}:00",
        f"  فعال‌ترین روز: {analytics.most_active_day}",
        f"  رسانه‌ها: {analytics.total_media} | لینک‌ها: {analytics.total_links} | سؤالات: {analytics.total_questions}",
    ]

    # Top members
    if analytics.top_members:
        lines.append(f"\n  👥 فعال‌ترین اعضا ({len(analytics.top_members)} نفر):")
        for i, m in enumerate(analytics.top_members[:10], 1):
            peak_h = max(m.active_hours, key=m.active_hours.get) if m.active_hours else "?"
            peak_d = max(m.active_days, key=m.active_days.get) if m.active_days else "?"
            lines.append(
                f"    {i}. {m.name} — {m.message_count} پیام "
                f"(میانگین {m.avg_message_length:.0f} حرف، "
                f"فعال‌ترین: {peak_d} {peak_h}:00)"
            )
            if m.top_words:
                words = ", ".join(w for w, _ in m.top_words[:5])
                lines.append(f"       کلمات پرتکرار: {words}")
            if m.emoji_usage:
                lines.append(f"       ایموجی: {m.emoji_usage} | سؤال: {m.question_count} | ریپلای: {m.reply_count}")

    # Top topics
    if analytics.top_topics:
        lines.append(f"\n  📌 موضوعات پرتکرار:")
        for word, count in analytics.top_topics[:10]:
            bar = "█" * min(count, 20)
            lines.append(f"    {word:15s} {bar} ({count})")

    # Hourly distribution (compact bar chart)
    if analytics.hourly_distribution:
        lines.append(f"\n  ⏰ توزیع ساعتی:")
        max_h = max(analytics.hourly_distribution.values()) if analytics.hourly_distribution else 1
        for h in range(24):
            count = analytics.hourly_distribution.get(h, 0)
            if count > 0:
                bar_len = int(count / max_h * 20)
                bar = "▓" * bar_len
                lines.append(f"    {h:02d}:00 {bar} ({count})")

    # Daily distribution
    if analytics.daily_distribution:
        lines.append(f"\n  📅 توزیع هفتگی:")
        max_d = max(analytics.daily_distribution.values()) if analytics.daily_distribution else 1
        day_order = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]
        for day in day_order:
            count = analytics.daily_distribution.get(day, 0)
            if count > 0:
                bar_len = int(count / max_d * 20)
                bar = "▓" * bar_len
                lines.append(f"    {day:8s} {bar} ({count})")

    return "\n".join(lines)


def format_person_report(profile: PersonProfile) -> str:
    """Format a single person's profile into a readable report."""
    peak_h = max(profile.active_hours, key=profile.active_hours.get) if profile.active_hours else "?"
    peak_d = max(profile.active_days, key=profile.active_days.get) if profile.active_days else "?"

    lines = [
        f"👤 پروفایل: {profile.name}",
    ]
    if profile.username:
        lines.append(f"  @{profile.username}")
    if profile.user_id:
        lines.append(f"  ID: {profile.user_id}")
    lines.extend([
        f"  تعداد پیام: {profile.message_count:,}",
        f"  مجموع کاراکتر: {profile.total_chars:,}",
        f"  میانگین طول پیام: {profile.avg_message_length:.0f} حرف",
        f"  بازه: {profile.first_seen} تا {profile.last_seen}",
        f"  فعال‌ترین زمان: {peak_d} ساعت {peak_h}:00",
        f"  ایموجی: {profile.emoji_usage}",
        f"  سؤالات: {profile.question_count}",
        f"  ریپلای‌ها: {profile.reply_count}",
        f"  رسانه‌ها: {profile.media_count}",
        f"  نسبت پیام شما به ایشان: {profile.is_outgoing_ratio:.1f}x",
    ])

    if profile.top_words:
        words = ", ".join(f"{w}({c})" for w, c in profile.top_words[:10])
        lines.append(f"  کلمات پرتکرار: {words}")

    return "\n".join(lines)
