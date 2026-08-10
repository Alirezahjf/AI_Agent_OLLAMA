"""Skill management actions — control the Skill System from chat.

These actions let the user (and the LLM) activate/deactivate skills,
set per-skill models, view status, and get suggestions.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_skills(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="skill_status",
        description=(
            "وضعیت Skill System: لیست skill های فعال و غیرفعال "
            "با تعداد ابزارهای هرکدام. SAFE."
        ),
        parameters={},
    )(skill_status)

    registry.decorator(
        name="skill_activate",
        description="فعال کردن یک skill (ابزارهایش در دسترس LLM قرار می‌گیرند). SAFE.",
        parameters={
            "skill_id": {"type": "string", "description": "شناسه skill (مثلاً github, telegram, system)"},
        },
        required=("skill_id",),
    )(skill_activate)

    registry.decorator(
        name="skill_deactivate",
        description="غیرفعال کردن یک skill (ابزارهایش از context LLM حذف می‌شوند). SAFE.",
        parameters={
            "skill_id": {"type": "string"},
        },
        required=("skill_id",),
    )(skill_deactivate)

    registry.decorator(
        name="skill_set_model",
        description=(
            "تنظیم مدل اختصاصی برای یک skill. وقتی فعال باشد، درخواست‌های "
            "مربوط به این skill از مدل مشخص‌شده استفاده می‌کنند. SAFE."
        ),
        parameters={
            "skill_id": {"type": "string"},
            "model": {"type": "string", "description": "نام مدل (مثلاً claude-sonnet-5, gpt-5.6-sol)"},
        },
        required=("skill_id", "model"),
    )(skill_set_model)

    registry.decorator(
        name="skill_set_prompt",
        description="تنظیم system prompt اختصاصی برای یک skill. SAFE.",
        parameters={
            "skill_id": {"type": "string"},
            "prompt": {"type": "string"},
        },
        required=("skill_id", "prompt"),
    )(skill_set_prompt)

    registry.decorator(
        name="skill_list",
        description="لیست کامل همه skill ها با توضیحات و وضعیت. SAFE.",
        parameters={},
    )(skill_list)

    registry.decorator(
        name="skill_suggest",
        description="پیشنهاد skill های غیرفعال بر اساس پیام کاربر. SAFE.",
        parameters={
            "message": {"type": "string", "description": "پیام برای تحلیل"},
        },
        required=("message",),
    )(skill_suggest)


def _skill_manager(context: ActionContext):
    mgr = context.extra.get("skill_manager")
    if mgr is None:
        raise AssistantError("Skill System فعال نیست.")
    return mgr


@risk(Risk.SAFE)
def skill_status(*, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    return mgr.status_text()


@risk(Risk.SAFE)
def skill_activate(*, skill_id: str, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    sid = str(skill_id).strip().lower()
    skill = mgr.get_skill(sid)
    if skill is None:
        available = ", ".join(s.id for s in mgr.list_skills())
        raise AssistantError(f"Skill «{sid}» یافت نشد. موجودها: {available}")
    mgr.activate(sid)
    return (
        f"✅ Skill «{skill.icon} {skill.name}» فعال شد.\n"
        f"  {len(skill.actions)} ابزار در دسترس قرار گرفت.\n"
        f"  {skill.description}"
    )


@risk(Risk.SAFE)
def skill_deactivate(*, skill_id: str, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    sid = str(skill_id).strip().lower()
    skill = mgr.get_skill(sid)
    if skill is None:
        raise AssistantError(f"Skill «{sid}» یافت نشد.")
    mgr.deactivate(sid)
    return f"⏸️ Skill «{skill.icon} {skill.name}» غیرفعال شد. ابزارهایش از context حذف شدند."


@risk(Risk.SAFE)
def skill_set_model(*, skill_id: str, model: str, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    sid = str(skill_id).strip().lower()
    skill = mgr.get_skill(sid)
    if skill is None:
        raise AssistantError(f"Skill «{sid}» یافت نشد.")
    mgr.set_model(sid, model)
    return f"✅ مدل «{model}» برای skill «{skill.name}» تنظیم شد."


@risk(Risk.SAFE)
def skill_set_prompt(*, skill_id: str, prompt: str, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    sid = str(skill_id).strip().lower()
    skill = mgr.get_skill(sid)
    if skill is None:
        raise AssistantError(f"Skill «{sid}» یافت نشد.")
    mgr.set_prompt(sid, prompt)
    return f"✅ System prompt skill «{skill.name}» به‌روزرسانی شد ({len(prompt)} کاراکتر)."


@risk(Risk.SAFE)
def skill_list(*, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    skills = mgr.list_skills()
    lines = [f"🧠 Skill System ({len(skills)} skill):\n"]

    categories: dict[str, list] = {}
    for s in skills:
        categories.setdefault(s.category, []).append(s)

    for cat, items in sorted(categories.items()):
        lines.append(f"  [{cat}]")
        for s in items:
            status = "✅" if s.is_active else "⏸️"
            model = f" 🔧{s.model_override}" if s.model_override else ""
            lines.append(
                f"    {status} {s.icon} {s.id:20s} — {s.name}{model}\n"
                f"       {s.description} ({len(s.actions)} ابزار)"
            )
        lines.append("")

    lines.append(
        "راهنما: skill_activate / skill_deactivate / skill_set_model / skill_set_prompt"
    )
    return "\n".join(lines)


@risk(Risk.SAFE)
def skill_suggest(*, message: str, context: ActionContext) -> str:
    mgr = _skill_manager(context)
    suggestions = mgr.suggest_skills(str(message))
    if not suggestions:
        return "هیچ skill غیرفعالی برای این پیام پیشنهاد نمی‌شود."
    lines = ["💡 Skill های پیشنهادی برای فعال‌سازی:"]
    for sid in suggestions:
        skill = mgr.get_skill(sid)
        if skill:
            lines.append(f"  {skill.icon} {skill.name} — {skill.description}")
    lines.append("\nبرای فعال‌سازی: skill_activate(skill_id)")
    return "\n".join(lines)
