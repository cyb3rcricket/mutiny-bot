"""Inspect a trusted local APScheduler file and recreate safe jobs paused.

This does not start either scheduler, so legacy callables are not executed.
The original job-store file is left unchanged. Do not point this at an uploaded file.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.execution import run_scheduled_job
from scheduler.scheduler_manager import job_store_url

try:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
except ImportError:  # pragma: no cover
    SQLAlchemyJobStore = None

_SAFE_TOOLS = {"get_morning_briefing"}


def _func_ref(job: Any) -> str:
    return str(getattr(job, "func_ref", "") or "")


def _classify(job: Any) -> dict[str, Any]:
    ref = _func_ref(job)
    args = list(getattr(job, "args", []) or [])
    record = {
        "legacy_id": getattr(job, "id", ""),
        "name": getattr(job, "name", "") or "",
        "func_ref": ref,
        "status": "unsupported",
        "detail": "This schedule cannot be recreated automatically.",
    }
    if ref.endswith("execute_news_monitor") or "news_monitor" in ref:
        record["status"] = "disabled"
        record["detail"] = "News schedules stay disabled and were not started."
        return record
    if ref.endswith("resume_job"):
        record["status"] = "unsupported"
        record["detail"] = "Snooze/resume callbacks must be recreated by hand."
        return record
    if ref.endswith("execute_and_broadcast") and args and args[0] in _SAFE_TOOLS:
        trigger = getattr(job, "trigger", None)
        hour = minute = None
        for field in getattr(trigger, "fields", []) or []:
            if field.name == "hour" and str(field).isdigit():
                hour = int(str(field))
            if field.name == "minute" and str(field).isdigit():
                minute = int(str(field))
        if hour is None or minute is None:
            record["detail"] = "The daily time could not be read from this job."
            return record
        record["status"] = "imported_paused"
        record["tool_name"] = args[0]
        record["hour"] = hour
        record["minute"] = minute
        record["detail"] = "Recreated paused on the local console scheduler."
        return record
    if ref.endswith("execute_and_broadcast"):
        record["detail"] = "The scheduled tool is not in the safe daily set."
    return record


def inspect_legacy_jobs(legacy_path: str) -> list[dict[str, Any]]:
    if SQLAlchemyJobStore is None:
        raise RuntimeError("SQLAlchemy is required to inspect schedules.")
    store = SQLAlchemyJobStore(url=job_store_url(legacy_path))
    try:
        jobs = list(store.get_all_jobs())
    finally:
        store.shutdown()
    return [_classify(job) for job in jobs]


def migrate_legacy_jobs(legacy_path: str, console_path: str) -> dict[str, Any]:
    """Recreate safe daily jobs paused in a new store. Never starts them."""
    classified = inspect_legacy_jobs(legacy_path)
    if SQLAlchemyJobStore is None:
        raise RuntimeError("SQLAlchemy is required to import schedules.")
    scheduler = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=job_store_url(console_path))})
    scheduler.start(paused=True)
    imported = 0
    try:
        for item in classified:
            if item["status"] != "imported_paused":
                continue
            zone = "UTC"
            scheduler.add_job(
                run_scheduled_job,
                CronTrigger(hour=item["hour"], minute=item["minute"], timezone=zone),
                args=[item["tool_name"], {}, None],
                id=f"migrated_{item['legacy_id']}"[:120],
                name=item["name"] or item["tool_name"],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            scheduler.pause_job(f"migrated_{item['legacy_id']}"[:120])
            imported += 1
    finally:
        scheduler.shutdown(wait=False)
    pending = [item for item in classified if item["status"] != "imported_paused"]
    return {
        "imported_paused": imported,
        "legacy_jobs_pending": bool(pending),
        "jobs": classified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate a trusted local Mutiny job store.")
    parser.add_argument("legacy_path")
    parser.add_argument("console_path")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    report = migrate_legacy_jobs(args.legacy_path, args.console_path)
    text = json.dumps(report, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
