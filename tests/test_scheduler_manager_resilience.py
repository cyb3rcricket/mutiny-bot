"""Tests for scheduler manager dependency resilience."""

import unittest
from unittest.mock import patch

from scheduler.scheduler_manager import SchedulerManager


class _DummyBot:
    pass


class SchedulerManagerResilienceTests(unittest.TestCase):
    """Ensure scheduler starts even when SQLAlchemy jobstore is unavailable."""

    def test_falls_back_to_memory_scheduler_without_sqlalchemy_jobstore(self) -> None:
        bot = _DummyBot()

        with patch("scheduler.scheduler_manager.SQLAlchemyJobStore", None):
            manager = SchedulerManager(bot)

        self.assertIsNotNone(manager.scheduler)
        self.assertIs(bot.scheduler, manager.scheduler)


if __name__ == "__main__":
    unittest.main()
