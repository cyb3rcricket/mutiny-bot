"""Migrate legacy MutinyBot SQLite memory into MemPalace drawers.

Run with:
    python -m scripts.migrate_old_memory_to_palace
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "mutiny.db"
DEFAULT_PALACE_PATH = os.path.expanduser("~/.mutiny/palace")
DEFAULT_CHAT_WING = "Legacy"
DEFAULT_CHAT_ROOM = "general"
DEFAULT_NOTES_WING = "Legacy"
DEFAULT_NOTES_ROOM = "remembered-facts"


try:
    from mempalace.knowledge_graph import KnowledgeGraph
except ImportError:
    KnowledgeGraph = None
try:
    from mempalace.mcp_server import tool_add_drawer
except ImportError:
    tool_add_drawer = None

_ADD_DRAWER_PARAMS = (
    set(inspect.signature(tool_add_drawer).parameters)
    if callable(tool_add_drawer)
    else set()
)


@dataclass
class MigrationStats:
    """Track migration counts for final CLI output."""

    conversations: int = 0
    notes: int = 0
    skipped: int = 0


def _get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute("SELECT name FROM pragma_table_info(?)", (table_name,)).fetchall()
    return {str(r[0]) for r in rows}


def _quote_sqlite_identifier(identifier: str) -> str:
    """Return a safely quoted SQLite identifier for trusted schema names."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier or ""):
        raise ValueError(f"Unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _build_select_query(table_name: str, columns: list[str]) -> str:
    quoted_table = _quote_sqlite_identifier(table_name)
    quoted_columns = [_quote_sqlite_identifier(col) for col in columns]
    return f"SELECT {', '.join(quoted_columns)} FROM {quoted_table} ORDER BY rowid ASC"  # nosec B608


def _normalize_name(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _ensure_wing_and_room(graph: Any, wing: str, room: str) -> None:
    if not graph:
        return

    ensure_wing = getattr(graph, "ensure_wing", None)
    ensure_room = getattr(graph, "ensure_room", None)

    if callable(ensure_wing):
        ensure_wing(wing)
    if callable(ensure_room):
        ensure_room(wing, room)


def _store_drawer(palace_path: str, wing: str, room: str, content: str, metadata: dict[str, Any]) -> None:
    if not callable(tool_add_drawer):
        raise RuntimeError(
            "MemPalace MCP server integration is unavailable. Install MemPalace to run migration writes."
        )

    # Keep custom palace target working on MemPalace variants that read path from env.
    os.environ["MEMPALACE_PALACE_PATH"] = palace_path

    if "palace_path" in _ADD_DRAWER_PARAMS:
        kwargs: dict[str, Any] = {
            "palace_path": palace_path,
            "wing": wing,
            "room": room,
            "content": content,
        }
        if "metadata" in _ADD_DRAWER_PARAMS:
            kwargs["metadata"] = metadata
        result = tool_add_drawer(**kwargs)
    else:
        result = tool_add_drawer(
            wing=wing,
            room=room,
            content=content,
            source_file=str(metadata.get("table", "")),
            added_by="migration",
        )

    if isinstance(result, dict) and result.get("success") is False:
        if result.get("reason") == "duplicate":
            return
        raise RuntimeError(result.get("error") or "Failed to add drawer")


def _migrate_chat_history(conn: sqlite3.Connection, palace_path: str, graph: Any, dry_run: bool = False) -> int:
    tables = set(_table_names(conn))
    if "chat_history" not in tables:
        return 0

    columns = _table_columns(conn, "chat_history")
    select_cols = ["content"]
    optional_cols = ["role", "user_id", "timestamp", "guild", "guild_name", "channel", "channel_name"]
    for col in optional_cols:
        if col in columns:
            select_cols.append(col)

    query = _build_select_query("chat_history", select_cols)
    rows = conn.execute(query).fetchall()

    migrated = 0
    for row in rows:
        content = str(row["content"] or "").strip()
        if not content:
            continue

        guild_value = row["guild"] if "guild" in row.keys() else row["guild_name"] if "guild_name" in row.keys() else None
        channel_value = row["channel"] if "channel" in row.keys() else row["channel_name"] if "channel_name" in row.keys() else None

        wing = _normalize_name(guild_value, DEFAULT_CHAT_WING)
        room = _normalize_name(channel_value, DEFAULT_CHAT_ROOM)
        _ensure_wing_and_room(graph, wing, room)

        metadata = {
            "source": "sqlite-v1",
            "table": "chat_history",
            "role": row["role"] if "role" in row.keys() else "unknown",
            "user_id": row["user_id"] if "user_id" in row.keys() else None,
            "timestamp": row["timestamp"] if "timestamp" in row.keys() else None,
            "guild": wing,
            "channel": room,
        }

        if not dry_run:
            _store_drawer(
                palace_path=palace_path,
                wing=wing,
                room=room,
                content=content,
                metadata=metadata,
            )
        migrated += 1

    return migrated


def _looks_like_notes_table(table_name: str, columns: set[str]) -> bool:
    if table_name.lower() == "facts":
        return True

    has_note_col = bool({"fact", "note", "content", "text"}.intersection(columns))
    name_hint = any(token in table_name.lower() for token in ("fact", "note", "memory"))
    return has_note_col and name_hint


def _pick_note_content_column(columns: set[str]) -> str | None:
    for candidate in ("fact", "note", "content", "text"):
        if candidate in columns:
            return candidate
    return None


def _migrate_notes(conn: sqlite3.Connection, palace_path: str, graph: Any, dry_run: bool = False) -> int:
    migrated = 0
    for table_name in _table_names(conn):
        columns = _table_columns(conn, table_name)
        if not _looks_like_notes_table(table_name, columns):
            continue

        content_col = _pick_note_content_column(columns)
        if not content_col:
            continue

        select_cols = [content_col]
        for optional in ("created_at", "timestamp", "user_id", "guild", "guild_name", "channel", "channel_name"):
            if optional in columns:
                select_cols.append(optional)

        query = _build_select_query(table_name, select_cols)
        rows = conn.execute(query).fetchall()

        for row in rows:
            content = str(row[content_col] or "").strip()
            if not content:
                continue

            guild_value = row["guild"] if "guild" in row.keys() else row["guild_name"] if "guild_name" in row.keys() else None
            channel_value = row["channel"] if "channel" in row.keys() else row["channel_name"] if "channel_name" in row.keys() else None

            wing = _normalize_name(guild_value, DEFAULT_NOTES_WING)
            room = _normalize_name(channel_value, DEFAULT_NOTES_ROOM)
            _ensure_wing_and_room(graph, wing, room)

            metadata = {
                "source": "sqlite-v1",
                "table": table_name,
                "type": "note",
                "user_id": row["user_id"] if "user_id" in row.keys() else None,
                "timestamp": row["created_at"] if "created_at" in row.keys() else row["timestamp"] if "timestamp" in row.keys() else None,
                "guild": wing,
                "channel": room,
            }

            if not dry_run:
                _store_drawer(
                    palace_path=palace_path,
                    wing=wing,
                    room=room,
                    content=content,
                    metadata=metadata,
                )
            migrated += 1

    return migrated


def migrate(db_path: str, palace_path: str, dry_run: bool = False) -> MigrationStats:
    """Run one-way migration from legacy SQLite to MemPalace drawers."""
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    if not dry_run:
        if not callable(tool_add_drawer):
            raise RuntimeError(
                "MemPalace MCP server integration is unavailable. Install MemPalace to run migration."
            )
        os.makedirs(palace_path, exist_ok=True)
        os.environ["MEMPALACE_PALACE_PATH"] = palace_path

    graph = None
    if KnowledgeGraph is not None and not dry_run:
        kg_db_path = os.path.join(palace_path, "knowledge_graph.sqlite3")
        graph = KnowledgeGraph(kg_db_path)
    stats = MigrationStats()

    with _get_connection(str(db_file)) as conn:
        stats.conversations = _migrate_chat_history(conn, palace_path, graph, dry_run)
        stats.notes = _migrate_notes(conn, palace_path, graph, dry_run)

    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate MutinyBot V1 SQLite memory into MemPalace")
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to old SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--palace-path",
        default=DEFAULT_PALACE_PATH,
        help=f"Path to MemPalace directory (default: {DEFAULT_PALACE_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Count items that would be migrated without actually migrating",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.dry_run:
            db_path = os.path.abspath(args.db_path)
            print(f"DEBUG: Using database path: {db_path}")
            print(f"DEBUG: File exists? {os.path.exists(db_path)}")
            try:
                conn = sqlite3.connect(db_path)
            except sqlite3.OperationalError as e:
                print("Migration failed: unable to open database file")
                print(f"Full error: {e}")
                print(f"Path attempted: {db_path}")
                raise
            conn.close()
            stats = migrate(db_path=db_path, palace_path=args.palace_path, dry_run=True)
        else:
            db_path = os.path.abspath(args.db_path)
            print(f"DEBUG: Using database path: {db_path}")
            print(f"DEBUG: File exists? {os.path.exists(db_path)}")
            try:
                conn = sqlite3.connect(db_path)
            except sqlite3.OperationalError as e:
                print("Migration failed: unable to open database file")
                print(f"Full error: {e}")
                print(f"Path attempted: {db_path}")
                raise
            conn.close()
            stats = migrate(db_path=db_path, palace_path=args.palace_path, dry_run=False)
    except Exception as exc:
        print(f"Migration failed: {exc}")
        return 1

    if args.dry_run:
        print(f"Would migrate {stats.conversations} conversations and {stats.notes} facts")
    else:
        print("Migration complete.")
        print(f"- Conversations migrated: {stats.conversations}")
        print(f"- Notes migrated: {stats.notes}")
        print(f"- Palace path: {args.palace_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
