"""Evaluation suite for Mutiny Research closed-mode pipeline against seeded fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unittest

from core.research import run_research

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FACTS_PATH = FIXTURES_DIR / "research_eval_facts.json"
CASES_PATH = FIXTURES_DIR / "research_eval_cases.json"

FACTS: list[dict[str, Any]] = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
CASES_BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in CASES}


class FakeDB:
    def __init__(self, facts: list[dict[str, Any]], model: str = "eval-model:latest") -> None:
        self.facts = list(facts)
        self.model = model

    async def list_facts(self, limit: int = 200) -> dict[str, Any]:
        return {"items": self.facts[:limit]}

    async def get_current_model(self) -> str:
        return self.model


class FakePalace:
    available = False

    def search(self, query: str, limit: int = 20) -> list[Any]:
        return []


class FakeLLM:
    def __init__(self) -> None:
        self.rewrite_calls = 0
        self.writer_calls = 0

    async def generate_response(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        system_content = messages[0]["content"] if messages else ""
        if "keyword search phrases" in system_content.lower():
            self.rewrite_calls += 1
            user_content = messages[1]["content"] if len(messages) > 1 else ""
            return json.dumps([user_content])

        # Writer call
        self.writer_calls += 1
        user_content = messages[1]["content"] if len(messages) > 1 else ""
        return f"According to the notes: {user_content[:80]}"


class ResearchEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_fixtures_shape_and_count(self) -> None:
        self.assertTrue(10 <= len(FACTS) <= 15, f"Expected 10-15 facts, got {len(FACTS)}")
        self.assertEqual(len(CASES), 20, f"Expected 20 cases, got {len(CASES)}")

        for fact in FACTS:
            self.assertIn("id", fact)
            self.assertIsInstance(fact["id"], int)
            self.assertIn("content", fact)
            self.assertNotIn("http://", fact["content"].lower())
            self.assertNotIn("https://", fact["content"].lower())

        for case in CASES:
            self.assertIn("id", case)
            self.assertIn("question", case)
            self.assertIn(case["expect"], {"hit", "miss"})
            self.assertIn("must_not_contain", case)
            self.assertIn("source_record_ids", case)
            if case["expect"] == "miss":
                self.assertEqual(case["source_record_ids"], [])
            else:
                self.assertTrue(len(case["source_record_ids"]) > 0)

    async def _evaluate_case(self, case_id: str) -> None:
        case = CASES_BY_ID[case_id]
        db = FakeDB(FACTS)
        palace = FakePalace()
        llm = FakeLLM()

        result = await run_research(
            db,
            palace,
            llm,
            question=case["question"],
            mode="closed",
        )

        # Baseline contract assertions for all closed research runs
        self.assertEqual(result["mode"], "closed")
        self.assertEqual(result["writer"], "local")
        self.assertEqual(result["question"], case["question"])

        for source in result["sources"]:
            self.assertIsNone(source["external_url"])
            self.assertIn(source["kind"], {"fact", "memory"})

        for forbidden in case.get("must_not_contain", ["http://", "https://"]):
            self.assertNotIn(forbidden, result["answer"].lower())

        if case["expect"] == "hit":
            retrieved_ids = [s["record_id"] for s in result["sources"]]
            expected_ids = case["source_record_ids"]
            self.assertTrue(
                any(eid in retrieved_ids for eid in expected_ids),
                f"Case '{case_id}': expected at least one of {expected_ids} in {retrieved_ids}",
            )
            self.assertNotIn("http://", result["answer"].lower())
            self.assertNotIn("https://", result["answer"].lower())
            self.assertGreaterEqual(
                llm.writer_calls, 1, f"Case '{case_id}': writer should have been called for hit"
            )
        elif case["expect"] == "miss":
            self.assertEqual(
                result["sources"], [], f"Case '{case_id}': expected zero sources for miss"
            )
            self.assertGreater(
                len(result["gaps"]), 0, f"Case '{case_id}': gaps must be non-empty for miss"
            )
            self.assertNotIn("http://", result["answer"].lower())
            self.assertNotIn("https://", result["answer"].lower())
            self.assertEqual(
                llm.writer_calls, 0, f"Case '{case_id}': writer should be unused for miss"
            )
        else:
            self.fail(f"Unknown expect value: {case['expect']}")

    # 10 Hit evaluation cases
    async def test_eval_hit_loopback(self) -> None:
        await self._evaluate_case("hit-loopback")

    async def test_eval_hit_default_chat(self) -> None:
        await self._evaluate_case("hit-default-chat")

    async def test_eval_hit_sqlite_storage(self) -> None:
        await self._evaluate_case("hit-sqlite-storage")

    async def test_eval_hit_morning_briefing(self) -> None:
        await self._evaluate_case("hit-morning-briefing")

    async def test_eval_hit_ollama_inference(self) -> None:
        await self._evaluate_case("hit-ollama-inference")

    async def test_eval_hit_news_disabled(self) -> None:
        await self._evaluate_case("hit-news-disabled")

    async def test_eval_hit_httponly_cookie(self) -> None:
        await self._evaluate_case("hit-httponly-cookie")

    async def test_eval_hit_conversation_transcripts(self) -> None:
        await self._evaluate_case("hit-conversation-transcripts")

    async def test_eval_hit_mempalace_vector(self) -> None:
        await self._evaluate_case("hit-mempalace-vector")

    async def test_eval_hit_research_mode(self) -> None:
        await self._evaluate_case("hit-research-mode")

    # 10 Miss evaluation cases (absent topics, decoys, stopwords)
    async def test_eval_miss_absent_weather(self) -> None:
        await self._evaluate_case("miss-absent-weather")

    async def test_eval_miss_absent_stock_market(self) -> None:
        await self._evaluate_case("miss-absent-stock-market")

    async def test_eval_miss_absent_recipe(self) -> None:
        await self._evaluate_case("miss-absent-recipe")

    async def test_eval_miss_absent_astronomy(self) -> None:
        await self._evaluate_case("miss-absent-astronomy")

    async def test_eval_miss_absent_gardening(self) -> None:
        await self._evaluate_case("miss-absent-gardening")

    async def test_eval_miss_decoy_kubernetes(self) -> None:
        await self._evaluate_case("miss-decoy-kubernetes")

    async def test_eval_miss_decoy_crypto(self) -> None:
        await self._evaluate_case("miss-decoy-crypto")

    async def test_eval_miss_decoy_bluetooth(self) -> None:
        await self._evaluate_case("miss-decoy-bluetooth")

    async def test_eval_miss_stopword_short_1(self) -> None:
        await self._evaluate_case("miss-stopword-short-1")

    async def test_eval_miss_stopword_short_2(self) -> None:
        await self._evaluate_case("miss-stopword-short-2")

    async def test_eval_hit_local_document(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = Path(tmpdir)
            doc_file = docs_dir / "diagnostics.md"
            doc_file.write_text(
                "# Local Diagnostics\nDiagnostic probes run strictly in-process without socket binds.\n\n"
                "## Telemetry Policy\nAll telemetry collection is permanently disabled in the console."
            )
            db = FakeDB(FACTS)
            palace = FakePalace()
            llm = FakeLLM()

            result = await run_research(
                db,
                palace,
                llm,
                question="What is the telemetry policy?",
                mode="closed",
                docs_path=docs_dir,
            )

            self.assertEqual(result["mode"], "closed")
            self.assertEqual(result["writer"], "local")
            self.assertTrue(len(result["sources"]) > 0)
            doc_source = next((s for s in result["sources"] if s["kind"] == "document"), None)
            self.assertIsNotNone(doc_source)
            self.assertEqual(doc_source["title"], "Telemetry Policy")
            self.assertEqual(doc_source["record_id"], "diagnostics.md#Telemetry Policy")
            self.assertIn("All telemetry collection is permanently disabled", doc_source["excerpt"])
            self.assertIsNone(doc_source["external_url"])
            self.assertNotIn("http://", result["answer"].lower())
            self.assertNotIn("https://", result["answer"].lower())
            self.assertGreaterEqual(llm.writer_calls, 1)


if __name__ == "__main__":
    unittest.main()
