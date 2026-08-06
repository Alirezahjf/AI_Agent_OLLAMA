"""Offline tests — ب: یادآوری و اجرای زمان‌بندی‌شده (scheduler).

هیچ شبکه/ساعت واقعی در کار نیست: ساعت fake است و رویدادها روی event_bus
ضبط می‌شوند.  اعلان دسکتاپ ویندوز در اینجا mock می‌شود (نیازمند تأیید
ویندوز ۱۱ واقعی).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from local_agent.actions import run_action
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings
from local_agent.core.errors import AssistantError
from local_agent.core.scheduler import Scheduler, parse_at


def _dt(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    # ساعت محلیِ بی‌منطقه عمدی است (همان قرارداد scheduler).
    return datetime(y, m, d, hh, mm, ss)  # noqa: DTZ001


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# parse_at
# ---------------------------------------------------------------------------


def test_parse_at_iso() -> None:
    when = parse_at("2026-08-06T18:30")
    assert when == _dt(2026, 8, 6, 18, 30)


def test_parse_at_persian_digits_iso() -> None:
    when = parse_at("۲۰۲۶-۰۸-۰۶T۱۸:۳۰")
    assert when == _dt(2026, 8, 6, 18, 30)


def test_parse_at_hhmm_today_or_tomorrow() -> None:
    now = _dt(2026, 8, 6, 10, 0, 0)
    when = parse_at("در 18:30", now=now)
    assert when == _dt(2026, 8, 6, 18, 30)
    # گذشته → فردا
    when = parse_at("در 09:00", now=now)
    assert when == _dt(2026, 8, 7, 9, 0)


def test_parse_at_relative_persian() -> None:
    now = _dt(2026, 8, 6, 10, 0, 0)
    assert parse_at("تا ۵ دقیقه دیگر", now=now) == now + timedelta(minutes=5)
    assert parse_at("۵ دقیقه بعد", now=now) == now + timedelta(minutes=5)
    assert parse_at("یک ساعت دیگر", now=now) == now + timedelta(hours=1)
    assert parse_at("in 5 minutes", now=now) == now + timedelta(minutes=5)


def test_parse_at_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_at("مثلاً فردا")


# ---------------------------------------------------------------------------
# Scheduler core (fake clock)
# ---------------------------------------------------------------------------


def test_add_reminder_persists_to_scheduled_json(tmp_path: Path) -> None:
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    scheduler = Scheduler(tmp_path, clock=clock)
    job = scheduler.add("تا ۵ دقیقه دیگر", type_="reminder", message="جلسه شروع شد")
    assert job.status == "pending"
    assert job.type == "reminder"
    persisted = json.loads((tmp_path / "scheduled.json").read_text(encoding="utf-8"))
    assert persisted["jobs"][0]["id"] == job.id
    assert persisted["jobs"][0]["message"] == "جلسه شروع شد"
    assert persisted["jobs"][0]["at"] == "2026-08-06T10:05:00"


def test_tick_fires_reminder_and_marks_fired(tmp_path: Path) -> None:
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    scheduler = Scheduler(tmp_path, clock=clock)
    fired_events: list[object] = []
    scheduler.set_fire_callback(fired_events.append)
    job = scheduler.add("در 10:01", type_="reminder", message="یادآوری تست")

    assert scheduler._tick() == []  # هنوز موعد نرسیده
    clock.advance(timedelta(minutes=2))
    fired = scheduler._tick()
    assert [j.id for j in fired] == [job.id]
    assert job.status == "fired"
    assert len(fired_events) == 1
    # status در فایل هم ثبت شده
    persisted = json.loads((tmp_path / "scheduled.json").read_text(encoding="utf-8"))
    assert persisted["jobs"][0]["status"] == "fired"


def test_cancel_pending_job(tmp_path: Path) -> None:
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    scheduler = Scheduler(tmp_path, clock=clock)
    job = scheduler.add("در 11:00", type_="reminder", message="x")
    assert scheduler.cancel(job.id) is True
    assert job.status == "cancelled"
    assert scheduler.cancel(job.id) is False
    # لغوشده نباید آتش بزند
    clock.advance(timedelta(hours=2))
    assert scheduler._tick() == []


def test_persistence_across_rebuild(tmp_path: Path) -> None:
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    scheduler = Scheduler(tmp_path, clock=clock)
    job = scheduler.add("در 12:00", type_="reminder", message="بعد از ری‌استارت")

    # ساخت دوباره (شبیه‌سازی ری‌استارت) → کار از فایل بارگذاری می‌شود
    scheduler2 = Scheduler(tmp_path, clock=clock)
    jobs = scheduler2.list_jobs()
    assert [j["id"] for j in jobs] == [job.id]
    assert jobs[0]["message"] == "بعد از ری‌استارت"


# ---------------------------------------------------------------------------
# Actions + BridgeHandlers wiring
# ---------------------------------------------------------------------------


def _handlers_with_scheduler(tmp_path: Path) -> tuple[BridgeHandlers, _FakeClock]:
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    scheduler = Scheduler(tmp_path, clock=clock)
    handlers.context.extra["scheduler"] = scheduler
    scheduler.set_fire_callback(handlers._on_scheduled_fired)
    return handlers, clock


def test_schedule_reminder_action_registers_and_fires_event(tmp_path: Path) -> None:
    handlers, clock = _handlers_with_scheduler(tmp_path)
    events: list[dict] = []

    def _capture(event) -> None:
        events.append(event)

    handlers.event_bus.subscribe(_capture)

    result = run_action(
        handlers.registry, "schedule_reminder",
        {"at": "تا ۵ دقیقه دیگر", "message": "خرید نان"},
        handlers.context,
    )
    assert "یادآوری ثبت شد" in result
    assert (tmp_path / "scheduled.json").is_file()

    clock.advance(timedelta(minutes=6))
    fired = handlers.context.extra["scheduler"]._tick()
    assert len(fired) == 1
    fired_events = [e for e in events if e.type == "scheduled_fired"]
    assert len(fired_events) == 1
    payload = fired_events[0].payload
    assert payload["job"]["type"] == "reminder"
    assert payload["job"]["message"] == "خرید نان"
    assert payload["success"] is True


def test_schedule_task_runs_safe_action_and_streams_result(tmp_path: Path) -> None:
    handlers, clock = _handlers_with_scheduler(tmp_path)
    events: list[dict] = []

    def _capture(event) -> None:
        events.append(event)

    handlers.event_bus.subscribe(_capture)
    handlers.gate.auto_approve()

    result = run_action(
        handlers.registry, "schedule_task",
        {"at": "در 10:02", "action_name": "system_info"},
        handlers.context,
    )
    assert "کار زمان‌بندی‌شده ثبت شد" in result

    clock.advance(timedelta(minutes=3))
    fired = handlers.context.extra["scheduler"]._tick()
    assert len(fired) == 1

    fired_events = [e for e in events if e.type == "scheduled_fired"]
    assert len(fired_events) == 1
    payload = fired_events[0].payload
    assert payload["job"]["action_name"] == "system_info"
    assert payload["success"] is True
    assert "hostname" in payload["result"] or "python" in payload["result"].lower()


def test_schedule_task_unknown_action_rejected(tmp_path: Path) -> None:
    handlers, _clock = _handlers_with_scheduler(tmp_path)
    handlers.gate.auto_approve()
    with pytest.raises(AssistantError) as exc:
        run_action(
            handlers.registry, "schedule_task",
            {"at": "در 11:00", "action_name": "no_such_action_xyz"},
            handlers.context,
        )
    assert "وجود ندارد" in str(exc.value)


def test_schedule_task_requires_confirmation(tmp_path: Path) -> None:
    handlers, _clock = _handlers_with_scheduler(tmp_path)
    # بدون auto-approve نباید اجرا شود (Destructive) — گیت باید بپرسد.
    with pytest.raises(NotImplementedError):
        run_action(
            handlers.registry, "schedule_task",
            {"at": "در 11:00", "action_name": "system_info"},
            handlers.context,
        )


def test_list_and_cancel_actions(tmp_path: Path) -> None:
    handlers, _clock = _handlers_with_scheduler(tmp_path)
    run_action(handlers.registry, "schedule_reminder",
               {"at": "تا ۱۰ دقیقه دیگر", "message": "یادآوری ۱"}, handlers.context)
    run_action(handlers.registry, "schedule_reminder",
               {"at": "تا ۲۰ دقیقه دیگر", "message": "یادآوری ۲"}, handlers.context)

    listing = run_action(handlers.registry, "list_scheduled_jobs", {}, handlers.context)
    assert "یادآوری ۱" in listing
    assert "یادآوری ۲" in listing

    jobs = handlers.context.extra["scheduler"].list_jobs()
    cancelled = run_action(
        handlers.registry, "cancel_scheduled_job", {"id": jobs[0]["id"]}, handlers.context
    )
    assert "لغو شد" in cancelled
    assert handlers.context.extra["scheduler"].list_jobs()[0]["status"] == "cancelled"


def test_scheduler_actions_registered(tmp_path: Path) -> None:
    handlers, _clock = _handlers_with_scheduler(tmp_path)
    names = {a.name for a in handlers.registry.all()}
    assert {"schedule_reminder", "schedule_task", "list_scheduled_jobs",
            "cancel_scheduled_job"} <= names


def test_scheduled_fired_reaches_websocket(web_server) -> None:
    """رویداد سراسری scheduled_fired باید به کلاینت وب برسد (پخش سراسری)."""
    import websockets.sync.client as ws_sync

    backend = web_server.client._backend
    handlers = backend._server.handlers
    clock = _FakeClock(_dt(2026, 8, 6, 10, 0, 0))
    scheduler = Scheduler(handlers.settings.data_dir, clock=clock)
    handlers.context.extra["scheduler"] = scheduler
    scheduler.set_fire_callback(handlers._on_scheduled_fired)

    received: list[str] = []

    with ws_sync.connect(f"ws://127.0.0.1:{web_server.port}/ws") as ws:
        scheduler.add("2026-08-06T10:00:30", type_="reminder", message="تست وب")
        clock.advance(timedelta(minutes=1))
        fired = scheduler._tick()
        assert len(fired) == 1
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=0.5)
            except Exception:  # noqa: BLE001, S112 - timeout یعنی هنوز رویدادی نرسیده
                continue
            msg = json.loads(raw)
            if msg.get("event_type") == "scheduled_fired":
                received.append(msg["payload"]["job"]["message"])
                break
    assert "تست وب" in received
