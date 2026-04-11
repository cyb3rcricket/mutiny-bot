"""Unit tests for scheduler automation tools and guardrails."""

import unittest
from unittest.mock import Mock

from apscheduler.jobstores.base import JobLookupError

from tools.scheduler_manager import (
    list_active_automations,
    reset_tool_request_context,
    schedule_daily_automation,
    set_tool_request_context,
    stop_automation,
)


class SchedulerToolsAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Ensure scheduler tools enforce access and input validation."""

    async def test_stop_automation_requires_admin(self) -> None:
        scheduler = Mock()
        tokens = set_tool_request_context(user_id="u1", is_admin=False, scheduler=scheduler)
        try:
            result = await stop_automation("auto_test_1")
        finally:
            reset_tool_request_context(tokens)

        self.assertIn("not authorized", result.lower())
        scheduler.remove_job.assert_not_called()

    async def test_stop_automation_requires_valid_job_id_format(self) -> None:
        scheduler = Mock()
        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
        try:
            result = await stop_automation("bad id with spaces")
        finally:
            reset_tool_request_context(tokens)

        self.assertIn("invalid job_id format", result.lower())
        scheduler.remove_job.assert_not_called()

    async def test_stop_automation_rejects_colon_in_job_id(self) -> None:
        scheduler = Mock()
        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
        try:
            result = await stop_automation("auto_test:1")
        finally:
            reset_tool_request_context(tokens)

        self.assertIn("invalid job_id format", result.lower())
        scheduler.remove_job.assert_not_called()

    async def test_stop_automation_handles_missing_job(self) -> None:
        scheduler = Mock()
        scheduler.remove_job.side_effect = JobLookupError("missing")

        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
        try:
            result = await stop_automation("auto_test_1")
        finally:
            reset_tool_request_context(tokens)

        self.assertIn("automation not found", result.lower())

    async def test_stop_automation_requires_scheduler_context(self) -> None:
        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=None)
        try:
            result = await stop_automation("auto_test_1")
        finally:
            reset_tool_request_context(tokens)

        self.assertIn("scheduler context is unavailable", result.lower())

    async def test_stop_automation_internal_error_is_sanitized(self) -> None:
        scheduler = Mock()
        scheduler.remove_job.side_effect = RuntimeError("sensitive details")

        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
        try:
            result = await stop_automation("auto_test_1")
        finally:
            reset_tool_request_context(tokens)

        self.assertEqual(result, "Error stopping automation. Check logs for details.")

    async def test_list_active_automations_internal_error_is_sanitized(self) -> None:
        scheduler = Mock()
        scheduler.get_jobs.side_effect = RuntimeError("db stack")

        tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
        try:
            result = await list_active_automations()
        finally:
            reset_tool_request_context(tokens)

        self.assertEqual(result, "Error listing automations. Check logs for details.")

    async def test_schedule_daily_automation_internal_error_is_sanitized(self) -> None:
        import tools.scheduler_manager as sm
        original = sm.BROADCAST_CHANNEL_ID
        try:
            sm.BROADCAST_CHANNEL_ID = 12345
            scheduler = Mock()
            scheduler.add_job.side_effect = RuntimeError("traceback")

            tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
            try:
                result = await schedule_daily_automation("get_morning_briefing", 7, 0)
            finally:
                reset_tool_request_context(tokens)
        finally:
            sm.BROADCAST_CHANNEL_ID = original

        self.assertEqual(result, "Error scheduling automation. Check logs for details.")

    async def test_schedule_daily_automation_rejects_when_broadcast_channel_unconfigured(self) -> None:
        import tools.scheduler_manager as sm
        original = sm.BROADCAST_CHANNEL_ID
        try:
            sm.BROADCAST_CHANNEL_ID = 0
            scheduler = Mock()
            tokens = set_tool_request_context(user_id="u1", is_admin=True, scheduler=scheduler)
            try:
                result = await schedule_daily_automation("get_morning_briefing", 7, 0)
            finally:
                reset_tool_request_context(tokens)
        finally:
            sm.BROADCAST_CHANNEL_ID = original

        self.assertIn("broadcast_channel_id", result.lower())
        scheduler.add_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
