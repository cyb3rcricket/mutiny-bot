"""Local schedules persist, fail closed, and do not execute legacy jobs."""

import os
import tempfile
import unittest

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database.db import DatabaseManager
from scheduler.execution import run_scheduled_job
from scheduler.scheduler_manager import SchedulerManager, SchedulerUnavailable, job_store_url
from scripts.migrate_legacy_jobs import migrate_legacy_jobs
from tools.morning_brief import get_morning_briefing
from tools.registry import AVAILABLE_TOOLS
from tools.scheduler_manager import execute_and_broadcast


class SchedulerJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app_path = os.path.join(self._tmp.name, "app.db")
        self.sched_path = os.path.join(self._tmp.name, "console-scheduler.db")
        self.legacy_path = os.path.join(self._tmp.name, "mutiny_scheduler.db")
        self.db = DatabaseManager(self.app_path)
        await self.db.setup_database()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_daily_job_survives_restart_and_stores_output(self) -> None:
        manager = SchedulerManager(self.db, self.sched_path, legacy_scheduler_db_path=self.legacy_path)
        await manager.start_scheduler()
        created = await manager.add_daily_job(
            name="Morning",
            tool_name="get_morning_briefing",
            time_of_day="07:15",
            timezone="UTC",
        )
        manager.shutdown()

        restarted = SchedulerManager(self.db, self.sched_path, legacy_scheduler_db_path=self.legacy_path)
        await restarted.start_scheduler()
        jobs = restarted.list_jobs()
        self.assertEqual([job["id"] for job in jobs], [created["id"]])
        self.assertEqual(jobs[0]["tool_name"], "get_morning_briefing")

        original = AVAILABLE_TOOLS["get_morning_briefing"]

        async def _long_briefing() -> str:
            return "N" * 9000

        AVAILABLE_TOOLS["get_morning_briefing"] = _long_briefing
        try:
            stored = await restarted.run_job_now(created["id"])
        finally:
            AVAILABLE_TOOLS["get_morning_briefing"] = original
            restarted.shutdown()
        self.assertEqual(stored["status"], "complete")
        self.assertEqual(len(stored["output"]), 9000)
        reloaded = await self.db.get_run(stored["id"])
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(len(reloaded["output"]), 9000)

    async def test_pause_resume_stop_and_policy_recheck(self) -> None:
        manager = SchedulerManager(self.db, self.sched_path, legacy_scheduler_db_path=self.legacy_path)
        await manager.start_scheduler()
        created = await manager.add_daily_job(
            name="Morning",
            tool_name="get_morning_briefing",
            time_of_day="06:00",
            timezone="UTC",
        )
        paused = manager.pause_job(created["id"])
        self.assertTrue(paused["paused"])
        resumed = manager.resume_job(created["id"])
        self.assertFalse(resumed["paused"])
        with self.assertRaises(SchedulerUnavailable):
            await manager.add_daily_job(
                name="Nope",
                tool_name="stop_automation",
                time_of_day="06:00",
                timezone="UTC",
            )
        denied = await run_scheduled_job("stop_automation", {}, created["id"])
        denied_run = await self.db.get_run(denied)
        self.assertEqual(denied_run["error_code"], "policy_denied")
        self.assertNotIn("Traceback", denied_run["output"])

        original = AVAILABLE_TOOLS["get_morning_briefing"]

        async def _explode() -> str:
            raise RuntimeError("SECRET-TOKEN")

        AVAILABLE_TOOLS["get_morning_briefing"] = _explode
        try:
            failed = await manager.run_job_now(created["id"])
        finally:
            AVAILABLE_TOOLS["get_morning_briefing"] = original
        self.assertEqual(failed["error_code"], "tool_failed")
        self.assertNotIn("SECRET-TOKEN", failed["output"])
        manager.remove_job(created["id"])
        self.assertEqual(manager.list_jobs(), [])
        retained = await self.db.list_runs_for_job(created["id"])
        self.assertGreaterEqual(len(retained["items"]), 1)
        manager.shutdown()

    async def test_missing_sqlalchemy_rejects_job_creation(self) -> None:
        manager = SchedulerManager(self.db, self.sched_path, legacy_scheduler_db_path=self.legacy_path)
        from scheduler import scheduler_manager as module

        original = module.SQLAlchemyJobStore
        module.SQLAlchemyJobStore = None
        try:
            await manager.start_scheduler()
            self.assertFalse(manager.available)
            with self.assertRaises(SchedulerUnavailable):
                await manager.add_daily_job(
                    name="Morning",
                    tool_name="get_morning_briefing",
                    time_of_day="07:00",
                    timezone="UTC",
                )
        finally:
            module.SQLAlchemyJobStore = original

    async def test_legacy_store_is_not_started(self) -> None:
        with open(self.legacy_path, "wb") as handle:
            handle.write(b"tools.scheduler_manager:execute_and_broadcast")
        manager = SchedulerManager(
            self.db,
            self.legacy_path,
            legacy_scheduler_db_path=self.legacy_path,
        )
        await manager.start_scheduler()
        self.assertFalse(manager.available)
        self.assertTrue(manager.legacy_jobs_pending)
        self.assertFalse(os.path.exists(self.sched_path))

    async def test_legacy_migration_imports_safe_jobs_paused(self) -> None:
        legacy = os.path.join(self._tmp.name, "real-legacy.db")
        console = os.path.join(self._tmp.name, "migrated.db")
        scheduler = BackgroundScheduler(jobstores={"default": __import__("apscheduler.jobstores.sqlalchemy", fromlist=["SQLAlchemyJobStore"]).SQLAlchemyJobStore(url=job_store_url(legacy))})
        scheduler.start(paused=True)
        scheduler.add_job(
            execute_and_broadcast,
            CronTrigger(hour=7, minute=5, timezone="UTC"),
            args=("get_morning_briefing",),
            id="auto_get_morning_briefing_old",
        )
        from tools.news_monitor import execute_news_monitor

        scheduler.add_job(
            execute_news_monitor,
            CronTrigger(hour=8, minute=0, timezone="UTC"),
            args=({"name": "news", "search_query": "python"},),
            id="news_monitor_old",
        )
        scheduler.shutdown(wait=False)

        report = migrate_legacy_jobs(legacy, console)
        self.assertEqual(report["imported_paused"], 1)
        self.assertTrue(report["legacy_jobs_pending"])
        statuses = {item["func_ref"]: item["status"] for item in report["jobs"]}
        self.assertIn("imported_paused", statuses.values())
        self.assertIn("disabled", statuses.values())

        manager = SchedulerManager(self.db, console, legacy_scheduler_db_path=legacy)
        await manager.start_scheduler()
        jobs = manager.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["paused"])
        self.assertEqual(jobs[0]["tool_name"], "get_morning_briefing")
        manager.shutdown()
        self.assertTrue(callable(get_morning_briefing))


if __name__ == "__main__":
    unittest.main()
