"""Tests for MemoryPalaceCog.on_message DM guard."""

import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Stub out the optional mempalace package before importing the cog.
_stub_mempalace = types.ModuleType("mempalace")
sys.modules.setdefault("mempalace", _stub_mempalace)
for _sub in ("knowledge_graph", "mcp_server", "searcher"):
    sys.modules.setdefault(f"mempalace.{_sub}", types.ModuleType(f"mempalace.{_sub}"))

from cogs.memory_palace_cog import MemoryPalaceCog  # noqa: E402


def _make_cog(mempalace_available: bool = True) -> MemoryPalaceCog:
    bot = SimpleNamespace()
    cog = object.__new__(MemoryPalaceCog)
    cog.bot = bot
    cog.palace_path = "/tmp/test_palace"
    cog.mempalace_available = mempalace_available
    cog.logger = MagicMock()
    return cog


def _make_message(*, guild=None, bot_author: bool = False, content: str = "hello") -> SimpleNamespace:
    author = SimpleNamespace(bot=bot_author, name="testuser")
    channel = SimpleNamespace(id=999, name="general") if guild else SimpleNamespace(id=999)
    return SimpleNamespace(
        author=author,
        guild=guild,
        channel=channel,
        content=content,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class OnMessageDMGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_message_returns_early_without_crash(self) -> None:
        """on_message must silently ignore DMs (message.guild is None)."""
        cog = _make_cog()
        stub_add_drawer = MagicMock()

        with patch("cogs.memory_palace_cog.tool_add_drawer", stub_add_drawer):
            await cog.on_message(_make_message(guild=None))

        stub_add_drawer.assert_not_called()

    async def test_bot_message_is_ignored(self) -> None:
        """on_message must ignore messages from other bots."""
        cog = _make_cog()
        guild = SimpleNamespace(name="Test Guild")
        stub_add_drawer = MagicMock()

        with patch("cogs.memory_palace_cog.tool_add_drawer", stub_add_drawer):
            await cog.on_message(_make_message(guild=guild, bot_author=True))

        stub_add_drawer.assert_not_called()

    async def test_guild_message_calls_tool_add_drawer(self) -> None:
        """on_message must forward guild messages to tool_add_drawer."""
        cog = _make_cog()
        guild = SimpleNamespace(name="Test Guild")
        stub_add_drawer = MagicMock()

        with patch("cogs.memory_palace_cog.tool_add_drawer", stub_add_drawer):
            await cog.on_message(_make_message(guild=guild, content="hi there"))

        stub_add_drawer.assert_called_once()
        call_kwargs = stub_add_drawer.call_args.kwargs
        assert call_kwargs["wing"] == "Test Guild"
        assert call_kwargs["room"] == "general"
        assert call_kwargs["content"] == "hi there"

    async def test_mempalace_unavailable_returns_early(self) -> None:
        """on_message must skip storage when mempalace is unavailable."""
        cog = _make_cog(mempalace_available=False)
        guild = SimpleNamespace(name="Test Guild")
        stub_add_drawer = MagicMock()

        with patch("cogs.memory_palace_cog.tool_add_drawer", stub_add_drawer):
            await cog.on_message(_make_message(guild=guild))

        stub_add_drawer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
