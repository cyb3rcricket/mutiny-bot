"""Security-focused tests for monitoring interactions."""

import unittest
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

# Avoid importing optional heavy deps during unit test module import.
stub_news_monitor = types.ModuleType("tools.news_monitor")
stub_news_monitor.get_fresh_news = lambda *args, **kwargs: []
stub_news_monitor.execute_news_monitor = lambda *args, **kwargs: None
sys.modules.setdefault("tools.news_monitor", stub_news_monitor)

from cogs.monitoring import DockerRestartView, MonitoringCog, parse_schedule_time


class _DummyResponse:
    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


class _DummyInteraction:
    def __init__(self, custom_id: str, is_admin: bool = True) -> None:
        self.guild = object()
        perms = SimpleNamespace(manage_guild=is_admin, administrator=False)
        self.user = SimpleNamespace(guild_permissions=perms)
        self.data = {"custom_id": custom_id}
        self.response = _DummyResponse()


class DockerRestartViewSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_invalid_container_name(self) -> None:
        view = DockerRestartView([{"name": "web"}], bot=None)
        interaction = _DummyInteraction("unused")

        with patch.object(MonitoringCog, "_has_admin_permissions", return_value=True), patch("cogs.monitoring.subprocess.run") as mock_run:
            await view.restart_container(interaction, "web;rm -rf /")

        mock_run.assert_not_called()
        self.assertTrue(interaction.response.messages)
        self.assertIn("Invalid or unauthorized container name.", interaction.response.messages[0][0])

    async def test_allows_known_safe_container(self) -> None:
        view = DockerRestartView([{"name": "web"}], bot=None)
        interaction = _DummyInteraction("unused")

        completed = SimpleNamespace(returncode=0, stderr="")
        with patch.object(MonitoringCog, "_has_admin_permissions", return_value=True), patch("cogs.monitoring.subprocess.run", return_value=completed) as mock_run:
            await view.restart_container(interaction, "web")

        mock_run.assert_called_once_with(
            ["docker", "restart", "web"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(interaction.response.messages)
        self.assertIn("restarted successfully", interaction.response.messages[0][0])


class AddNewsMonitorScheduleParseTests(unittest.IsolatedAsyncioTestCase):
    """Ensure add_news_monitor handles bad schedule strings without crashing."""

    def _make_cog(self):
        """Build a minimal MonitoringCog-like object without a real bot."""
        bot = SimpleNamespace(scheduler_manager=SimpleNamespace(scheduler=SimpleNamespace(get_job=lambda _: None)))
        cog = object.__new__(MonitoringCog)
        cog.bot = bot
        cog.palace_path = "/tmp/test_palace"
        return cog

    def _make_interaction(self, is_admin: bool = True) -> "_DummyInteraction":
        return _DummyInteraction("unused", is_admin=is_admin)

    async def test_bad_frequency_sends_error_not_crash(self) -> None:
        """ValueError from parse_schedule_time must reach the user, not APScheduler."""
        cog = self._make_cog()
        interaction = self._make_interaction()

        with patch.object(MonitoringCog, "_check_channel", return_value=True), \
             patch.object(MonitoringCog, "_has_admin_permissions", return_value=True):
            await cog.add_news_monitor.callback(
                cog,
                interaction,
                channel=SimpleNamespace(id=1, mention="#news"),
                name="test",
                search_query="python",
                frequency="every five seconds",  # malformed — no trailing " minutes"
                time="08:00",
            )

        self.assertTrue(interaction.response.messages, "Expected an error message to be sent")
        error_text, ephemeral = interaction.response.messages[0]
        self.assertTrue(ephemeral, "Error reply must be ephemeral")
        self.assertIn("Unsupported time format", error_text)

    async def test_valid_daily_schedule_does_not_raise(self) -> None:
        """A well-formed 'daily' input must not produce an error reply."""
        added_jobs: list = []

        def fake_add_job(func, trigger=None, args=None, id=None, name=None):
            added_jobs.append(id)

        bot = SimpleNamespace(
            scheduler_manager=SimpleNamespace(
                scheduler=SimpleNamespace(
                    get_job=lambda _: None,
                    add_job=fake_add_job,
                )
            )
        )
        cog = object.__new__(MonitoringCog)
        cog.bot = bot
        cog.palace_path = "/tmp/test_palace"
        interaction = self._make_interaction()

        with patch.object(MonitoringCog, "_check_channel", return_value=True), \
             patch.object(MonitoringCog, "_has_admin_permissions", return_value=True):
            await cog.add_news_monitor.callback(
                cog,
                interaction,
                channel=SimpleNamespace(id=42, mention="#news"),
                name="test",
                search_query="python",
                frequency="daily",
                time="08:00",
            )

        self.assertEqual(len(added_jobs), 1, "Job should be added for valid schedule")
        error_messages = [m for m in interaction.response.messages if m[1]]  # ephemeral = error
        self.assertFalse(error_messages, "No error messages expected for a valid schedule")


if __name__ == "__main__":
    unittest.main()
