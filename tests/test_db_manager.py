"""Unit tests for DatabaseManager with in-memory SQLite."""

import os
import tempfile
import unittest

from database.db import DatabaseManager, MAX_STORED_CONTENT_CHARS, TRUNCATION_SUFFIX


class DatabaseManagerAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Async tests for core DB read/write behavior."""

    async def asyncSetUp(self) -> None:
        self.db_manager = DatabaseManager(":memory:")
        await self.db_manager.setup_database()

    async def asyncTearDown(self) -> None:
        await self.db_manager.close()

    async def test_insert_and_read_recent_history(self) -> None:
        await self.db_manager.insert_history_message("u1", "user", "hello")
        await self.db_manager.insert_history_message("u1", "assistant", "world")

        history = await self.db_manager.get_user_recent_history("u1", limit=10)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["content"], "world")

    async def test_oversized_content_is_truncated(self) -> None:
        oversized = "a" * (MAX_STORED_CONTENT_CHARS + 200)
        await self.db_manager.insert_history_message("u2", "assistant", oversized)

        history = await self.db_manager.get_user_recent_history("u2", limit=1)
        stored = history[0]["content"]
        self.assertEqual(len(stored), MAX_STORED_CONTENT_CHARS)
        self.assertTrue(stored.endswith(TRUNCATION_SUFFIX))

    async def test_get_user_recent_history_empty_user_id_returns_empty(self) -> None:
        await self.db_manager.insert_history_message("u1", "user", "hello")
        history = await self.db_manager.get_user_recent_history("", limit=10)
        self.assertEqual(history, [])

    async def test_get_chat_history_blank_user_id_returns_empty(self) -> None:
        await self.db_manager.insert_history_message("u1", "user", "hello")
        history = await self.db_manager.get_chat_history(user_id="   ", limit=10)
        self.assertEqual(history, [])


class DatabaseManagerFormatSizeTests(unittest.TestCase):
    """Tests for db size formatter helper."""

    def test_format_db_size_missing_file(self) -> None:
        db_manager = DatabaseManager("/tmp/definitely_missing_mutiny_db.sqlite")
        self.assertEqual(db_manager.format_db_size(), "0 KB")

    def test_format_db_size_existing_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"0" * 2048)
            tmp_path = tmp.name

        try:
            db_manager = DatabaseManager(tmp_path)
            size_text = db_manager.format_db_size()
            self.assertIn("KB", size_text)
        finally:
            os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()
