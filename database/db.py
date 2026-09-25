"""SQLite manager for Mutiny local console data."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aiosqlite

from config import DEFAULT_SYSTEM_PROMPT, LEGACY_DEFAULT_SYSTEM_PROMPT, MAX_INPUT_CHARS
from database.migrations import backup_sqlite, restore_sqlite, upgrade, utc_now
from llm.models import get_installed_models, select_default_model


class InputTooLong(ValueError):
    """Raised when a caller exceeds the documented input limit."""


class DatabaseManager:
    """Handles SQLite persistence. The optional bot argument is ignored."""

    def __init__(self, db_path: str, bot: Any = None):
        self.db_path = db_path
        self.bot = bot
        self._db: Optional[aiosqlite.Connection] = None
        self._db_lock: Optional[asyncio.Lock] = None
        self._tx_lock: Optional[asyncio.Lock] = None

    async def _get_db_lock(self) -> asyncio.Lock:
        if self._db_lock is None:
            self._db_lock = asyncio.Lock()
        return self._db_lock

    async def _get_tx_lock(self) -> asyncio.Lock:
        if self._tx_lock is None:
            self._tx_lock = asyncio.Lock()
        return self._tx_lock

    async def _ensure_connection(self) -> aiosqlite.Connection:
        db_lock = await self._get_db_lock()
        async with db_lock:
            if self._db is None:
                self._db = await aiosqlite.connect(self.db_path)
                self._db.row_factory = aiosqlite.Row
                await self._db.execute("PRAGMA foreign_keys = ON")
        return self._db

    async def _get_db(self) -> aiosqlite.Connection:
        db = await self._ensure_connection()
        if db is None:
            raise RuntimeError("Database connection is not initialized")
        return db

    async def close(self) -> None:
        db_lock = await self._get_db_lock()
        async with db_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    def _backup_target(self) -> str | None:
        if not self.db_path or self.db_path == ":memory:" or not os.path.exists(self.db_path):
            return None
        return f"{self.db_path}.migration-bak"

    async def setup_database(self) -> None:
        """Migrate, then ensure defaults. Failures restore the pre-migration backup."""
        backup_path = self._backup_target()
        if backup_path:
            backup_sqlite(self.db_path, backup_path)
        db = await self._get_db()
        tx_lock = await self._get_tx_lock()
        try:
            async with tx_lock:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await upgrade(db)
                    await self._ensure_defaults(db)
                    await self._interrupt_unfinished(db)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception:
            await self.close()
            if backup_path and os.path.exists(backup_path):
                restore_sqlite(backup_path, self.db_path)
            raise

    async def _ensure_defaults(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = 'model'")
        if await cursor.fetchone() is None:
            await db.execute(
                "INSERT INTO bot_config (key, value) VALUES ('model', ?)",
                (select_default_model(get_installed_models()),),
            )
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = 'system_prompt'")
        row = await cursor.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO bot_config (key, value) VALUES ('system_prompt', ?)",
                (DEFAULT_SYSTEM_PROMPT,),
            )
        elif str(row[0]) == LEGACY_DEFAULT_SYSTEM_PROMPT:
            await db.execute(
                "UPDATE bot_config SET value = ? WHERE key = 'system_prompt'",
                (DEFAULT_SYSTEM_PROMPT,),
            )
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = 'automation_timezone'")
        if await cursor.fetchone() is None:
            from config import AUTOMATION_TIMEZONE

            await db.execute(
                "INSERT INTO bot_config (key, value) VALUES ('automation_timezone', ?)",
                (AUTOMATION_TIMEZONE,),
            )

    async def _interrupt_unfinished(self, db: aiosqlite.Connection) -> None:
        now = utc_now()
        await db.execute(
            "UPDATE chat_history SET status = 'interrupted' WHERE status = 'pending'"
        )
        await db.execute(
            """
            UPDATE runs
            SET status = 'interrupted', error_code = 'interrupted', finished_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize multi-statement writes. The connection lock alone does not."""
        db = await self._get_db()
        tx_lock = await self._get_tx_lock()
        async with tx_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    def _check_input(self, content: str) -> str:
        text = str(content or "")
        if len(text) > MAX_INPUT_CHARS:
            raise InputTooLong(
                f"Content exceeds the {MAX_INPUT_CHARS} character input limit."
            )
        return text

    async def update_config(self, key: str, value: str) -> None:
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    async def get_config(self, key: str, default: str) -> str:
        db = await self._get_db()
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if not row or row[0] is None or str(row[0]) == "":
            await self.update_config(key, default)
            return default
        return str(row[0])

    async def get_current_model(self) -> str:
        db = await self._get_db()
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = ?", ("model",))
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return ""
        return str(row[0])

    async def get_system_prompt(self) -> str:
        return await self.get_config("system_prompt", DEFAULT_SYSTEM_PROMPT)

    def format_db_size(self) -> str:
        if not os.path.exists(self.db_path):
            return "0 KB"
        size_bytes = os.path.getsize(self.db_path)
        size_kb = size_bytes / 1024
        if size_kb >= 1024:
            return f"{size_kb / 1024:.2f} MB"
        return f"{size_kb:.2f} KB"

    async def create_thread(self, title: str | None = None) -> dict[str, Any]:
        now = utc_now()
        thread_id = uuid.uuid4().hex
        stored_title = (title or "").strip() or "New conversation"
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO threads (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, stored_title, now, now),
            )
        return {"id": thread_id, "title": stored_title, "created_at": now, "updated_at": now}

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_threads(self, *, limit: int = 50, before: str | None = None) -> dict[str, Any]:
        db = await self._get_db()
        params: list[Any] = []
        where = ""
        if before:
            cursor = await db.execute(
                "SELECT updated_at, id FROM threads WHERE id = ?",
                (before,),
            )
            marker = await cursor.fetchone()
            if marker:
                where = "WHERE (updated_at < ?) OR (updated_at = ? AND id < ?)"
                params.extend([marker[0], marker[0], marker[1]])
        params.append(limit + 1)
        cursor = await db.execute(
            f"""
            SELECT id, title, created_at, updated_at
            FROM threads
            {where}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        next_cursor = rows[limit]["id"] if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": next_cursor}

    async def _touch_thread(self, db: aiosqlite.Connection, thread_id: str, when: str) -> None:
        await db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (when, thread_id))

    async def insert_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        model: str | None = None,
        status: str = "complete",
        request_id: str | None = None,
        legacy_user_id: str | None = None,
        enforce_limit: bool = True,
    ) -> dict[str, Any]:
        if enforce_limit and role == "user":
            content = self._check_input(content)
        else:
            content = str(content or "")
        if request_id:
            existing = await self.get_message_by_request(thread_id, request_id, role)
            if existing:
                return existing
        now = utc_now()
        message_id = uuid.uuid4().hex
        async with self.transaction() as db:
            if request_id:
                cursor = await db.execute(
                    """
                    SELECT * FROM chat_history
                    WHERE thread_id = ? AND request_id = ? AND role = ?
                    """,
                    (thread_id, request_id, role),
                )
                row = await cursor.fetchone()
                if row:
                    return dict(row)
            await db.execute(
                """
                INSERT INTO chat_history
                    (id, thread_id, role, content, timestamp, model, status, request_id, legacy_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, thread_id, role, content, now, model, status, request_id, legacy_user_id),
            )
            await self._touch_thread(db, thread_id, now)
            if role == "user":
                cursor = await db.execute("SELECT title FROM threads WHERE id = ?", (thread_id,))
                title_row = await cursor.fetchone()
                if title_row and str(title_row[0]) == "New conversation":
                    title = content.strip().splitlines()[0][:80] if content.strip() else "New conversation"
                    await db.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
        return await self.get_message(message_id) or {}

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute("SELECT * FROM chat_history WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_message_by_request(self, thread_id: str, request_id: str, role: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT * FROM chat_history
            WHERE thread_id = ? AND request_id = ? AND role = ?
            """,
            (thread_id, request_id, role),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_messages(self, thread_id: str, *, limit: int = 100, before: str | None = None) -> dict[str, Any]:
        db = await self._get_db()
        params: list[Any] = [thread_id]
        where = "WHERE thread_id = ?"
        if before:
            cursor = await db.execute(
                "SELECT timestamp, rowid FROM chat_history WHERE id = ? AND thread_id = ?",
                (before, thread_id),
            )
            marker = await cursor.fetchone()
            if marker:
                where += " AND (timestamp < ? OR (timestamp = ? AND rowid < ?))"
                params.extend([marker[0], marker[0], marker[1]])
        params.append(limit + 1)
        cursor = await db.execute(
            f"""
            SELECT * FROM chat_history
            {where}
            ORDER BY timestamp DESC, rowid DESC
            LIMIT ?
            """,
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        next_cursor = rows[limit]["id"] if len(rows) > limit else None
        items = list(reversed(rows[:limit]))
        return {"items": items, "next_cursor": next_cursor}

    async def update_message_status(self, message_id: str, status: str, content: str | None = None) -> None:
        async with self.transaction() as db:
            if content is None:
                await db.execute(
                    "UPDATE chat_history SET status = ? WHERE id = ?",
                    (status, message_id),
                )
            else:
                await db.execute(
                    "UPDATE chat_history SET status = ?, content = ? WHERE id = ?",
                    (status, content, message_id),
                )

    async def has_active_turn(self, thread_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT 1 FROM chat_history WHERE thread_id = ? AND status = 'pending' LIMIT 1",
            (thread_id,),
        )
        return await cursor.fetchone() is not None

    async def clear_thread_messages(self, thread_id: str) -> None:
        async with self.transaction() as db:
            await db.execute("DELETE FROM chat_history WHERE thread_id = ?", (thread_id,))
            await db.execute(
                """
                UPDATE threads
                SET summary = NULL, summary_boundary_message_id = NULL, context_start_message_id = NULL
                WHERE id = ?
                """,
                (thread_id,),
            )

    async def reset_context(self, thread_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT id FROM chat_history
            WHERE thread_id = ?
            ORDER BY timestamp DESC, rowid DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        row = await cursor.fetchone()
        boundary = str(row[0]) if row else None
        async with self.transaction() as tx:
            await tx.execute(
                """
                UPDATE threads
                SET context_start_message_id = ?, summary = NULL, summary_boundary_message_id = NULL
                WHERE id = ?
                """,
                (boundary, thread_id),
            )
        return await self.get_thread(thread_id)

    async def messages_for_context(self, thread_id: str, limit: int = 12) -> list[dict[str, str]]:
        thread = await self.get_thread(thread_id)
        db = await self._get_db()
        params: list[Any] = [thread_id]
        boundary_clause = ""
        if thread and thread.get("context_start_message_id"):
            cursor = await db.execute(
                "SELECT timestamp, rowid FROM chat_history WHERE id = ?",
                (thread["context_start_message_id"],),
            )
            marker = await cursor.fetchone()
            if marker:
                boundary_clause = "AND (timestamp > ? OR (timestamp = ? AND rowid >= ?))"
                params.extend([marker[0], marker[0], marker[1]])
        params.append(limit)
        cursor = await db.execute(
            f"""
            SELECT role, content FROM (
                SELECT role, content, timestamp, rowid
                FROM chat_history
                WHERE thread_id = ? AND status = 'complete' {boundary_clause}
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, rowid ASC
            """,
            params,
        )
        return [{"role": str(row[0]), "content": str(row[1])} for row in await cursor.fetchall()]

    async def _thread_for_user(self, user_id: str) -> str:
        legacy_user = str(user_id or "")
        thread_id = f"legacy-{legacy_user or 'anonymous'}"
        existing = await self.get_thread(thread_id)
        if existing:
            return thread_id
        now = utc_now()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO threads (id, title, created_at, updated_at)
                VALUES (?, 'Imported conversation', ?, ?)
                """,
                (thread_id, now, now),
            )
        return thread_id

    async def insert_history_message(self, user_id: str, role: str, content: str) -> None:
        normalized_user_id = str(user_id or "")
        content = self._check_input(content)
        thread_id = await self._thread_for_user(normalized_user_id)
        await self.insert_message(
            thread_id,
            str(role or ""),
            content,
            legacy_user_id=normalized_user_id,
            enforce_limit=False,
        )

    async def get_user_recent_history(self, user_id: str, limit: int = 10) -> list[dict[str, str]]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return []
        return await self._history_for_user(normalized_user_id, limit)

    async def get_chat_history(self, user_id: Optional[str] = None, limit: int = 30) -> list[dict[str, str]]:
        if user_id is not None:
            normalized_user_id = str(user_id).strip()
            if not normalized_user_id:
                return []
            return await self._history_for_user(normalized_user_id, limit)
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, timestamp, rowid
                FROM chat_history
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, rowid ASC
            """,
            (limit,),
        )
        return [{"role": str(row[0]), "content": str(row[1])} for row in await cursor.fetchall()]

    async def _history_for_user(self, user_id: str, limit: int) -> list[dict[str, str]]:
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, timestamp, rowid
                FROM chat_history
                WHERE legacy_user_id = ?
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, rowid ASC
            """,
            (user_id, limit),
        )
        return [{"role": str(row[0]), "content": str(row[1])} for row in await cursor.fetchall()]

    async def get_recent_history(self) -> list[dict[str, str]]:
        return await self.get_chat_history(limit=20)

    async def clear_chat_history(self, user_id: str) -> None:
        thread_id = f"legacy-{str(user_id or '') or 'anonymous'}"
        if await self.get_thread(thread_id):
            await self.clear_thread_messages(thread_id)

    async def get_next_broadcast(self) -> Optional[tuple[int, str, Optional[int]]]:
        db = await self._get_db()
        try:
            cursor = await db.execute(
                "SELECT id, content, channel_id FROM broadcast_queue ORDER BY id ASC LIMIT 1"
            )
        except aiosqlite.OperationalError:
            return None
        row = await cursor.fetchone()
        if not row:
            return None
        channel_id = row[2] if row[2] is not None else None
        return int(row[0]), str(row[1] or "").strip(), channel_id

    async def delete_broadcast(self, message_id: int) -> None:
        async with self.transaction() as db:
            try:
                await db.execute("DELETE FROM broadcast_queue WHERE id = ?", (message_id,))
            except aiosqlite.OperationalError:
                return

    async def save_fact(
        self,
        fact: str,
        *,
        source_message_id: str | None = None,
        idempotency_key: str | None = None,
        palace_status: str | None = None,
    ) -> dict[str, Any]:
        content = self._check_input(fact)
        if idempotency_key:
            existing = await self.get_fact_by_key(idempotency_key)
            if existing:
                return existing
        async with self.transaction() as db:
            if idempotency_key:
                cursor = await db.execute(
                    "SELECT * FROM facts WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = await cursor.fetchone()
                if row:
                    return _fact_record(row)
            cursor = await db.execute(
                """
                INSERT INTO facts (fact, source_message_id, idempotency_key, palace_status)
                VALUES (?, ?, ?, ?)
                """,
                (content, source_message_id, idempotency_key, palace_status),
            )
            fact_id = cursor.lastrowid
        return await self.get_fact(int(fact_id or 0)) or {}

    async def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,))
        row = await cursor.fetchone()
        return _fact_record(row) if row else None

    async def get_fact_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM facts WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        row = await cursor.fetchone()
        return _fact_record(row) if row else None

    async def set_fact_palace_status(self, fact_id: int, status: str, reference: str | None = None) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE facts SET palace_status = ?, palace_reference = ? WHERE id = ?",
                (status, reference, fact_id),
            )

    async def list_facts(self, *, limit: int = 50, before: str | None = None) -> dict[str, Any]:
        db = await self._get_db()
        params: list[Any] = []
        where = ""
        if before:
            where = "WHERE id < ?"
            params.append(int(before))
        params.append(limit + 1)
        cursor = await db.execute(
            f"SELECT * FROM facts {where} ORDER BY id DESC LIMIT ?",
            params,
        )
        rows = [_fact_record(row) for row in await cursor.fetchall()]
        next_cursor = str(rows[limit]["id"]) if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": next_cursor}

    async def get_all_facts(self) -> list[str]:
        listed = await self.list_facts(limit=1000)
        return [item["content"] for item in listed["items"]]

    async def create_run(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
        status: str = "running",
    ) -> dict[str, Any]:
        if request_id:
            existing = await self.get_run_by_request(request_id)
            if existing:
                return existing
        run_id = uuid.uuid4().hex
        now = utc_now()
        payload = json.dumps(arguments or {}, sort_keys=True)
        async with self.transaction() as db:
            if request_id:
                cursor = await db.execute("SELECT * FROM runs WHERE request_id = ?", (request_id,))
                row = await cursor.fetchone()
                if row:
                    return _run_record(row)
            await db.execute(
                """
                INSERT INTO runs
                    (id, job_id, tool_name, arguments_json, status, started_at, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, job_id, tool_name, payload, status, now, request_id),
            )
        return await self.get_run(run_id) or {}

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        output: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE runs
                SET status = ?, output = ?, error_code = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, output, error_code, utc_now(), run_id),
            )
        return await self.get_run(run_id)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        record = _run_record(row)
        record["sources"] = await self.list_sources(run_id=run_id)
        return record

    async def get_run_by_request(self, request_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute("SELECT id FROM runs WHERE request_id = ?", (request_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return await self.get_run(str(row[0]))

    async def list_runs_for_job(self, job_id: str, *, limit: int = 50, before: str | None = None) -> dict[str, Any]:
        db = await self._get_db()
        params: list[Any] = [job_id]
        where = "WHERE job_id = ?"
        if before:
            where += " AND id < ?"
            params.append(before)
        params.append(limit + 1)
        cursor = await db.execute(
            f"SELECT * FROM runs {where} ORDER BY started_at DESC, id DESC LIMIT ?",
            params,
        )
        rows = [_run_record(row) for row in await cursor.fetchall()]
        next_cursor = rows[limit]["id"] if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": next_cursor}

    async def add_source(
        self,
        *,
        kind: str,
        excerpt: str,
        message_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        record_id: str | None = None,
        external_url: str | None = None,
    ) -> dict[str, Any]:
        source_id = uuid.uuid4().hex
        now = utc_now()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO sources
                    (id, message_id, run_id, kind, title, excerpt, record_id, retrieved_at, external_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, message_id, run_id, kind, title, excerpt, record_id, now, external_url),
            )
        return {
            "id": source_id,
            "kind": kind,
            "title": title,
            "excerpt": excerpt,
            "record_id": record_id,
            "retrieved_at": now,
            "external_url": external_url,
        }

    async def list_sources(self, *, message_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        db = await self._get_db()
        if message_id:
            cursor = await db.execute(
                "SELECT * FROM sources WHERE message_id = ? ORDER BY retrieved_at ASC, id ASC",
                (message_id,),
            )
        elif run_id:
            cursor = await db.execute(
                "SELECT * FROM sources WHERE run_id = ? ORDER BY retrieved_at ASC, id ASC",
                (run_id,),
            )
        else:
            return []
        return [_source_record(row) for row in await cursor.fetchall()]

    async def remember_import(self, source_key: str, *, status: str, palace_reference: str | None = None) -> None:
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO memory_imports (source_key, palace_reference, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    palace_reference = excluded.palace_reference,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (source_key, palace_reference, status, utc_now()),
            )

    async def get_import(self, source_key: str) -> dict[str, Any] | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM memory_imports WHERE source_key = ?",
            (source_key,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


def _fact_record(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "content": str(row["fact"]),
        "created_at": str(row["created_at"] or ""),
        "source_message_id": row["source_message_id"],
        "idempotency_key": row["idempotency_key"],
        "palace_status": row["palace_status"],
        "palace_reference": row["palace_reference"],
    }


def _run_record(row: aiosqlite.Row) -> dict[str, Any]:
    raw_args = row["arguments_json"] or "{}"
    try:
        arguments = json.loads(raw_args)
    except json.JSONDecodeError:
        arguments = {}
    return {
        "id": str(row["id"]),
        "job_id": row["job_id"],
        "tool_name": str(row["tool_name"]),
        "arguments": arguments,
        "status": str(row["status"]),
        "started_at": str(row["started_at"] or ""),
        "finished_at": row["finished_at"],
        "output": row["output"],
        "error_code": row["error_code"],
        "request_id": row["request_id"],
    }


def _source_record(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "title": row["title"],
        "excerpt": row["excerpt"],
        "record_id": row["record_id"],
        "retrieved_at": str(row["retrieved_at"]),
        "external_url": row["external_url"],
    }
