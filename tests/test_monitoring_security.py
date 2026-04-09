"""Security-focused tests for monitoring interactions."""

import unittest
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

# Avoid importing optional heavy deps during unit test module import.
stub_news_monitor = types.ModuleType("tools.news_monitor")
stub_news_monitor.get_fresh_news = lambda *args, **kwargs: []
sys.modules.setdefault("tools.news_monitor", stub_news_monitor)

from cogs.monitoring import DockerRestartView, MonitoringCog


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


if __name__ == "__main__":
    unittest.main()
