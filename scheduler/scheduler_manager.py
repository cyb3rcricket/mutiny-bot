"""Local APScheduler lifecycle. Jobs persist in SQLite and results persist as runs."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import AUTOMATION_TIMEZONE, LEGACY_SCHEDULER_DB_PATH, SCHEDULER_DB_PATH
from core.tool_runner import ToolRejected, assert_schedulable
from database.db import DatabaseManager
from scheduler.execution import bind_database, run_scheduled_job

try:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
except ImportError:  # pragma: no cover
    SQLAlchemyJobStore = None

logger = logging.getLogger("mutiny_bot.scheduler")
_LEGACY_MARKERS = (
    b"tools.scheduler_manager:execute_and_broadcast",
    b"tools.news_monitor:execute_news_monitor",
    b"scheduler.scheduler_manager:resume_job",
)


class SchedulerUnavailable(RuntimeError):
    """Raised when a schedule cannot be stored durably."""


def resume_job(job_id: str) -> None:
    """Legacy pickled callback. It never resumes a job on its own."""
    logger.warning("Ignored legacy resume_job callback for %s", job_id)


def job_store_url(path: str) -> str:
    absolute = os.path.abspath(path)
    return f"sqlite:///{absolute}"


def legacy_markers_present(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return False
    return any(marker in blob for marker in _LEGACY_MARKERS)


class SchedulerManager:
    """One scheduler process. Persistence is required; there is no memory fallback."""

    def __init__(
        self,
        db: DatabaseManager,
        scheduler_db_path: str = SCHEDULER_DB_PATH,
        legacy_scheduler_db_path: str = LEGACY_SCHEDULER_DB_PATH,
    ) -> None:
        self.db = db
        self.scheduler_db_path = scheduler_db_path
        self.legacy_scheduler_db_path = legacy_scheduler_db_path
        self.scheduler: AsyncIOScheduler | None = None
        self.available = False
        self.unavailable_reason = ""
        self.legacy_jobs_pending = os.path.exists(legacy_scheduler_db_path)

    async def start_scheduler(self) -> None:
        bind_database(self.db)
        if SQLAlchemyJobStore is None:
            self.available = False
            self.unavailable_reason = "SQLAlchemy is required to store schedules."
            return
        if os.path.abspath(self.scheduler_db_path) == os.path.abspath(self.legacy_scheduler_db_path):
            self.available = False
            self.legacy_jobs_pending = True
            self.unavailable_reason = "Refusing to start the scheduler from the legacy job store."
            return
        if legacy_markers_present(self.scheduler_db_path):
            self.available = False
            self.legacy_jobs_pending = True
            self.unavailable_reason = "This job store still contains legacy callable references."
            return
        import tools.morning_brief  # noqa: F401  registers the schedulable local briefing

        jobstores = {"default": SQLAlchemyJobStore(url=job_store_url(self.scheduler_db_path))}
        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )
        self.scheduler.start()
        self.available = True
        self.unavailable_reason = ""

    def shutdown(self) -> None:
        if self.scheduler is not None and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.scheduler = None
        self.available = False

    def _require(self) -> AsyncIOScheduler:
        if not self.available or self.scheduler is None:
            raise SchedulerUnavailable(self.unavailable_reason or "Scheduler is unavailable.")
        return self.scheduler

    async def add_daily_job(
        self,
        *,
        name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        time_of_day: str,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        scheduler = self._require()
        try:
            assert_schedulable(tool_name)
        except ToolRejected as exc:
            raise SchedulerUnavailable(exc.message) from exc
        hour, minute = _parse_hhmm(time_of_day)
        zone_name = timezone or AUTOMATION_TIMEZONE
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise SchedulerUnavailable("Unknown timezone.") from exc
        arguments = dict(arguments or {})
        job = scheduler.add_job(
            run_scheduled_job,
            CronTrigger(hour=hour, minute=minute, timezone=zone),
            args=[tool_name, arguments],
            kwargs={},
            id=f"daily_{tool_name}_{hour:02d}{minute:02d}_{int(datetime.now().timestamp())}",
            name=name.strip() or tool_name,
            replace_existing=False,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        # The dispatcher reads the job id from the third positional slot when present.
        job.modify(args=[tool_name, arguments, job.id])
        return _job_record(job, arguments)

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.available or self.scheduler is None:
            return []
        records = []
        for job in self.scheduler.get_jobs():
            arguments = {}
            args = list(getattr(job, "args", []) or [])
            if len(args) >= 2 and isinstance(args[1], dict):
                arguments = args[1]
            records.append(_job_record(job, arguments))
        return records

    def pause_job(self, job_id: str) -> dict[str, Any]:
        scheduler = self._require()
        scheduler.pause_job(job_id)
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        scheduler = self._require()
        scheduler.resume_job(job_id)
        return self.get_job(job_id)

    def remove_job(self, job_id: str) -> None:
        scheduler = self._require()
        scheduler.remove_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        scheduler = self._require()
        job = scheduler.get_job(job_id)
        if job is None:
            raise SchedulerUnavailable("Job not found.")
        args = list(job.args or [])
        arguments = args[1] if len(args) >= 2 and isinstance(args[1], dict) else {}
        return _job_record(job, arguments)

    async def run_job_now(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        run_id = await run_scheduled_job(job["tool_name"], job["arguments"], job_id)
        stored = await self.db.get_run(run_id)
        if stored is None:
            raise SchedulerUnavailable("Run was not stored.")
        return stored


def _parse_hhmm(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise SchedulerUnavailable("Schedule time must be HH:MM.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise SchedulerUnavailable("Schedule time must be HH:MM.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SchedulerUnavailable("Schedule time must be HH:MM.")
    return hour, minute


def _job_record(job: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    next_run = getattr(job, "next_run_time", None)
    trigger = getattr(job, "trigger", None)
    schedule = {"type": "daily", "time": _trigger_time(trigger), "timezone": _trigger_zone(trigger)}
    args = list(getattr(job, "args", []) or [])
    tool_name = str(args[0]) if args else ""
    return {
        "id": job.id,
        "name": getattr(job, "name", None) or job.id,
        "tool_name": tool_name,
        "arguments": arguments,
        "schedule": schedule,
        "paused": next_run is None,
        "next_run_at": next_run.isoformat() if next_run is not None else None,
    }


def _trigger_time(trigger: Any) -> str:
    if trigger is None:
        return ""
    fields = getattr(trigger, "fields", None)
    if not fields:
        return ""
    hour = minute = None
    for field in fields:
        if field.name == "hour":
            hour = str(field)
        elif field.name == "minute":
            minute = str(field)
    if hour is None or minute is None or not hour.isdigit() or not minute.isdigit():
        return ""
    return f"{int(hour):02d}:{int(minute):02d}"


def _trigger_zone(trigger: Any) -> str:
    timezone = getattr(trigger, "timezone", None)
    if timezone is None:
        return AUTOMATION_TIMEZONE
    return getattr(timezone, "key", None) or str(timezone)
