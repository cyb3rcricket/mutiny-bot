"""Tests for news_monitor two-phase commit: articles are marked posted AFTER broadcast."""

import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies before importing the module under test.
# ---------------------------------------------------------------------------
for _mod in (
    "feedparser",
    "litellm",
    "config",
    "mempalace",
    "mempalace.mcp_server",
    "mempalace.searcher",
    "tools.scheduler_manager",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# Give config the attributes the module reads at import time.
sys.modules["config"].DEFAULT_MODEL = "llama3"
sys.modules["config"].OLLAMA_API_BASE = "http://localhost:11434"

# Give stub modules the attributes that patch.object / the code accesses.
sys.modules["feedparser"].parse = MagicMock()
sys.modules["litellm"].acompletion = AsyncMock()
sys.modules["tools.scheduler_manager"]._enqueue_broadcast = AsyncMock()

import tools.news_monitor as news_monitor_mod  # noqa: E402


class GetFreshNewsNoMarkingTests(unittest.IsolatedAsyncioTestCase):
    """get_fresh_news must return new articles WITHOUT marking them as posted."""

    async def test_does_not_call_tool_add_drawer(self) -> None:
        stub_add_drawer = MagicMock()
        stub_search = MagicMock(return_value=None)  # no results → new article

        fake_entry = types.SimpleNamespace(
            link="https://example.com/article1",
            title="Article 1",
            published="2024-01-01",
            summary="summary text",
        )
        fake_feed = types.SimpleNamespace(entries=[fake_entry])

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch.object(news_monitor_mod, "search_memories", stub_search), \
             patch("tools.news_monitor.feedparser.parse", return_value=fake_feed):
            articles = await news_monitor_mod.get_fresh_news("python", "test-room", "/tmp/palace")

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["link"], "https://example.com/article1")
        stub_add_drawer.assert_not_called()

    async def test_already_posted_article_excluded(self) -> None:
        stub_add_drawer = MagicMock()
        stub_search = MagicMock(return_value={"results": [{"text": "seen"}]})  # already posted

        fake_entry = types.SimpleNamespace(
            link="https://example.com/old",
            title="Old Article",
            published="2024-01-01",
            summary="",
        )
        fake_feed = types.SimpleNamespace(entries=[fake_entry])

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch.object(news_monitor_mod, "search_memories", stub_search), \
             patch("tools.news_monitor.feedparser.parse", return_value=fake_feed):
            articles = await news_monitor_mod.get_fresh_news("python", "test-room", "/tmp/palace")

        self.assertEqual(articles, [])
        stub_add_drawer.assert_not_called()


class ExecuteNewsMonitorTwoPhaseTests(unittest.IsolatedAsyncioTestCase):
    """execute_news_monitor must mark articles AFTER a successful broadcast."""

    def _make_job_data(self) -> dict:
        return {
            "name": "test-monitor",
            "search_query": "python",
            "channel_id": 42,
            "palace_path": "/tmp/palace",
        }

    async def test_marks_articles_after_broadcast_success(self) -> None:
        stub_add_drawer = MagicMock()
        articles = [{"title": "A", "link": "https://example.com/a", "published": "", "summary": "s"}]

        fake_choice = types.SimpleNamespace(message=types.SimpleNamespace(content="blurb"))
        fake_response = types.SimpleNamespace(choices=[fake_choice])

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch("tools.news_monitor.get_fresh_news", AsyncMock(return_value=articles)), \
             patch("tools.news_monitor.litellm.acompletion", AsyncMock(return_value=fake_response)), \
             patch("tools.scheduler_manager._enqueue_broadcast", AsyncMock()) as mock_broadcast:
            await news_monitor_mod.execute_news_monitor(self._make_job_data())

        mock_broadcast.assert_awaited_once()
        stub_add_drawer.assert_called_once_with(
            wing="news-monitor",
            room="test-monitor",
            content="https://example.com/a",
            added_by="news_monitor",
        )

    async def test_does_not_mark_when_broadcast_raises(self) -> None:
        stub_add_drawer = MagicMock()
        articles = [{"title": "A", "link": "https://example.com/a", "published": "", "summary": "s"}]

        fake_choice = types.SimpleNamespace(message=types.SimpleNamespace(content="blurb"))
        fake_response = types.SimpleNamespace(choices=[fake_choice])

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch("tools.news_monitor.get_fresh_news", AsyncMock(return_value=articles)), \
             patch("tools.news_monitor.litellm.acompletion", AsyncMock(return_value=fake_response)), \
             patch("tools.scheduler_manager._enqueue_broadcast", AsyncMock(side_effect=RuntimeError("send failed"))):
            with self.assertRaises(RuntimeError):
                await news_monitor_mod.execute_news_monitor(self._make_job_data())

        stub_add_drawer.assert_not_called()

    async def test_marks_all_articles_when_multiple(self) -> None:
        stub_add_drawer = MagicMock()
        articles = [
            {"title": "A", "link": "https://example.com/a", "published": "", "summary": ""},
            {"title": "B", "link": "https://example.com/b", "published": "", "summary": ""},
        ]

        fake_choice = types.SimpleNamespace(message=types.SimpleNamespace(content="blurb"))
        fake_response = types.SimpleNamespace(choices=[fake_choice])

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch("tools.news_monitor.get_fresh_news", AsyncMock(return_value=articles)), \
             patch("tools.news_monitor.litellm.acompletion", AsyncMock(return_value=fake_response)), \
             patch("tools.scheduler_manager._enqueue_broadcast", AsyncMock()):
            await news_monitor_mod.execute_news_monitor(self._make_job_data())

        self.assertEqual(stub_add_drawer.call_count, 2)
        links_marked = {c.kwargs["content"] for c in stub_add_drawer.call_args_list}
        self.assertEqual(links_marked, {"https://example.com/a", "https://example.com/b"})

    async def test_no_articles_does_not_call_tool_add_drawer(self) -> None:
        stub_add_drawer = MagicMock()

        with patch.object(news_monitor_mod, "tool_add_drawer", stub_add_drawer), \
             patch("tools.news_monitor.get_fresh_news", AsyncMock(return_value=[])), \
             patch("tools.scheduler_manager._enqueue_broadcast", AsyncMock()):
            await news_monitor_mod.execute_news_monitor(self._make_job_data())

        stub_add_drawer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
