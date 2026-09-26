from pathlib import Path
import tempfile
from typing import Any
import unittest

from core.research import _parse_rewrite_queries, run_research
from memory.palace import MemoryHit


class FakeDB:
    def __init__(self, facts: list[dict[str, Any]] | None = None, model: str = "qwen2.5-coder:7b") -> None:
        self.facts = list(facts or [])
        self.model = model
        self.get_model_calls = 0

    async def list_facts(self, limit: int = 200) -> dict[str, Any]:
        return {"items": self.facts[:limit]}

    async def get_current_model(self) -> str:
        self.get_model_calls += 1
        return self.model


class FakePalace:
    def __init__(self, hits: list[Any] | None = None, available: bool = False) -> None:
        self.hits = list(hits or [])
        self.available = available

    def search(self, query: str, limit: int = 20) -> list[Any]:
        return self.hits[:limit]


class FakeLLM:
    def __init__(self, rewrite_reply: Any = None, writer_reply: Any = None) -> None:
        self.rewrite_reply = rewrite_reply
        self.writer_reply = writer_reply
        self.calls: list[dict[str, Any]] = []

    async def generate_response(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        # Distinguish between rewrite call and writer call
        system_text = ""
        for msg in messages:
            if msg.get("role") == "system":
                system_text = msg.get("content", "")
                break

        if "search phrase" in system_text.lower():
            if isinstance(self.rewrite_reply, Exception):
                raise self.rewrite_reply
            return self.rewrite_reply if self.rewrite_reply is not None else '["search phrase"]'

        if isinstance(self.writer_reply, Exception):
            raise self.writer_reply
        return (
            self.writer_reply
            if self.writer_reply is not None
            else "Mutiny is a personal local AI console."
        )


class ResearchPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_run_with_two_facts_returns_sources_and_no_external_url(self) -> None:
        db = FakeDB(
            facts=[
                {"id": 1, "content": "Mutiny runs models locally via Ollama."},
                {"id": 2, "content": "Default chat uses tools=None and loopback endpoints."},
            ]
        )
        palace = FakePalace(available=False)
        llm = FakeLLM(
            rewrite_reply='["Mutiny models", "default chat"]',
            writer_reply="According to the notes, Mutiny runs models locally and default chat uses tools=None.",
        )

        result = await run_research(db, palace, llm, question="How does Mutiny chat work?")

        self.assertEqual(result["mode"], "closed")
        self.assertEqual(result["writer"], "local")
        self.assertEqual(result["model"], "qwen2.5-coder:7b")
        self.assertEqual(result["question"], "How does Mutiny chat work?")
        self.assertEqual(result["queries"], ["Mutiny models", "default chat"])
        self.assertEqual(result["gaps"], [])
        self.assertIn("Mutiny runs models locally", result["answer"])

        self.assertEqual(len(result["sources"]), 2)
        for source in result["sources"]:
            self.assertEqual(source["kind"], "fact")
            self.assertIsNone(source["external_url"])
            self.assertIn(source["record_id"], {"1", "2"})
            self.assertTrue(len(source["excerpt"]) > 0)

        # Verify all LLM calls used tools=None
        self.assertTrue(len(llm.calls) >= 1)
        for call in llm.calls:
            self.assertIsNone(call["tools"])

    async def test_mode_web_or_wiki_errors(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM()

        with self.assertRaises(ValueError) as ctx_web:
            await run_research(db, palace, llm, question="test", mode="web")
        self.assertIn("closed", str(ctx_web.exception).lower())

        with self.assertRaises(ValueError) as ctx_wiki:
            await run_research(db, palace, llm, question="test", mode="wiki")
        self.assertIn("closed", str(ctx_wiki.exception).lower())

        with self.assertRaises(ValueError):
            await run_research(db, palace, llm, question="test", mode="invalid_mode")

    async def test_empty_corpus_produces_non_empty_gaps_and_no_invented_url(self) -> None:
        db = FakeDB(facts=[])
        palace = FakePalace(hits=[], available=False)
        llm = FakeLLM(rewrite_reply='["nonexistent topic"]')

        result = await run_research(db, palace, llm, question="What is nonexistent?")

        self.assertEqual(result["sources"], [])
        self.assertTrue(len(result["gaps"]) > 0)
        self.assertIn("No relevant notes", result["answer"])
        self.assertNotIn("http://", result["answer"])
        self.assertNotIn("https://", result["answer"])
        for gap in result["gaps"]:
            self.assertNotIn("http://", gap)
            self.assertNotIn("https://", gap)

        # Writer must not have been called when zero sources were found
        writer_calls = [
            c for c in llm.calls if any("answer only from" in msg.get("content", "").lower() for msg in c["messages"])
        ]
        self.assertEqual(len(writer_calls), 0)

    async def test_rewrite_failure_falls_back_to_question(self) -> None:
        db = FakeDB(
            facts=[{"id": 1, "content": "Mutiny uses SQLite for persistence."}]
        )
        palace = FakePalace()

        # Case 1: Exception during rewrite
        llm_err = FakeLLM(rewrite_reply=RuntimeError("rewrite failed"))
        res_err = await run_research(db, palace, llm_err, question="How is data stored?")
        self.assertEqual(res_err["queries"], ["How is data stored?"])

        # Case 2: Malformed non-JSON rewrite response
        llm_bad = FakeLLM(rewrite_reply="Here are some search terms:\n1. SQLite\n2. storage")
        res_bad = await run_research(db, palace, llm_bad, question="How is data stored?")
        self.assertEqual(res_bad["queries"], ["How is data stored?"])

        # Case 3: Empty JSON list in rewrite response
        llm_empty = FakeLLM(rewrite_reply="[]")
        res_empty = await run_research(db, palace, llm_empty, question="How is data stored?")
        self.assertEqual(res_empty["queries"], ["How is data stored?"])

    async def test_model_resolution_and_explicit_model_override(self) -> None:
        db = FakeDB(
            facts=[{"id": 1, "content": "Fact one"}],
            model="db-default-model",
        )
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["fact"]')

        # Model omitted -> resolves from db
        res_default = await run_research(db, palace, llm, question="test")
        self.assertEqual(res_default["model"], "db-default-model")
        self.assertEqual(db.get_model_calls, 1)

        # Explicit model -> uses explicit model without db lookup
        res_explicit = await run_research(
            db, palace, llm, question="test", model="custom-llama:8b"
        )
        self.assertEqual(res_explicit["model"], "custom-llama:8b")
        self.assertEqual(db.get_model_calls, 1)

    async def test_deduplicates_sources_by_record_id_and_excerpt(self) -> None:
        db = FakeDB(
            facts=[
                {"id": 1, "content": "Shared query topic between runs"},
            ]
        )
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["query alpha", "query beta"]')

        result = await run_research(db, palace, llm, question="test deduplication")
        # Even though two queries were executed and both matched the same fact, only one source is returned
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["record_id"], "1")

    async def test_derives_gaps_when_writer_indicates_notes_do_not_contain_answer(self) -> None:
        db = FakeDB(
            facts=[{"id": 1, "content": "Mutiny project notes mention release history is undocumented."}]
        )
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["release date"]',
            writer_reply="The notes do not contain the answer to when Mutiny was released.",
        )

        result = await run_research(db, palace, llm, question="When was Mutiny released?")
        self.assertEqual(result["gaps"], ["not in retrieved notes"])
        self.assertEqual(len(result["sources"]), 1)

    async def test_closed_run_with_palace_hit_has_memory_kind_and_no_external_url(self) -> None:
        hit = MemoryHit(text="Palace remembered note", record_id="mem-99", title="MemPalace Hit")
        db = FakeDB(facts=[])
        palace = FakePalace(hits=[hit], available=True)
        llm = FakeLLM(
            rewrite_reply='["palace memory"]',
            writer_reply="The note mentions Palace remembered note.",
        )

        result = await run_research(db, palace, llm, question="Recall palace note")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["kind"], "memory")
        self.assertEqual(result["sources"][0]["title"], "MemPalace Hit")
        self.assertEqual(result["sources"][0]["excerpt"], "Palace remembered note")
        self.assertEqual(result["sources"][0]["record_id"], "mem-99")
        self.assertIsNone(result["sources"][0]["external_url"])

    async def test_empty_or_whitespace_question_does_not_leak_facts_or_call_writer(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "Private confidential stored fact"}])
        palace = FakePalace()
        llm = FakeLLM()

        for empty_q in ["", "   ", "\t\n"]:
            result = await run_research(db, palace, llm, question=empty_q)
            self.assertEqual(result["sources"], [])
            self.assertEqual(result["queries"], [])
            self.assertEqual(result["gaps"], ["not in retrieved notes"])
            self.assertIn("No relevant notes", result["answer"])
            # Writer must NOT be called for blank questions
            writer_calls = [
                c for c in llm.calls if any("answer only from" in msg.get("content", "").lower() for msg in c["messages"])
            ]
            self.assertEqual(len(writer_calls), 0)

    async def test_short_stopword_query_does_not_leak_unmatched_facts(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "Unrelated stored database fact"}])
        palace = FakePalace(hits=[], available=False)
        # Query with only words <= 2 characters
        llm = FakeLLM(rewrite_reply='["is it on"]')

        result = await run_research(db, palace, llm, question="is it on?")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["gaps"], ["not in retrieved notes"])
        self.assertIn("No relevant notes", result["answer"])

    async def test_writer_gap_detection_with_varied_phrasing(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "Mutiny project facts"}])
        palace = FakePalace()

        varied_replies = [
            "The release date is missing from the provided notes.",
            "The notes do not specify when Mutiny was released.",
            "There is no mention of Mutiny's release date in the notes.",
            "The provided notes contain no information regarding the release date.",
            "I don't know the release date as it cannot be answered from the notes.",
            "Missing.",
            "Not in retrieved notes.",
            "This detail is unknown from the provided notes.",
        ]

        for reply in varied_replies:
            llm = FakeLLM(rewrite_reply='["Mutiny"]', writer_reply=reply)
            result = await run_research(db, palace, llm, question="When was Mutiny released?")
            self.assertEqual(
                result["gaps"],
                ["not in retrieved notes"],
                f"Failed to detect gap for reply: '{reply}'",
            )

    def test_rewrite_parsing_deduplicates_and_handles_codeblock_text(self) -> None:
        # Markdown block with surrounding text
        raw1 = 'Here are the queries:\n```json\n["query alpha", "query beta", "query alpha"]\n```\nHope that helps!'
        parsed1 = _parse_rewrite_queries(raw1)
        self.assertEqual(parsed1, ["query alpha", "query beta"])

        # Quoted items and cap at 3
        raw2 = '["item 1", "item 2", "item 3", "item 4"]'
        parsed2 = _parse_rewrite_queries(raw2)
        self.assertEqual(parsed2, ["item 1", "item 2", "item 3"])

    async def test_sources_normalize_record_id_and_filter_empty_excerpts(self) -> None:
        db = FakeDB(
            facts=[
                {"id": 42, "content": "Valid fact content"},
                {"id": 99, "content": "   "},  # empty content
            ]
        )
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["fact"]')

        result = await run_research(db, palace, llm, question="query")
        self.assertEqual(len(result["sources"]), 1)
        source = result["sources"][0]
        # record_id must be a string, not int
        self.assertIsInstance(source["record_id"], str)
        self.assertEqual(source["record_id"], "42")
        self.assertEqual(source["excerpt"], "Valid fact content")


class LocalDocumentResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.docs_dir = Path(self.temp_dir.name)

        # Create 3 sample markdown files
        (self.docs_dir / "architecture.md").write_text(
            "# System Architecture\nMutiny runs locally as a single-process console.\n\n"
            "## Network Policy\nAll network endpoints bind strictly to loopback addresses."
        )
        (self.docs_dir / "diagnostics.md").write_text(
            "# Diagnostic Runbook\nDiagnostic probes run strictly in-process without socket binds.\n\n"
            "## Health Check\nInternal health status reports uptime and database connectivity."
        )
        (self.docs_dir / "storage.md").write_text(
            "Local persistence stores all messages and run history in SQLite."
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_question_answered_by_file_returns_document_kind_and_no_url(self) -> None:
        db = FakeDB(facts=[])
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["Network Policy", "network endpoints"]',
            writer_reply="According to the documents, all network endpoints bind strictly to loopback addresses.",
        )

        result = await run_research(
            db,
            palace,
            llm,
            question="What is the network policy?",
            mode="closed",
            docs_path=self.docs_dir,
        )

        self.assertEqual(result["mode"], "closed")
        self.assertEqual(result["writer"], "local")
        self.assertTrue(len(result["sources"]) >= 1)
        doc_source = result["sources"][0]
        self.assertEqual(doc_source["kind"], "document")
        self.assertEqual(doc_source["title"], "Network Policy")
        self.assertEqual(doc_source["record_id"], "architecture.md#Network Policy")
        self.assertEqual(
            doc_source["excerpt"], "All network endpoints bind strictly to loopback addresses."
        )
        self.assertIsNone(doc_source["external_url"])
        self.assertNotIn("http://", result["answer"])
        self.assertNotIn("https://", result["answer"])

        # LLM writer was called with tools=None
        self.assertTrue(len(llm.calls) >= 1)
        for call in llm.calls:
            self.assertIsNone(call["tools"])

    async def test_question_not_in_files_or_facts_produces_miss_gaps_and_no_writer(self) -> None:
        db = FakeDB(facts=[])
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["heirloom tomato gardening"]')

        result = await run_research(
            db,
            palace,
            llm,
            question="How do I cultivate heirloom tomatoes?",
            mode="closed",
            docs_path=self.docs_dir,
        )

        self.assertEqual(result["sources"], [])
        self.assertTrue(len(result["gaps"]) > 0)
        self.assertIn("No relevant notes", result["answer"])
        self.assertNotIn("http://", result["answer"])
        self.assertNotIn("https://", result["answer"])

        # Writer must NOT be called on zero sources
        writer_calls = [
            c
            for c in llm.calls
            if any("answer only from" in msg.get("content", "").lower() for msg in c["messages"])
        ]
        self.assertEqual(len(writer_calls), 0)

    async def test_merges_and_deduplicates_document_and_fact_hits(self) -> None:
        db = FakeDB(
            facts=[{"id": 42, "content": "Mutiny runs locally as a single-process console."}]
        )
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["single-process console"]',
            writer_reply="The notes confirm Mutiny runs locally.",
        )

        result = await run_research(
            db,
            palace,
            llm,
            question="Tell me about the single-process console",
            mode="closed",
            docs_path=self.docs_dir,
        )

        kinds = {s["kind"] for s in result["sources"]}
        self.assertIn("document", kinds)
        self.assertIn("fact", kinds)
        for s in result["sources"]:
            self.assertIsNone(s["external_url"])

    async def test_default_chat_unchanged(self) -> None:
        from core.chat import send_message
        from database.db import DatabaseManager

        class FakeChatLLM:
            def __init__(self):
                self.calls = []

            async def generate_response(self, model, messages, tools=None):
                self.calls.append({"model": model, "messages": messages, "tools": tools})
                return "Chat reply without tools."

        db = DatabaseManager(":memory:")
        await db.setup_database()
        thread = await db.create_thread(title="Test thread")
        llm = FakeChatLLM()
        reply = await send_message(
            db, llm, thread_id=thread["id"], content="Hello chat", request_id="r1"
        )
        self.assertEqual(
            reply["assistant_message"]["content"], "Chat reply without tools."
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertIsNone(llm.calls[0]["tools"])
        await db.close()


if __name__ == "__main__":
    unittest.main()

