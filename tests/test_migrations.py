"""Legacy schema migration preserves rows and rolls back on failure."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from config import DEFAULT_SYSTEM_PROMPT, LEGACY_DEFAULT_SYSTEM_PROMPT
from database.db import DatabaseManager
from database import migrations


def _legacy_database(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chat_history (
            user_id TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE bot_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE broadcast_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            channel_id INTEGER
        );
        """
    )
    same_time = "2024-01-01 00:00:00"
    conn.execute(
        "INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("user-a", "user", "first", same_time),
    )
    conn.execute(
        "INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("user-a", "assistant", "second", same_time),
    )
    conn.execute(
        "INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("user-b", "user", "other", "2024-01-02 00:00:00"),
    )
    conn.execute(
        "INSERT INTO bot_config (key, value) VALUES ('system_prompt', ?)",
        (LEGACY_DEFAULT_SYSTEM_PROMPT,),
    )
    conn.execute(
        "INSERT INTO bot_config (key, value) VALUES ('model', 'missing-model:latest')"
    )
    conn.execute("INSERT INTO facts (fact) VALUES ('remember this')")
    conn.execute("INSERT INTO broadcast_queue (content, channel_id) VALUES ('queued briefing', 7)")
    conn.commit()
    conn.close()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_rows_orders_and_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            _legacy_database(path)
            manager = DatabaseManager(path)
            await manager.setup_database()
            await manager.setup_database()

            threads = await manager.list_threads(limit=10)
            titles = {item["title"] for item in threads["items"]}
            self.assertEqual(titles, {"Imported conversation"})
            self.assertEqual(len(threads["items"]), 2)

            history = await manager.get_user_recent_history("user-a", limit=10)
            self.assertEqual([item["content"] for item in history], ["first", "second"])

            facts = await manager.list_facts(limit=10)
            self.assertEqual(facts["items"][0]["id"], 1)
            self.assertEqual(facts["items"][0]["content"], "remember this")

            prompt = await manager.get_system_prompt()
            self.assertEqual(prompt, DEFAULT_SYSTEM_PROMPT)
            self.assertEqual(await manager.get_current_model(), "missing-model:latest")

            runs = await manager.list_runs_for_job("")
            # Legacy broadcasts have a null job id; read them directly.
            db = await manager._get_db()
            cursor = await db.execute("SELECT output FROM runs WHERE tool_name = 'legacy_broadcast'")
            row = await cursor.fetchone()
            self.assertEqual(row[0], "queued briefing")
            await manager.close()
            self.assertTrue(os.path.exists(path + ".migration-bak"))

    async def test_rollback_restores_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            _legacy_database(path)

            async def _boom(db):
                await db.execute("DELETE FROM chat_history")
                raise RuntimeError("migration failed")

            manager = DatabaseManager(path)
            with patch("database.db.upgrade", new=_boom):
                with self.assertRaises(RuntimeError):
                    await manager.setup_database()

            conn = sqlite3.connect(path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
                columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_history)")}
            finally:
                conn.close()
            self.assertEqual(count, 3)
            self.assertNotIn("thread_id", columns)


if __name__ == "__main__":
    unittest.main()
