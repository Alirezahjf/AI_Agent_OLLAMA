"""Actions for time-based reminders and scheduled tasks.

``schedule_reminder`` — Safe; shows a notification at the due time.
``schedule_task``    — Destructive; runs a registered action at the due
                       time and streams the result as an event.
``list_scheduled_jobs`` / ``cancel_scheduled_job`` — Safe bookkeeping.

The scheduler itself lives in ``context.extra[\"scheduler\"]`` (owned by
:class:`BridgeHandlers`); the registry is exposed through
``context.extra[\"registry\"]`` so ``schedule_task`` can validate the
action name up front.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_scheduler(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="schedule_reminder",
        description=(
            "ثبت یک یادآوری زمان‌بندی‌شده: سرِ موعد یک اعلان (notification) "
            "به کاربر نشان داده می‌شود. at می‌تواند ISO (مثل 2026-08-06T18:30) "
            "یا «در HH:MM» یا «تا N دقیقه دیگر» باشد. SAFE."
        ),
        parameters={
            "at": {"type": "string", "description": "زمان اجرا (ISO یا «در HH:MM» یا «تا N دقیقه دیگر»)"},
            "message": {"type": "string", "description": "متن یادآوری"},
        },
        required=("at", "message"),
    )(schedule_reminder)

    registry.decorator(
        name="schedule_task",
        description=(
            "زمان‌بندی اجرای یک اکشن مشخص در آینده (مثلاً ارسال پیام یا اجرای "
            "دستور). سرِ موعد، action_name با arguments اجرا می‌شود و نتیجه به‌صورت "
            "رویداد اعلام می‌شود. DESTRUCTIVE — هنگام ثبت تأیید می‌خواهد."
        ),
        parameters={
            "at": {"type": "string", "description": "زمان اجرا (ISO یا «در HH:MM» یا «تا N دقیقه دیگر»)"},
            "action_name": {"type": "string", "description": "نام اکشنِ زمان‌بندی‌شده"},
            "arguments": {"type": "object", "description": "آرگومان‌های اکشن (اختیاری)"},
        },
        required=("at", "action_name"),
        risk_level=Risk.DESTRUCTIVE,
    )(schedule_task)

    registry.decorator(
        name="list_scheduled_jobs",
        description="فهرست کارهای زمان‌بندی‌شده با شناسه، زمان، نوع و وضعیت. SAFE.",
        parameters={},
    )(list_scheduled_jobs)

    registry.decorator(
        name="cancel_scheduled_job",
        description="لغو یک کار زمان‌بندی‌شدهٔ در انتظار با شناسهٔ آن. SAFE.",
        parameters={"id": {"type": "string", "description": "شناسهٔ کار (از list_scheduled_jobs)"}},
        required=("id",),
    )(cancel_scheduled_job)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scheduler(context: ActionContext):
    scheduler = context.extra.get("scheduler")
    if scheduler is None:
        raise DependencyMissing(
            "scheduler is not configured",
            install_hint="زمان‌بند در این حالت در دسترس نیست.",
        )
    return scheduler


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def schedule_reminder(*, at: str, message: str, context: ActionContext) -> str:
    if not isinstance(message, str) or not message.strip():
        raise AssistantError("متن یادآوری خالی است")
    try:
        job = _scheduler(context).add(at, type_="reminder", message=message.strip())
    except ValueError as exc:
        raise AssistantError(str(exc)) from exc
    return f"✅ یادآوری ثبت شد: «{job.message}» در {job.at} (شناسه: {job.id})"


@risk(Risk.DESTRUCTIVE)
def schedule_task(*, at: str, action_name: str, arguments: dict[str, Any] | None = None,
                  context: ActionContext) -> str:
    registry = context.extra.get("registry")
    if registry is not None:
        try:
            registry.get(str(action_name))
        except AssistantError as exc:
            raise AssistantError(
                f"اکشن «{action_name}» وجود ندارد؛ نام اکشن را از فهرست اکشن‌ها انتخاب کنید."
            ) from exc
    try:
        job = _scheduler(context).add(
            at, type_="task",
            action_name=str(action_name),
            arguments=dict(arguments or {}),
        )
    except ValueError as exc:
        raise AssistantError(str(exc)) from exc
    return f"✅ کار زمان‌بندی‌شده ثبت شد: {job.action_name} در {job.at} (شناسه: {job.id})"


@risk(Risk.SAFE)
def list_scheduled_jobs(*, context: ActionContext) -> str:
    jobs = _scheduler(context).list_jobs()
    if not jobs:
        return "هیچ کار زمان‌بندی‌شده‌ای ثبت نشده است."
    lines: list[str] = []
    for job in jobs:
        kind = "یادآوری" if job["type"] == "reminder" else f"کار ({job['action_name']})"
        status = {"pending": "در انتظار", "fired": "انجام شد", "cancelled": "لغو شده"}.get(
            job["status"], job["status"]
        )
        tail = f" — {job['message']}" if job.get("message") else ""
        lines.append(f"  • [{status}] {job['id']} — {kind} — {job['at']}{tail}")
    return "کارهای زمان‌بندی‌شده:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def cancel_scheduled_job(*, id: str, context: ActionContext) -> str:
    ok = _scheduler(context).cancel(str(id))
    if not ok:
        raise AssistantError(
            f"کاری با شناسهٔ «{id}» در حالت در انتظار پیدا نشد (شاید اجرا شده یا لغو شده)."
        )
    return f"✅ کار زمان‌بندی‌شدهٔ «{id}» لغو شد."
