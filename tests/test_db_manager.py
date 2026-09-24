"""Unit tests for DatabaseManager with in-memory SQLite."""

import os
import tempfile
import unittest

from config import MAX_INPUT_CHARS
from database.db import DatabaseManager, InputTooLong


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

    async def test_oversized_input_is_rejected_and_long_output_is_kept(self) -> None:
        long_output = "z" * (MAX_INPUT_CHARS + 500)
        await self.db_manager.insert_message(
            (await self.db_manager.create_thread("notes"))["id"],
            "assistant",
            long_output,
            enforce_limit=False,
        )
        oversized = "a" * (MAX_INPUT_CHARS + 1)
        with self.assertRaises(InputTooLong):
            await self.db_manager.insert_history_message("u2", "user", oversized)

        thread = await self.db_manager.create_thread("kept")
        stored = await self.db_manager.insert_message(
            thread["id"],
            "assistant",
            "b" * 9000,
            enforce_limit=False,
        )
        self.assertEqual(len(stored["content"]), 9000)

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
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db = os.path.join(tmp_dir, "definitely_missing_mutiny_db.sqlite")
            db_manager = DatabaseManager(missing_db)
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
