"""Scheduler persistence is required. There is no silent in-memory fallback."""

import os
import tempfile
import unittest

from database.db import DatabaseManager
from scheduler.scheduler_manager import SchedulerManager, SchedulerUnavailable


class SchedulerManagerResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_scheduler_rejects_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DatabaseManager(os.path.join(tmp, "app.db"))
            await db.setup_database()
            manager = SchedulerManager(
                db,
                os.path.join(tmp, "sched.db"),
                legacy_scheduler_db_path=os.path.join(tmp, "missing-legacy.db"),
            )
            from scheduler import scheduler_manager as module

            original = module.SQLAlchemyJobStore
            module.SQLAlchemyJobStore = None
            try:
                await manager.start_scheduler()
                self.assertFalse(manager.available)
                self.assertIn("SQLAlchemy", manager.unavailable_reason)
                with self.assertRaises(SchedulerUnavailable):
                    await manager.add_daily_job(
                        name="Morning",
                        tool_name="get_morning_briefing",
                        time_of_day="07:00",
                        timezone="UTC",
                    )
            finally:
                module.SQLAlchemyJobStore = original
                await db.close()


if __name__ == "__main__":
    unittest.main()
