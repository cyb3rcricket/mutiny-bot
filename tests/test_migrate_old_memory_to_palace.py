"""Security regression tests for SQLite migration query construction."""

import sys
import types
import unittest

stub_kg_module = types.ModuleType("mempalace.knowledge_graph")
stub_kg_module.KnowledgeGraph = object
stub_server_module = types.ModuleType("mempalace.mcp_server")
stub_server_module.tool_add_drawer = lambda **kwargs: {"success": True}
stub_package = types.ModuleType("mempalace")

sys.modules.setdefault("mempalace", stub_package)
sys.modules.setdefault("mempalace.knowledge_graph", stub_kg_module)
sys.modules.setdefault("mempalace.mcp_server", stub_server_module)

from scripts.migrate_old_memory_to_palace import _build_select_query, _quote_sqlite_identifier


class MigrationQuerySafetyTests(unittest.TestCase):
    def test_quote_sqlite_identifier_accepts_valid_name(self) -> None:
        self.assertEqual(_quote_sqlite_identifier("chat_history"), '"chat_history"')

    def test_quote_sqlite_identifier_rejects_injection_like_input(self) -> None:
        with self.assertRaises(ValueError):
            _quote_sqlite_identifier("notes; DROP TABLE notes;--")

    def test_build_select_query_quotes_all_identifiers(self) -> None:
        query = _build_select_query("chat_history", ["content", "user_id"])
        self.assertEqual(
            query,
            'SELECT "content", "user_id" FROM "chat_history" ORDER BY rowid ASC',
        )


class RepeatImportTests(unittest.TestCase):
    def test_second_run_skips_rows_already_checkpointed(self) -> None:
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch

        from scripts import migrate_old_memory_to_palace as mod

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            palace = os.path.join(tmp, "palace")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE chat_history (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    role TEXT,
                    legacy_user_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE memory_imports (
                    source_key TEXT PRIMARY KEY,
                    palace_reference TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO chat_history (id, content, role, legacy_user_id) VALUES ('m1', 'hello local', 'user', 'legacy-user')"
            )
            conn.commit()
            conn.close()
            calls: list[dict] = []

            def add_drawer(**kwargs):
                calls.append(kwargs)
                return {"success": True}

            with patch.object(mod, "tool_add_drawer", add_drawer), patch.object(mod, "KnowledgeGraph", None):
                first = mod.migrate(db_path, palace, dry_run=False)
                second = mod.migrate(db_path, palace, dry_run=False)

        self.assertEqual(first.conversations, 1)
        self.assertEqual(second.conversations, 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
