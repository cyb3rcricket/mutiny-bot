"""Database manager for MutinyBot SQLite operations."""

import os
from typing import Optional

import aiosqlite

from config import ALLOWED_MODELS, BROADCAST_CHANNEL_ID, DEFAULT_MODEL, DEFAULT_SYSTEM_PROMPT


class DatabaseManager:
    """Handles all SQLite database operations for the bot."""

    def __init__(self, db_path: str, bot=None):
        self.db_path = db_path
        self.bot = bot

    async def setup_database(self) -> None:
        """Create the SQLite database schema and indexes if they do not exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    user_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_history_user_timestamp
                ON chat_history (user_id, timestamp)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES ('model', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (DEFAULT_MODEL,),
            )
            await db.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES ('system_prompt', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (DEFAULT_SYSTEM_PROMPT,),
            )
            await db.commit()

    async def update_config(self, key: str, value: str) -> None:
        """Safely insert or update a bot_config setting."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await db.commit()

    async def get_config(self, key: str, default: str) -> str:
        """Read a bot_config value with fallback and automatic default persistence."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
            row = await cursor.fetchone()

        if not row or not row[0]:
            await self.update_config(key, default)
            return default

        return str(row[0])

    async def get_current_model(self) -> str:
        """Read and validate the configured model from SQLite."""
        selected_model = await self.get_config("model", DEFAULT_MODEL)

        if selected_model not in ALLOWED_MODELS:
            await self.update_config("model", DEFAULT_MODEL)
            return DEFAULT_MODEL

        return selected_model

    async def get_system_prompt(self) -> str:
        """Read the active system prompt from SQLite."""
        return await self.get_config("system_prompt", DEFAULT_SYSTEM_PROMPT)

    def format_db_size(self) -> str:
        """Format database size in KB/MB for status display."""
        if not os.path.exists(self.db_path):
            return "0 KB"

        size_bytes = os.path.getsize(self.db_path)
        size_kb = size_bytes / 1024
        if size_kb >= 1024:
            return f"{size_kb / 1024:.2f} MB"
        return f"{size_kb:.2f} KB"

    async def insert_history_message(self, user_id: str, role: str, content: str) -> None:
        """Persist one conversation message for a user."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content),
            )
            await db.commit()

    async def get_recent_history(self, user_id: str, limit: int = 10) -> list[dict[str, str]]:
        """Read the most recent messages for a user in chronological order."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT role, content
                FROM (
                    SELECT role, content, timestamp, rowid
                    FROM chat_history
                    WHERE user_id = ?
                    ORDER BY timestamp DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC, rowid ASC
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()

        return [{"role": row[0], "content": row[1]} for row in rows]

    async def get_next_broadcast(self) -> Optional[tuple[int, str]]:
        """Get the next broadcast message from the queue."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, content FROM broadcast_queue ORDER BY id ASC LIMIT 1"
            )
            row = await cursor.fetchone()

        if not row:
            return None

        message_id = int(row[0])
        content = str(row[1] or "").strip()
        return message_id, content

    async def delete_broadcast(self, message_id: int) -> None:
        """Delete a broadcast message from the queue."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM broadcast_queue WHERE id = ?", (message_id,))
            await db.commit()

    async def get_chat_history(self, user_id: Optional[str] = None, limit: int = 30) -> list[dict[str, str]]:
        """Return the last `limit` messages from chat_history.

        If `user_id` is provided, only return messages for that user. Results are
        ordered chronologically (oldest first) within the returned window.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if user_id:
                cursor = await db.execute(
                    """
                    SELECT role, content
                    FROM (
                        SELECT role, content, timestamp, rowid
                        FROM chat_history
                        WHERE user_id = ?
                        ORDER BY timestamp DESC, rowid DESC
                        LIMIT ?
                    )
                    ORDER BY timestamp ASC, rowid ASC
                    """,
                    (user_id, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT role, content
                    FROM (
                        SELECT role, content, timestamp, rowid
                        FROM chat_history
                        ORDER BY timestamp DESC, rowid DESC
                        LIMIT ?
                    )
                    ORDER BY timestamp ASC, rowid ASC
                    """,
                    (limit,),
                )

            rows = await cursor.fetchall()

        return [{"role": row[0], "content": row[1]} for row in rows]