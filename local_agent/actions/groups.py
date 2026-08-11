"""Stable, user-facing groups for selecting which tools a chat sees."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolGroup:
    id: str
    label: str
    icon: str
    description: str
    default_on: bool
    order: int

TOOL_GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup("files", "فایل و ترمینال", "📁", "خواندن/نوشتن فایل، جست‌وجو، اجرای دستور", True, 1),
    ToolGroup("system", "سیستم و پردازش", "⚙️", "پروسس‌ها، پنجره‌ها، کلیپ‌بورد، اطلاعات سیستم", True, 2),
    ToolGroup("automation", "اتوماسیون و گرافیک", "🖱️", "کلیک/تایپ ماوس‌وکیبورد، اسکرین‌شات، باز کردن برنامه", False, 3),
    ToolGroup("web", "وب", "🌐", "جست‌وجوی وب و اسکرین‌شات صفحه", True, 4),
    ToolGroup("telegram", "تلگرام شخصی", "✈️", "مدیریت اکانت تلگرام (Telethon)", False, 5),
    ToolGroup("gmail", "جیمیل", "📧", "خواندن/ارسال ایمیل", False, 6),
    ToolGroup("github", "گیت‌هاب", "🐙", "clone/commit/push/PR/Issue/Release", False, 7),
    ToolGroup("scheduler", "زمان‌بند", "⏰", "یادآوری و کار زمان‌بندی‌شده", False, 8),
    ToolGroup("config", "تنظیمات", "🔧", "تغییر تنظیمات از داخل چت (config_set)", False, 9),
)
DEFAULT_GROUP_IDS = tuple(g.id for g in TOOL_GROUPS if g.default_on)
_VALID = {g.id for g in TOOL_GROUPS}

def group_by_id(gid: str) -> ToolGroup | None:
    return next((g for g in TOOL_GROUPS if g.id == gid), None)

def infer_group(name: str) -> str:
    n = name.lower()
    for prefix, group in (("telegram.","telegram"),("gmail.","gmail"),("github.","github"),("scheduler.","scheduler"),("config_", "config")):
        if n.startswith(prefix): return group
    if any(x in n for x in ("click", "type", "mouse", "keyboard", "screen_capture", "launch_app")): return "automation"
    if any(x in n for x in ("web.", "browser", "search_web")): return "web"
    if any(x in n for x in ("process", "window", "clipboard", "system", "app_control")): return "system"
    return "files"
