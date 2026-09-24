"""MemPalace adapter normalization and failure isolation."""

import unittest

from memory.palace import MemoryHit, PalaceAdapter, normalize_search_results


class NormalizeSearchTests(unittest.TestCase):
    def test_empty_and_list_and_dict_shapes(self) -> None:
        self.assertEqual(normalize_search_results(None), [])
        self.assertEqual(normalize_search_results({"results": []}), [])
        self.assertEqual(
            normalize_search_results(["  note  "]),
            [MemoryHit(text="note")],
        )
        self.assertEqual(
            normalize_search_results({"results": [{"text": "alpha", "id": "1"}]}),
            [MemoryHit(text="alpha", record_id="1")],
        )

    def test_arbitrary_dict_is_not_a_source(self) -> None:
        self.assertEqual(normalize_search_results([{"score": 0.2, "meta": {"a": 1}}]), [])
        self.assertEqual(normalize_search_results({"unexpected": True}), [])


class PalaceAdapterTests(unittest.TestCase):
    def test_missing_package_is_unavailable(self) -> None:
        def _boom():
            raise ImportError("mempalace missing")

        adapter = PalaceAdapter("/tmp/palace", assets_ready=True, importer=_boom)
        self.assertFalse(adapter.available)
        self.assertIn("import failed", adapter.degraded_reason or "")
        self.assertEqual(adapter.search("hello"), [])
        self.assertEqual(adapter.index_fact("1", "hello"), "unavailable")

    def test_init_without_assets_does_not_import(self) -> None:
        called = {"count": 0}

        def _importer():
            called["count"] += 1
            return (lambda *args, **kwargs: [], lambda **kwargs: {"success": True})

        adapter = PalaceAdapter("/tmp/palace", assets_ready=False, importer=_importer)
        self.assertEqual(adapter.degraded_reason, "embedding assets are not cached locally")
        self.assertEqual(called["count"], 0)
        self.assertEqual(adapter.index_fact("1", "fact"), "unavailable")

    def test_write_failure_is_separate_from_search(self) -> None:
        def _search(*_args, **_kwargs):
            return {"results": [{"content": "saved"}]}

        def _add(**_kwargs):
            raise RuntimeError("disk full")

        adapter = PalaceAdapter("/tmp/palace", search_fn=_search, add_fn=_add, assets_ready=True)
        self.assertEqual(adapter.search("saved"), [MemoryHit(text="saved")])
        self.assertEqual(adapter.index_fact("9", "saved"), "failed")

    def test_duplicate_write_is_indexed(self) -> None:
        adapter = PalaceAdapter(
            "/tmp/palace",
            search_fn=lambda *args, **kwargs: [],
            add_fn=lambda **kwargs: {"success": False, "reason": "duplicate"},
            assets_ready=True,
        )
        self.assertEqual(adapter.index_fact("9", "saved"), "indexed")

    def test_search_error_dict_returns_no_hits(self) -> None:
        adapter = PalaceAdapter(
            "/tmp/palace",
            search_fn=lambda *args, **kwargs: {"error": "no palace"},
            add_fn=lambda **kwargs: {"success": True},
            assets_ready=True,
        )
        self.assertEqual(adapter.search("q"), [])
