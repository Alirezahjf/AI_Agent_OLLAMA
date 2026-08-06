"""یادآوری و اجرای زمان‌بندی‌شده (scheduler).

یک ریسمان دیمون هر ~۳۰ ثانیه بیدار می‌شود و کارهای سررسیدشده را اجرا
می‌کند.  داده در ``<data_dir>/scheduled.json`` ذخیره می‌شود تا بعد از
ری‌استارت بماند.  دو نوع کار:

* ``reminder`` — سرِ موعد یک رویداد ``scheduled_fired`` منتشر می‌کند تا
  همهٔ کلاینت‌ها (وب/دسکتاپ) اعلان نشان دهند.
* ``task`` — سرِ موعد یک اکشن ثبت‌شده را اجرا می‌کند و نتیجه را به‌صورت
  رویداد ``scheduled_fired`` (با success/result) می‌فرستد.

همهٔ متدها thread-safe هستند (قفل re-entrant) و ذخیره‌سازی اتمیک است.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.logging_setup import get_logger

logger = get_logger("scheduler")

#: اعداد فارسی/عربی به انگلیسی برای عبارت‌های نسبی مثل «تا ۵ دقیقه دیگر»
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_WORD_NUMBERS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5,
    "شش": 6, "هفت": 7, "هشت": 8, "نه": 9, "ده": 10,
    "یازده": 11, "دوازده": 12,
}


def _word_number(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    return _WORD_NUMBERS.get(raw, 1)


@dataclass
class ScheduledJob:
    """یک کار زمان‌بندی‌شدهٔ واحد."""

    id: str
    at: str  # ISO datetime
    type: str  # reminder | task
    message: str = ""
    action_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | fired | cancelled
    # ساعت محلیِ بی‌منطقه عمدی است: «در HH:MM» یعنی ساعتِ خودِ کاربر.
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))  # noqa: DTZ005

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScheduledJob:
        return cls(
            id=str(payload.get("id", "")),
            at=str(payload.get("at", "")),
            type=str(payload.get("type", "reminder")),
            message=str(payload.get("message", "")),
            action_name=str(payload.get("action_name", "")),
            arguments=dict(payload.get("arguments") or {}),
            status=str(payload.get("status", "pending")),
            created_at=str(payload.get("created_at", "")),
        )


def parse_at(value: Any, *, now: datetime | None = None) -> datetime:
    """تبدیل «at» به datetime محلی.

    ورودی‌های پذیرفته‌شده:

    * رشتهٔ ISO (``2026-08-06T18:30`` یا همراه منطقهٔ زمانی)
    * «در HH:MM» یا ``HH:MM`` — امروز؛ اگر گذشته بود فردا
    * عبارت‌های نسبی فارسی: «تا ۵ دقیقه دیگر»، «۵ دقیقه بعد»، «یک ساعت
      دیگر» و معادل انگلیسی ساده (``in 5 minutes``)
    """
    # ساعت محلیِ بی‌منطقه عمدی است تا «در HH:MM» با ساعت کاربر هم‌خوان شود.
    now = now or datetime.now()  # noqa: DTZ005
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip().translate(_PERSIAN_DIGITS).strip().lower()
    if not raw:
        raise ValueError("زمان (at) خالی است")

    # ISO
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None and now.tzinfo is not None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed

    # «در HH:MM» / HH:MM (امروز؛ اگر گذشته بود فردا)
    match = re.fullmatch(r"(?:در\s+)?(\d{1,2}):(\d{2})", raw)
    if match:
        when = now.replace(
            hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0,
        )
        if when <= now:
            when += timedelta(days=1)
        return when

    # نسبی: «تا ۵ دقیقه دیگر» / «۵ دقیقه بعد» / «یک ساعت دیگر» / «in 5 minutes»
    number = r"(?:\d+|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده|یازده|دوازده)"
    match = re.search(rf"({number})\s*(?:دقیقه|دقایق|min(?:ute)?s?)", raw)
    if match:
        return now + timedelta(minutes=_word_number(match.group(1)))
    match = re.search(rf"({number})\s*(?:ساعت|ساعات|hour(?:s)?)", raw)
    if match:
        return now + timedelta(hours=_word_number(match.group(1)))
    match = re.search(rf"({number})\s*(?:ثانیه|ثانیه‌ها|sec(?:ond)?s?)", raw)
    if match:
        return now + timedelta(seconds=_word_number(match.group(1)))

    raise ValueError(
        f"زمان «{value}» قابل فهم نیست؛ از ISO (مثل 2026-08-06T18:30) یا "
        "«در HH:MM» یا «تا N دقیقه دیگر» استفاده کنید."
    )


class Scheduler:
    """ریسمان دیمون زمان‌بندی + ذخیره‌سازی پایدار در scheduled.json."""

    def __init__(
        self,
        data_dir: Path,
        *,
        check_interval: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "scheduled.json"
        self._interval = check_interval
        self._clock = clock or datetime.now
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jobs: list[ScheduledJob] = []
        self._on_fire: Callable[[ScheduledJob], None] | None = None
        self._load()

    # ------------------------------------------------------------ lifecycle

    @property
    def path(self) -> Path:
        return self._path

    def set_fire_callback(self, callback: Callable[[ScheduledJob], None]) -> None:
        self._on_fire = callback

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="scheduler", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._tick()
            except Exception:
                logger.exception("scheduler tick crashed")

    # ---------------------------------------------------------------- jobs

    def add(
        self,
        at: Any,
        *,
        type_: str,
        message: str = "",
        action_name: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> ScheduledJob:
        """ثبت یک کار جدید. ``at`` با :func:`parse_at` تفسیر می‌شود."""
        when = parse_at(at, now=self._clock())
        if when <= self._clock():
            raise ValueError("زمان باید در آینده باشد")
        job = ScheduledJob(
            id=uuid.uuid4().hex[:12],
            at=when.isoformat(timespec="seconds"),
            type=type_,
            message=message,
            action_name=action_name,
            arguments=dict(arguments or {}),
        )
        with self._lock:
            self._jobs.append(job)
            self._save_locked()
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self._jobs]

    def cancel(self, job_id: str) -> bool:
        """لغو یک کار در حالت در انتظار؛ اگر بود/انجام شد False برمی‌گرداند."""
        with self._lock:
            for job in self._jobs:
                if job.id == job_id and job.status == "pending":
                    job.status = "cancelled"
                    self._save_locked()
                    return True
        return False

    # ---------------------------------------------------------------- tick

    def _tick(self, now: datetime | None = None) -> list[ScheduledJob]:
        """کارهای سررسیدشده را آتش می‌زند؛ لیست آن‌ها را برمی‌گرداند (تست)."""
        fired: list[ScheduledJob] = []
        with self._lock:
            for job in self._jobs:
                if job.status == "pending" and self._is_due(job, now or self._clock()):
                    job.status = "fired"
                    fired.append(job)
            if fired:
                self._save_locked()
        for job in fired:
            self._notify_fire(job)
        return fired

    def _is_due(self, job: ScheduledJob, now: datetime) -> bool:
        try:
            when = datetime.fromisoformat(job.at)
        except ValueError:
            return False
        if when.tzinfo is not None and now.tzinfo is None:
            when = when.astimezone().replace(tzinfo=None)
        elif when.tzinfo is None and now.tzinfo is not None:
            when = when.replace(tzinfo=now.tzinfo)
        return when <= now

    def _notify_fire(self, job: ScheduledJob) -> None:
        callback = self._on_fire
        if callback is None:
            return
        try:
            callback(job)
        except Exception:
            logger.exception("scheduled fire callback crashed")

    # ---------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("scheduled.json خوانده نشد؛ نادیده گرفته شد")
            return
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if isinstance(raw_jobs, list):
            self._jobs = [
                ScheduledJob.from_dict(item)
                for item in raw_jobs
                if isinstance(item, dict) and item.get("id")
            ]

    def _save_locked(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"jobs": [job.to_dict() for job in self._jobs]},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("ذخیرهٔ کارهای زمان‌بندی‌شده ممکن نشد: %s", exc)
