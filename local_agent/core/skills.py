"""Skill System — persistent, model-aware capability bundles.

A **Skill** is a self-contained capability package that:

  1. Groups related actions together (e.g. ``github`` = all github.* actions)
  2. Can be **activated/deactivated** by the user — inactive skills don't
     consume LLM context and their actions are hidden from the model
  3. Stores a **per-skill system prompt fragment** that is injected into
     the main prompt only when the skill is active — this means the LLM
     doesn't have to re-learn how to use a tool every chat session
  4. Supports a **per-skill model override** — e.g. use Claude for coding
     tasks, GPT-4 for writing, a local model for simple lookups
  5. Persists its state (active/inactive, model override, custom prompt)
     in ``<data_dir>/skills.json`` so settings survive restarts

This is inspired by:
  * Claude's "Projects" (bundled context + tools)
  * ChatGPT's "GPTs" (custom instructions + capabilities)
  * Cursor's ".cursorrules" (project-specific AI instructions)

The key insight: **when a skill is active, the LLM already knows how to
use it** — no more explaining "use github.list_repos to see your repos"
in every chat.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger("skills")


@dataclass
class Skill:
    """A single capability bundle."""

    id: str
    name: str
    description: str
    actions: list[str] = field(default_factory=list)
    system_prompt: str = ""
    is_active: bool = True
    model_override: str = ""
    icon: str = ""
    category: str = "general"
    # Keywords that trigger auto-activation suggestions
    trigger_keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "actions": self.actions,
            "system_prompt": self.system_prompt,
            "is_active": self.is_active,
            "model_override": self.model_override,
            "icon": self.icon,
            "category": self.category,
            "trigger_keywords": self.trigger_keywords,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Skill:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            actions=list(data.get("actions", [])),
            system_prompt=str(data.get("system_prompt", "")),
            is_active=bool(data.get("is_active", True)),
            model_override=str(data.get("model_override", "")),
            icon=str(data.get("icon", "")),
            category=str(data.get("category", "general")),
            trigger_keywords=list(data.get("trigger_keywords", [])),
        )


# ===========================================================================
# Default skill definitions
# ===========================================================================


DEFAULT_SKILLS: list[dict[str, Any]] = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "مدیریت کامل repos، issues، PRs، branches و releases در GitHub.",
        "icon": "🐙",
        "category": "integrations",
        "actions": [
            "github.status", "github.list_repos", "github.get_repo",
            "github.list_issues", "github.get_issue", "github.create_issue",
            "github.close_issue", "github.reopen_issue", "github.add_comment",
            "github.add_labels", "github.assign_issue", "github.list_prs",
            "github.get_pr", "github.create_pr", "github.merge_pr",
            "github.close_pr", "github.list_branches", "github.create_branch",
            "github.delete_branch", "github.get_commits", "github.search_code",
            "github.search_issues", "github.list_notifications",
            "github.mark_notifications_read", "github.get_file",
            "github.list_files", "github.list_releases", "github.create_release",
            "github.update_file",
        ],
        "system_prompt": (
            "تو به GitHub کاربر دسترسی داری. از ابزارهای github.* برای مدیریت پروژه استفاده کن.\n"
            "- برای پیدا کردن repo از github.list_repos استفاده کن (sort=updated)\n"
            "- repo همیشه به فرمت owner/name است (مثلاً Alirezahjf/AI_Agent_OLLAMA)\n"
            "- قبل از ساخت PR، ابتدا branch بساز و commits را بررسی کن\n"
            "- برای جست‌وجوی کد از github.search_code استفاده کن\n"
            "- اعلان‌ها را با github.list_notifications بررسی کن\n"
            "- قبل از merge PR، حتماً github.get_pr را برای بررسی diff اجرا کن"
        ),
        "trigger_keywords": ["github", "repo", "issue", "pull request", "PR", "commit", "branch", "release", "گیت‌هاب"],
    },
    {
        "id": "telegram",
        "name": "تلگرام شخصی",
        "description": "کنترل اکانت شخصی تلگرام: چت‌ها، پیام‌ها، مخاطبین، ارسال.",
        "icon": "✈️",
        "category": "messaging",
        "actions": [
            "telegram.list_accounts", "telegram.switch_account",
            "telegram.list_chats", "telegram.search_messages",
            "telegram.get_me", "telegram.search_contacts",
            "telegram.get_chat_history", "telegram.get_profile",
            "telegram.download_media", "telegram.mark_read",
            "telegram.resolve_username", "telegram.send_message",
            "telegram.send_photo", "telegram.send_file",
            "telegram.send_video", "telegram.send_voice",
            "telegram.send_audio", "telegram.send_document",
            "telegram.send_sticker", "telegram.send_animation",
            "telegram.send_location", "telegram.reply_to",
            "telegram.forward_message", "telegram.delete_message",
            "telegram.edit_message", "telegram.list_contacts",
            "telegram.get_contact_info", "telegram.add_contact",
            "telegram.delete_contact", "telegram.block_user",
            "telegram.unblock_user", "telegram.join_channel",
            "telegram.leave_channel", "telegram.list_members",
            "telegram.list_admins", "telegram.update_profile",
            "telegram.update_username", "telegram.set_profile_photo",
            "telegram.set_online_status", "telegram.statistics",
        ],
        "system_prompt": (
            "تو به اکانت شخصی تلگرام کاربر دسترسی داری.\n"
            "- برای پیدا کردن چت خصوصی: telegram.list_chats با kind='private' و query\n"
            "- اگر نام چت پیدا نشد: telegram.search_contacts و سپس از ID استفاده کن\n"
            "- قبل از ارسال پیام، حتماً تأیید بگیر (DESTRUCTIVE)\n"
            "- برای پیدا کردن افراد: search_contacts (جست‌وجو در مخاطبین + چت‌های خصوصی)\n"
            "- برای آمار کلی: telegram.statistics\n"
            "- Saved Messages: chat='خودم' یا chat='saved'"
        ),
        "trigger_keywords": ["تلگرام", "telegram", "چت", "پیام", "مخاطب", "ارسال پیام"],
    },
    {
        "id": "email",
        "name": "ایمیل (Gmail)",
        "description": "خواندن و ارسال ایمیل از Gmail.",
        "icon": "📧",
        "category": "messaging",
        "actions": [
            "gmail.list_unread", "gmail.search", "gmail.read",
            "gmail.send", "gmail.reply", "gmail.download_attachment",
        ],
        "system_prompt": (
            "تو به Gmail کاربر دسترسی داری.\n"
            "- آدرس ایمیل را خام بنویس (بدون Markdown link)\n"
            "- HTML خودکار multipart/alternative می‌شود\n"
            "- قبل از ارسال، حتماً تأیید بگیر"
        ),
        "trigger_keywords": ["ایمیل", "email", "gmail", "جیمیل"],
    },
    {
        "id": "calendar",
        "name": "تقویم (Google Calendar)",
        "description": "مدیریت رویدادها و قرارها.",
        "icon": "📅",
        "category": "productivity",
        "actions": [
            "calendar.status", "calendar.list_events", "calendar.get_event",
            "calendar.create_event", "calendar.delete_event",
            "calendar.list_calendars",
        ],
        "system_prompt": (
            "تو به Google Calendar کاربر دسترسی داری.\n"
            "- تاریخ‌ها را به فرمت ISO 8601 بنویس: 2026-08-15T10:00:00\n"
            "- timezone پیش‌فرض: Asia/Tehran\n"
            "- برای رویداد تمام روز، فقط تاریخ: 2026-08-15\n"
            "- ابتدا calendar.list_events برای بررسی قبل از ساخت"
        ),
        "trigger_keywords": ["تقویم", "calendar", "رویداد", "قرار", "جلسه", "event"],
    },
    {
        "id": "system",
        "name": "مانیتورینگ سیستم",
        "description": "آمار CPU، RAM، Disk، Network و پروسس‌ها.",
        "icon": "📊",
        "category": "system",
        "actions": [
            "system_monitor", "process_list", "disk_usage", "network_stats",
        ],
        "system_prompt": (
            "می‌توانی آمار لحظه‌ای سیستم را ببینی.\n"
            "- system_monitor با top_processes=5 برای دیدن پروسس‌های پرمصرف\n"
            "- process_list با filter برای پیدا کردن پروسس خاص\n"
            "- disk_usage برای بررسی فضای دیسک"
        ),
        "trigger_keywords": ["سیستم", "cpu", "ram", "دیسک", "disk", "memory", "network"],
    },
    {
        "id": "web_info",
        "name": "اطلاعات و اخبار",
        "description": "آب‌وهوا، ارز، رمزارز، YouTube و RSS.",
        "icon": "🌐",
        "category": "information",
        "actions": [
            "weather", "currency_rate", "crypto_price",
            "youtube_search", "rss_feed", "search_web", "web_fetch",
        ],
        "system_prompt": (
            "می‌توانی اطلاعات لحظه‌ای بگیری:\n"
            "- weather: آب‌وهوا (نام شهر به فارسی یا انگلیسی)\n"
            "- currency_rate: نرخ ارز (from_currency/to_currency)\n"
            "- crypto_price: قیمت رمزارز (btc/eth/sol/...)\n"
            "- youtube_search: جست‌وجوی ویدیو\n"
            "- rss_feed: خواندن RSS feeds"
        ),
        "trigger_keywords": ["آب‌وهوا", "weather", "ارز", "دلار", "رمزارز", "بیت‌کوین", "یوتیوب", "youtube", "اخبار", "rss"],
    },
    {
        "id": "ai_content",
        "name": "هوش مصنوعی و محتوا",
        "description": "ساخت تصویر، OCR، TTS، STT، ترجمه، تحلیل تصویر — همه از AvalAI API.",
        "icon": "🤖",
        "category": "ai",
        "actions": [
            "generate_image", "ocr", "text_to_speech", "speech_to_text",
            "translate", "analyze_image", "list_ai_models",
            "run_code", "pdf_read", "generate_password",
            "db_query", "db_tables",
        ],
        "system_prompt": (
            "ابزارهای AI — همه از AvalAI API (همان کلید و endpoint پروژه):\n"
            "- generate_image: ساخت تصویر (model: dall-e-3, gpt-image-1, flux-pro, qwen-image)\n"
            "- ocr: تشخیص متن از تصویر/PDF (model: mistral-ocr-latest)\n"
            "- text_to_speech: تبدیل متن به صدا (model: tts-1, voice: alloy/nova/...)\n"
            "- speech_to_text: تبدیل صدا به متن (model: whisper-1)\n"
            "- translate: ترجمه با LLM (model: gpt-4o-mini یا پیش‌فرض پروژه)\n"
            "- analyze_image: تحلیل تصویر با Vision (model: gpt-4o, claude-sonnet-5)\n"
            "- list_ai_models: لیست مدل‌های موجود در API\n"
            "- run_code: اجرای Python/JavaScript\n"
            "- pdf_read: خواندن PDF | generate_password: رمز قوی\n"
            "- db_query / db_tables: SQLite read-only"
        ),
        "trigger_keywords": ["تصویر", "image", "عکس", "ocr", "صدا", "speech", "ترجمه", "translate", "کد", "code", "pdf", "رمز", "vision", "تحلیل تصویر", "tts", "stt", "whisper"],
    },
    {
        "id": "database",
        "name": "دیتابیس",
        "description": "اجرای query روی فایل‌های SQLite.",
        "icon": "🗃️",
        "category": "development",
        "actions": ["db_query", "db_tables"],
        "system_prompt": (
            "می‌توانی روی فایل‌های SQLite query اجرا کنی:\n"
            "- ابتدا db_tables برای دیدن ساختار\n"
            "- فقط SELECT مجاز است (read-only)\n"
            "- max_rows برای محدود کردن نتایج"
        ),
        "trigger_keywords": ["دیتابیس", "database", "sql", "sqlite", "query", "جدول"],
    },
    {
        "id": "discord",
        "name": "Discord",
        "description": "مدیریت سرورها و کانال‌های Discord.",
        "icon": "🎮",
        "category": "messaging",
        "actions": [
            "discord.status", "discord.list_guilds", "discord.list_channels",
            "discord.get_messages", "discord.send_message", "discord.delete_message",
        ],
        "system_prompt": (
            "دسترسی به Discord bot:\n"
            "- ابتدا discord.list_guilds برای دیدن سرورها\n"
            "- سپس discord.list_channels با guild_id\n"
            "- channel_id عددی است (از list_channels بگیر)"
        ),
        "trigger_keywords": ["discord", "دیسکورد"],
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "مدیریت کانال‌ها و پیام‌های Slack.",
        "icon": "💬",
        "category": "messaging",
        "actions": [
            "slack.status", "slack.list_channels", "slack.get_messages",
            "slack.send_message",
        ],
        "system_prompt": (
            "دسترسی به Slack:\n"
            "- slack.list_channels برای دیدن کانال‌ها\n"
            "- channel ID از list_channels بگیر"
        ),
        "trigger_keywords": ["slack", "اسلک"],
    },
    {
        "id": "notion",
        "name": "Notion",
        "description": "مدیریت صفحات و دیتابیس‌های Notion.",
        "icon": "📝",
        "category": "productivity",
        "actions": [
            "notion.status", "notion.search", "notion.get_page",
            "notion.create_page", "notion.list_databases",
        ],
        "system_prompt": (
            "دسترسی به Notion:\n"
            "- notion.search برای پیدا کردن صفحات\n"
            "- notion.get_page با page_id برای خواندن\n"
            "- notion.create_page: parent_id می‌تواند page یا database باشد"
        ),
        "trigger_keywords": ["notion", "نوشن"],
    },
    {
        "id": "analytics",
        "name": "تحلیل داده و ارتباطات",
        "description": "تحلیل عمیق چت‌های تلگرام، ایمیل‌ها، افراد و فایل‌های داده (CSV/Excel).",
        "icon": "📈",
        "category": "analysis",
        "actions": [
            "analytics.analyze_chat", "analytics.analyze_person",
            "analytics.analyze_group_members", "analytics.analyze_gmail",
            "analytics.compare_chats", "analytics.data_analyze",
        ],
        "system_prompt": (
            "ابزارهای تحلیل داده و ارتباطات:\n"
            "- analytics.analyze_chat: تحلیل کامل یک چت (فعالیت، ساعات اوج، موضوعات)\n"
            "- analytics.analyze_person: پروفایل یک شخص (کلمات، ساعات، سبک)\n"
            "- analytics.analyze_group_members: رتبه‌بندی اعضای گروه\n"
            "- analytics.analyze_gmail: تحلیل ایمیل‌ها (فرستنده‌ها، موضوعات، ساعات)\n"
            "- analytics.compare_chats: مقایسه فعالیت بین چند چت\n"
            "- analytics.data_analyze: تحلیل فایل CSV/Excel (describe/head/info/value_counts/groupby/query/correlation/plot)\n"
            "- limit بالاتر = تحلیل دقیق‌تر (حداکثر 5000 پیام)"
        ),
        "trigger_keywords": ["تحلیل", "آنالیز", "analyze", "analytics", "آمار", "گزارش", "report", "data", "داده"],
    },
    {
        "id": "smart_home",
        "name": "خانه هوشمند",
        "description": "کنترل Home Assistant devices.",
        "icon": "🏠",
        "category": "iot",
        "actions": [
            "hass_status", "hass_list_entities", "hass_get_state",
            "hass_call_service", "push_notification", "pushbullet_send",
        ],
        "system_prompt": (
            "دسترسی به Home Assistant:\n"
            "- hass_list_entities با domain filter (light/switch/sensor)\n"
            "- hass_call_service: domain=light, service=turn_on, entity_id=light.bedroom\n"
            "- push_notification برای ارسال نوتیف به گوشی"
        ),
        "trigger_keywords": ["خانه هوشمند", "home assistant", "چراغ", "light", "switch", "smart"],
    },
]


# ===========================================================================
# Skill Manager
# ===========================================================================


class SkillManager:
    """Manages skills: activation, persistence, prompt injection."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._skills_file = data_dir / "skills.json"
        self._skills: dict[str, Skill] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """Load skills from disk or initialise defaults."""
        with self._lock:
            # Start with defaults
            for defn in DEFAULT_SKILLS:
                skill = Skill.from_dict(defn)
                self._skills[skill.id] = skill

            # Override with persisted state
            if self._skills_file.is_file():
                try:
                    data = json.loads(self._skills_file.read_text(encoding="utf-8"))
                    for skill_data in data.get("skills", []):
                        sid = skill_data.get("id", "")
                        if sid in self._skills:
                            # Merge persisted state into the default
                            existing = self._skills[sid]
                            existing.is_active = bool(skill_data.get("is_active", existing.is_active))
                            existing.model_override = str(skill_data.get("model_override", ""))
                            # Custom prompt overrides default
                            custom = str(skill_data.get("custom_prompt", ""))
                            if custom:
                                existing.system_prompt = custom
                        else:
                            # User-created custom skill
                            self._skills[sid] = Skill.from_dict(skill_data)
                except Exception as exc:
                    logger.warning("failed to load skills: %s", exc)

    def _save(self) -> None:
        """Persist skill state to disk."""
        with self._lock:
            data = {
                "skills": [
                    {
                        "id": s.id,
                        "is_active": s.is_active,
                        "model_override": s.model_override,
                        "custom_prompt": s.system_prompt if s.system_prompt != self._default_prompt(s.id) else "",
                    }
                    for s in self._skills.values()
                ]
            }
            try:
                self._skills_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._skills_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._skills_file)
            except Exception as exc:
                logger.warning("failed to save skills: %s", exc)

    def _default_prompt(self, skill_id: str) -> str:
        for defn in DEFAULT_SKILLS:
            if defn["id"] == skill_id:
                return defn.get("system_prompt", "")
        return ""

    # -------------------------------------------------------- Public API

    def list_skills(self) -> list[Skill]:
        """Return all registered skills."""
        return list(self._skills.values())

    def get_skill(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def activate(self, skill_id: str) -> bool:
        """Activate a skill."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.is_active = True
        self._save()
        logger.info("skill activated: %s", skill_id)
        return True

    def deactivate(self, skill_id: str) -> bool:
        """Deactivate a skill (hide from LLM context)."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.is_active = False
        self._save()
        logger.info("skill deactivated: %s", skill_id)
        return True

    def set_model(self, skill_id: str, model: str) -> bool:
        """Set a per-skill model override."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.model_override = str(model).strip()
        self._save()
        return True

    def set_prompt(self, skill_id: str, prompt: str) -> bool:
        """Override the system prompt for a skill."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.system_prompt = str(prompt)
        self._save()
        return True

    def active_skills(self) -> list[Skill]:
        """Return only active skills."""
        return [s for s in self._skills.values() if s.is_active]

    def active_actions(self) -> set[str]:
        """Return the set of action names from all active skills."""
        actions: set[str] = set()
        for skill in self.active_skills():
            actions.update(skill.actions)
        return actions

    def inactive_actions(self) -> set[str]:
        """Return the set of action names from all inactive skills."""
        active = self.active_actions()
        all_actions: set[str] = set()
        for skill in self._skills.values():
            all_actions.update(skill.actions)
        return all_actions - active

    def should_hide_action(self, action_name: str) -> bool:
        """Check if an action should be hidden (belongs only to inactive skills).

        An action is hidden only when ALL skills that contain it are inactive.
        Actions not belonging to any skill are never hidden.
        """
        belongs_to_active = False
        belongs_to_any = False
        for skill in self._skills.values():
            if action_name in skill.actions:
                belongs_to_any = True
                if skill.is_active:
                    belongs_to_active = True
                    break
        if not belongs_to_any:
            return False  # action doesn't belong to any skill → always visible
        return not belongs_to_active

    def filter_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Filter tool definitions to only include active skill tools.

        Tools not belonging to any skill are always included.
        Tools belonging to inactive skills are excluded.
        """
        return [
            t for t in tools
            if not self.should_hide_action(
                t.get("function", {}).get("name", "")
                if isinstance(t, dict) else ""
            )
        ]

    def get_model_for_skill(self, skill_id: str, default_model: str = "") -> str:
        """Return the model to use for a specific skill.

        Returns the skill's model override if set, otherwise the default.
        """
        skill = self._skills.get(skill_id)
        if skill and skill.model_override:
            return skill.model_override
        return default_model

    def get_model_for_action(self, action_name: str, default_model: str = "") -> str:
        """Return the model to use for a specific action.

        Finds the active skill that owns this action and returns its
        model override, or the default if none is set.
        """
        for skill in self.active_skills():
            if action_name in skill.actions and skill.model_override:
                return skill.model_override
        return default_model

    def detect_skill_for_message(self, user_message: str) -> str | None:
        """Detect which skill is most relevant for a user message.

        Returns the skill_id if a clear match is found, None otherwise.
        Used for model routing: when a message matches a skill with a
        model override, route to that model.
        """
        msg_lower = user_message.lower()
        best_skill: str | None = None
        best_score = 0

        for skill in self.active_skills():
            if not skill.model_override:
                continue
            score = 0
            for keyword in skill.trigger_keywords:
                if keyword.lower() in msg_lower:
                    score += 1
            if score > best_score:
                best_score = score
                best_skill = skill.id

        return best_skill if best_score > 0 else None

    def build_system_prompt_fragment(self) -> str:
        """Build the combined system prompt from all active skills.

        This fragment is appended to the main system prompt so the LLM
        already knows how to use every active tool — no re-learning needed.
        """
        parts: list[str] = []
        for skill in self.active_skills():
            if skill.system_prompt:
                parts.append(f"\n## {skill.icon} {skill.name}\n{skill.system_prompt}")
        return "\n".join(parts)

    def suggest_skills(self, user_message: str) -> list[str]:
        """Suggest skills to activate based on message keywords."""
        msg_lower = user_message.lower()
        suggestions: list[str] = []
        for skill in self._skills.values():
            if skill.is_active:
                continue
            for keyword in skill.trigger_keywords:
                if keyword.lower() in msg_lower:
                    suggestions.append(skill.id)
                    break
        return suggestions

    def status_text(self) -> str:
        """Return a human-readable status of all skills."""
        active = [s for s in self._skills.values() if s.is_active]
        inactive = [s for s in self._skills.values() if not s.is_active]
        lines = [f"🧠 Skill System: {len(active)} فعال از {len(self._skills)}"]
        if active:
            lines.append("\n  فعال:")
            for s in active:
                model = f" (model: {s.model_override})" if s.model_override else ""
                lines.append(f"    {s.icon} {s.name}{model} — {len(s.actions)} ابزار")
        if inactive:
            lines.append("\n  غیرفعال:")
            for s in inactive:
                lines.append(f"    {s.icon} {s.name} — {s.description}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {s.id: s.to_dict() for s in self._skills.values()}
