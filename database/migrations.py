"""Versioned SQLite migrations for the local console.

Back up the database with the SQLite backup API before migrating a file, and
keep the whole upgrade inside the caller's transaction so a failure rolls back.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from config import DEFAULT_SYSTEM_PROMPT, LEGACY_DEFAULT_SYSTEM_PROMPT

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_sqlite(source_path: str, dest_path: str) -> None:
    """Copy a live database consistently. Do not use a plain file copy."""
    source = sqlite3.connect(source_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def restore_sqlite(backup_path: str, dest_path: str) -> None:
    backup_sqlite(backup_path, dest_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(dest_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def _tables(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    return {str(row[0]) for row in rows}


async def current_version(db: aiosqlite.Connection) -> int:
    tables = await _tables(db)
    if "schema_migrations" not in tables:
        return 0
    cursor = await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    row = await cursor.fetchone()
    return int(row[0] or 0)


async def _create_current_tables(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            context_start_message_id TEXT,
            summary TEXT,
            summary_boundary_message_id TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'complete',
            request_id TEXT,
            legacy_user_id TEXT,
            FOREIGN KEY (thread_id) REFERENCES threads(id)
        )
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
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source_message_id TEXT,
            idempotency_key TEXT UNIQUE,
            palace_status TEXT,
            palace_reference TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            tool_name TEXT NOT NULL,
            arguments_json TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            output TEXT,
            error_code TEXT,
            request_id TEXT UNIQUE
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            run_id TEXT,
            kind TEXT NOT NULL,
            title TEXT,
            excerpt TEXT,
            record_id TEXT,
            retrieved_at TEXT NOT NULL,
            external_url TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_imports (
            source_key TEXT PRIMARY KEY,
            palace_reference TEXT,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_request_role
        ON chat_history (thread_id, request_id, role)
        WHERE request_id IS NOT NULL
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_history_thread_timestamp
        ON chat_history (thread_id, timestamp)
        """
    )


async def _migrate_legacy(db: aiosqlite.Connection) -> None:
    tables = await _tables(db)
    history_cols = await _columns(db, "chat_history") if "chat_history" in tables else set()
    if "thread_id" in history_cols:
        await _ensure_added_columns(db)
        return

    if "chat_history" in tables:
        await db.execute("ALTER TABLE chat_history RENAME TO chat_history_legacy")
    await _create_current_tables(db)

    if "chat_history_legacy" in await _tables(db):
        cursor = await db.execute(
            """
            SELECT rowid, user_id, role, content, timestamp
            FROM chat_history_legacy
            ORDER BY timestamp ASC, rowid ASC
            """
        )
        rows = await cursor.fetchall()
        threads: dict[str, str] = {}
        now = utc_now()
        for rowid, user_id, role, content, timestamp in rows:
            legacy_user = "" if user_id is None else str(user_id)
            thread_id = threads.get(legacy_user)
            if thread_id is None:
                thread_id = f"legacy-{legacy_user or 'anonymous'}"
                threads[legacy_user] = thread_id
                await db.execute(
                    """
                    INSERT OR IGNORE INTO threads (id, title, created_at, updated_at)
                    VALUES (?, 'Imported conversation', ?, ?)
                    """,
                    (thread_id, str(timestamp or now), str(timestamp or now)),
                )
            await db.execute(
                """
                INSERT OR IGNORE INTO chat_history
                    (id, thread_id, role, content, timestamp, status, legacy_user_id)
                VALUES (?, ?, ?, ?, ?, 'complete', ?)
                """,
                (
                    f"legacy-msg-{int(rowid)}",
                    thread_id,
                    str(role or ""),
                    str(content or ""),
                    str(timestamp or now),
                    legacy_user,
                ),
            )
            await db.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?",
                (str(timestamp or now), thread_id),
            )
        await db.execute("DROP TABLE chat_history_legacy")

    if "facts" in tables:
        fact_cols = await _columns(db, "facts")
        for column_name in ("source_message_id", "idempotency_key", "palace_status", "palace_reference"):
            if column_name not in fact_cols:
                await db.execute(f"ALTER TABLE facts ADD COLUMN {column_name} TEXT")
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_idempotency
            ON facts (idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
    else:
        # Created by _create_current_tables above only when we always call it.
        pass

    if "broadcast_queue" in tables:
        cursor = await db.execute(
            "SELECT id, content FROM broadcast_queue ORDER BY id ASC"
        )
        queued = await cursor.fetchall()
        for queue_id, content in queued:
            await db.execute(
                """
                INSERT OR IGNORE INTO runs
                    (id, job_id, tool_name, arguments_json, status, started_at, finished_at, output)
                VALUES (?, NULL, 'legacy_broadcast', '{}', 'complete', ?, ?, ?)
                """,
                (f"legacy-broadcast-{int(queue_id)}", utc_now(), utc_now(), str(content or "")),
            )

    await _replace_legacy_prompt(db)


async def _ensure_added_columns(db: aiosqlite.Connection) -> None:
    """Repair a partially created current schema."""
    await _create_current_tables(db)


async def _replace_legacy_prompt(db: aiosqlite.Connection) -> None:
    tables = await _tables(db)
    if "bot_config" not in tables:
        return
    cursor = await db.execute("SELECT value FROM bot_config WHERE key = 'system_prompt'")
    row = await cursor.fetchone()
    if row and str(row[0]) == LEGACY_DEFAULT_SYSTEM_PROMPT:
        await db.execute(
            "UPDATE bot_config SET value = ? WHERE key = 'system_prompt'",
            (DEFAULT_SYSTEM_PROMPT,),
        )


async def upgrade(db: aiosqlite.Connection) -> None:
    """Advance the open connection to SCHEMA_VERSION. Caller owns the transaction."""
    version = await current_version(db)
    if version >= SCHEMA_VERSION:
        await _create_current_tables(db)
        return

    tables = await _tables(db)
    if "chat_history" in tables:
        cols = await _columns(db, "chat_history")
        if "thread_id" not in cols:
            await _migrate_legacy(db)
        else:
            await _create_current_tables(db)
            await _replace_legacy_prompt(db)
    else:
        await _create_current_tables(db)

    await _replace_legacy_prompt(db)
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )
