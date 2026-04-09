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


if __name__ == "__main__":
    unittest.main()
