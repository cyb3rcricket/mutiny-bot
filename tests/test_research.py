import logging
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import config
import core.research as research_module
from core.research import ResearchError, _parse_rewrite_queries, run_research, search_web
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

    async def test_writer_urls_and_invalid_citations_are_removed(self) -> None:
        db = FakeDB(
            facts=[
                {"id": 1, "content": "Mutiny runs models locally."},
                {"id": 2, "content": "Mutiny default chat uses local tools."},
            ]
        )
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["Mutiny"]',
            writer_reply=(
                "Mutiny runs models locally. "
                "[https://example.invalid/not-a-source](https://example.invalid/not-a-source) "
                "http://raw.invalid https://raw.invalid <https://angle.invalid> "
                "(https://paren.invalid) \"https://quote.invalid\" `https://tick.invalid` "
                "http:// https:// [1] [2] [0] [-1] [99] "
                "[99999999999999999999999999999999999999999999999999]"
            ),
        )

        result = await run_research(db, palace, llm, question="What is Mutiny?")

        self.assertIn("Mutiny runs models locally.", result["answer"])
        self.assertIn("[1]", result["answer"])
        self.assertIn("[2]", result["answer"])
        self.assertNotIn("[0]", result["answer"])
        self.assertNotIn("[-1]", result["answer"])
        self.assertNotIn("[99]", result["answer"])
        self.assertNotIn("example.invalid", result["answer"])
        self.assertNotIn("http://", result["answer"].lower())
        self.assertNotIn("https://", result["answer"].lower())
        self.assertNotIn("[]()", result["answer"])
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(all(source["external_url"] is None for source in result["sources"]))

    async def test_writer_url_only_output_falls_back_to_sanitized_excerpt(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "Grounded local fact."}])
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["Grounded"]',
            writer_reply="[https://writer.invalid](https://writer.invalid)",
        )

        result = await run_research(db, palace, llm, question="What is grounded?")

        self.assertEqual(result["answer"], "Grounded local fact.")
        self.assertNotIn("http://", result["answer"].lower())
        self.assertNotIn("https://", result["answer"].lower())

    async def test_url_only_retrieved_excerpt_uses_no_claim_fallback(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "https://stored.invalid/source"}])
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["stored"]',
            writer_reply="https://writer.invalid/source",
        )

        result = await run_research(db, palace, llm, question="What is stored?")

        self.assertEqual(result["answer"], "No usable retrieved excerpt remains.")
        self.assertNotIn("http://", result["answer"].lower())
        self.assertNotIn("https://", result["answer"].lower())
        self.assertEqual(result["sources"][0]["excerpt"], "https://stored.invalid/source")
        self.assertIsNone(result["sources"][0]["external_url"])

    async def test_mode_web_or_wiki_errors(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM()

        # Web mode with outbound off errors with outbound_disabled
        with patch.object(config, "OUTBOUND_ENABLED", False):
            with self.assertRaises(ValueError) as ctx_web:
                await run_research(db, palace, llm, question="test", mode="web")
        self.assertIn("outbound_disabled", str(ctx_web.exception).lower())

        # Wiki mode is still refused
        with self.assertRaises(ValueError) as ctx_wiki:
            await run_research(db, palace, llm, question="test", mode="wiki")
        self.assertIn("wiki", str(ctx_wiki.exception).lower())

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

    async def test_queries_only_include_rewrites_that_retrieval_executed(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "The first phrase matches this fact."}])
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["first phrase", "second phrase", "third phrase"]')
        recall_queries: list[str] = []
        original_recall = research_module.recall

        async def track_recall(db_arg, palace_arg, *, query: str = "", limit: int = 20):
            recall_queries.append(query)
            return await original_recall(db_arg, palace_arg, query=query, limit=limit)

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            research_module, "recall", new=track_recall
        ):
            result = await run_research(
                db,
                palace,
                llm,
                question="Which phrase matches?",
                limit=1,
                docs_path=Path(temp_dir),
            )

        self.assertEqual(recall_queries, ["first phrase"])
        self.assertEqual(result["queries"], ["first phrase"])
        self.assertEqual([source["record_id"] for source in result["sources"]], ["1"])

    async def test_zero_source_result_reports_every_nonblank_query_that_ran(self) -> None:
        db = FakeDB(facts=[])
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["first missing phrase", "", "second missing phrase"]')
        recall_queries: list[str] = []
        original_recall = research_module.recall

        async def track_recall(db_arg, palace_arg, *, query: str = "", limit: int = 20):
            recall_queries.append(query)
            return await original_recall(db_arg, palace_arg, query=query, limit=limit)

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            research_module, "recall", new=track_recall
        ):
            result = await run_research(
                db,
                palace,
                llm,
                question="Which phrases are absent?",
                docs_path=Path(temp_dir),
            )

        self.assertEqual(recall_queries, ["first missing phrase", "second missing phrase"])
        self.assertEqual(result["queries"], recall_queries)
        self.assertEqual(result["sources"], [])

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
        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch(
                "core.research.search_web",
                new=AsyncMock(side_effect=AssertionError("ordinary chat called web research")),
            ):
                reply = await send_message(
                    db, llm, thread_id=thread["id"], content="Hello chat", request_id="r1"
                )
        self.assertEqual(
            reply["assistant_message"]["content"], "Chat reply without tools."
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertIsNone(llm.calls[0]["tools"])
        await db.close()

    async def test_web_mode_outbound_off_raises_error_zero_network(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM()

        with patch.object(config, "OUTBOUND_ENABLED", False):
            with self.assertRaises(ResearchError) as ctx:
                await run_research(db, palace, llm, question="what is mutiny", mode="web")
            self.assertEqual(ctx.exception.code, "outbound_disabled")
            self.assertEqual(len(llm.calls), 0)

    async def test_closed_mode_with_outbound_on_does_not_call_searxng(self) -> None:
        db = FakeDB(facts=[{"id": 1, "content": "Mutiny runs locally."}])
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["Mutiny"]',
            writer_reply="Mutiny runs locally [1].",
        )

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch("core.research.search_web", new=AsyncMock()) as mock_search:
                result = await run_research(db, palace, llm, question="what is mutiny", mode="closed")
                mock_search.assert_not_called()
                self.assertEqual(result["mode"], "closed")
                self.assertEqual(result["sources"][0]["kind"], "fact")
                self.assertIsNone(result["sources"][0]["external_url"])

    async def test_web_mode_mocked_searxng_hits(self) -> None:
        db = FakeDB(facts=[{"id": 99, "content": "Should not be retrieved in web mode"}])
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["mutiny local console"]',
            writer_reply="Mutiny is a console [1]. See https://mutiny.example/about.",
        )

        hits_response = {
            "results": [
                {
                    "title": "Mutiny Console",
                    "url": "https://mutiny.example/about",
                    "content": "Mutiny is an open local-first AI console.",
                },
                {
                    "title": "Duplicate URL",
                    "url": "https://mutiny.example/about",
                    "content": "Duplicate snippet.",
                },
                {
                    "title": "Malicious non-http scheme",
                    "url": "javascript:alert(1)",
                    "content": "Bad script.",
                },
                {
                    "title": "Mutiny Docs",
                    "url": "https://mutiny.example/docs",
                    "content": "Documentation for local models.",
                },
            ]
        }

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/search", str(request.url))
            self.assertIn("format=json", str(request.url))
            return httpx.Response(200, json=hits_response, request=request)

        transport = httpx.MockTransport(mock_handler)

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
                result = await run_research(
                    db, palace, llm, question="what is mutiny", mode="web"
                )

        self.assertEqual(result["mode"], "web")
        self.assertEqual(result["writer"], "local")
        self.assertEqual(result["queries"], ["mutiny local console"])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(len(result["sources"]), 2)
        urls = [s["external_url"] for s in result["sources"]]
        self.assertEqual(urls, ["https://mutiny.example/about", "https://mutiny.example/docs"])
        for s in result["sources"]:
            self.assertEqual(s["kind"], "web")
            self.assertEqual(s["record_id"], s["external_url"])
            self.assertIn("T", s["retrieved_at"])
            self.assertTrue(s["excerpt"])

        self.assertIn("https://mutiny.example/about", result["answer"])
        self.assertIn("[1]", result["answer"])

        writer_call = [
            c for c in llm.calls if any("answer only from" in m.get("content", "").lower() for m in c["messages"])
        ][0]
        self.assertIsNone(writer_call["tools"])

    async def test_web_mode_reports_only_queries_sent_until_limit(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["first phrase", "second phrase", "third phrase"]',
            writer_reply="The first result is relevant [1].",
        )
        requested_queries: list[str] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            requested_queries.append(request.url.params["q"])
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "First result",
                            "url": "https://mutiny.example/first",
                            "content": "The first result is relevant.",
                        }
                    ]
                },
                request=request,
            )

        transport = httpx.MockTransport(mock_handler)
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return real_async_client(transport=transport, trust_env=False)

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch.object(research_module.httpx, "AsyncClient", side_effect=client_factory):
                result = await run_research(
                    db,
                    palace,
                    llm,
                    question="what is mutiny",
                    mode="web",
                    limit=1,
                )

        self.assertEqual(requested_queries, ["first phrase"])
        self.assertEqual(result["queries"], ["first phrase"])

    async def test_search_web_disables_proxy_environment_and_redacts_query_logs(self) -> None:
        response_hits = {
            "results": [
                {
                    "title": "Searx result",
                    "url": "https://mutiny.example/result",
                    "content": "A result.",
                }
            ]
        }

        real_async_client = httpx.AsyncClient
        client_kwargs: dict[str, Any] = {}
        selected_transport_types: list[tuple[str, str]] = []

        def client_factory(**kwargs: Any) -> httpx.AsyncClient:
            client_kwargs.update(kwargs)
            client = real_async_client(**kwargs)
            selected = client._transport_for_url(
                httpx.URL("http://127.0.0.1:8080/searx/search?q=path+prefixed")
            )
            selected_transport_types.append(
                (type(selected._pool).__name__, type(client._transport._pool).__name__)
            )
            response = httpx.Response(
                200,
                json=response_hits,
                request=httpx.Request("GET", "http://127.0.0.1:8080/searx/search"),
            )

            async def fake_get(url: str, **request_kwargs: Any) -> httpx.Response:
                httpx_logger.info(
                    "HTTP Request: GET %s?q=path+prefixed+secret+phrase&format=json",
                    url,
                )
                httpx_logger.info("HTTP Request: GET http://127.0.0.1:8080/health")
                return response

            client.get = AsyncMock(side_effect=fake_get)
            return client

        class CaptureHandler(logging.Handler):
            def __init__(self) -> None:
                super().__init__()
                self.messages: list[str] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.messages.append(record.getMessage())

        httpx_logger = logging.getLogger("httpx")
        capture = CaptureHandler()
        previous_level = httpx_logger.level
        httpx_logger.setLevel(logging.INFO)
        httpx_logger.addHandler(capture)
        try:
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://proxy.invalid:3128",
                    "HTTPS_PROXY": "http://proxy.invalid:3128",
                    "ALL_PROXY": "http://proxy.invalid:3128",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                with patch.object(config, "OUTBOUND_ENABLED", True):
                    with patch.object(research_module.httpx, "AsyncClient", side_effect=client_factory):
                        result = await search_web(
                            ["path prefixed secret phrase"],
                            searxng_url="http://user:pass@127.0.0.1:8080/searx",
                        )
        finally:
            httpx_logger.removeHandler(capture)
            httpx_logger.setLevel(previous_level)

        self.assertEqual(len(result), 1)
        self.assertFalse(client_kwargs["trust_env"])
        self.assertIsNone(client_kwargs["proxy"])
        self.assertEqual(selected_transport_types, [("AsyncConnectionPool", "AsyncConnectionPool")])
        self.assertIn("health", "\n".join(capture.messages))
        self.assertTrue(all("secret phrase" not in message for message in capture.messages))
        self.assertTrue(all("?q=" not in message for message in capture.messages))

    async def test_search_web_http_error_does_not_expose_query_in_logs_or_error(self) -> None:
        async def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"}, request=request)

        transport = httpx.MockTransport(fail_handler)
        capture_messages: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                capture_messages.append(record.getMessage())

        capture = CaptureHandler()
        research_logger = research_module.logger
        httpx_logger = logging.getLogger("httpx")
        previous_levels = (research_logger.level, httpx_logger.level)
        research_logger.setLevel(logging.WARNING)
        httpx_logger.setLevel(logging.INFO)
        research_logger.addHandler(capture)
        httpx_logger.addHandler(capture)
        try:
            with patch.object(config, "OUTBOUND_ENABLED", True):
                with patch.object(
                    research_module.httpx,
                    "AsyncClient",
                    return_value=httpx.AsyncClient(transport=transport),
                ):
                    with self.assertRaises(ResearchError) as ctx:
                        await search_web(["raw secret"], searxng_url="http://127.0.0.1:8080/searx")
        finally:
            research_logger.removeHandler(capture)
            httpx_logger.removeHandler(capture)
            research_logger.setLevel(previous_levels[0])
            httpx_logger.setLevel(previous_levels[1])

        self.assertEqual(ctx.exception.code, "searxng_unavailable")
        self.assertEqual(str(ctx.exception), "searxng_unavailable: SearxNG search failed.")
        combined_logs = "\n".join(capture_messages)
        self.assertIn("HTTPStatusError", combined_logs)
        for leaked in ("?q=", "raw secret", "raw+secret", "raw%20secret"):
            self.assertNotIn(leaked, str(ctx.exception))
            self.assertNotIn(leaked, combined_logs)

    async def test_search_web_discards_invalid_and_empty_results_and_caps_snippets(self) -> None:
        long_snippet = "Useful result. " + ("x" * 2500)
        response_hits = {
            "results": [
                {
                    "title": "  ",
                    "url": "https://mutiny.example/valid",
                    "content": "Useful result.",
                },
                {
                    "title": "Empty",
                    "url": "https://mutiny.example/empty",
                    "content": "   \n",
                },
                {
                    "title": "Long",
                    "url": "https://mutiny.example/long",
                    "content": long_snippet,
                },
                {
                    "title": "Script",
                    "url": "javascript:alert(1)",
                    "content": "Do not keep.",
                },
                {
                    "title": "Protocol relative",
                    "url": "//mutiny.example/relative",
                    "content": "Do not keep.",
                },
                {
                    "title": "Malformed",
                    "url": "http://",
                    "content": "Do not keep.",
                },
            ]
        }
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_hits, request=request)
        )

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch.object(
                research_module.httpx,
                "AsyncClient",
                return_value=httpx.AsyncClient(transport=transport),
            ):
                result = await search_web(["mixed results"])

        self.assertEqual(
            [source["external_url"] for source in result],
            ["https://mutiny.example/valid", "https://mutiny.example/long"],
        )
        self.assertEqual(result[0]["title"], "https://mutiny.example/valid")
        self.assertEqual(len(result[1]["excerpt"]), 2000)

    async def test_web_mode_all_empty_results_skips_writer(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["empty topic"]')
        response_hits = {
            "results": [
                {"url": "https://mutiny.example/empty", "content": "  "},
                {"url": "javascript:alert(1)", "content": "not a source"},
            ]
        }
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_hits, request=request)
        )

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch.object(
                research_module.httpx,
                "AsyncClient",
                return_value=httpx.AsyncClient(transport=transport),
            ):
                result = await run_research(
                    db, palace, llm, question="empty topic", mode="web"
                )

        self.assertEqual(result["sources"], [])
        self.assertEqual(result["gaps"], ["not in retrieved notes"])
        writer_calls = [
            call
            for call in llm.calls
            if any("answer only from" in m.get("content", "").lower() for m in call["messages"])
        ]
        self.assertEqual(writer_calls, [])

    async def test_web_mode_sanitizer_removes_unapproved_urls_and_invalid_citations(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM(
            rewrite_reply='["mutiny"]',
            writer_reply=(
                "Mutiny is local [1]. "
                "Visit https://mutiny.example/about and "
                "https://mutiny.example/path_(official) [2]. "
                "Also check (https://evil.example/lookalike), "
                "[escaped](https://evil.example/a\\(b\\)), //evil.example/raw, "
                "javascript:alert(1), ftp://evil.example/file, "
                "[relative](/not-allowed), and [evil link](https://evil.example). "
                "Invalid references [0] [-1] [99]."
            ),
        )

        hits_response = {
            "results": [
                {
                    "title": "Mutiny",
                    "url": "https://mutiny.example/about",
                    "content": "Mutiny is local.",
                },
                {
                    "title": "Official path",
                    "url": "https://mutiny.example/path_(official)",
                    "content": "The official path is available.",
                }
            ]
        }

        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=hits_response, request=req))

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
                result = await run_research(db, palace, llm, question="what is mutiny", mode="web")

        self.assertIn("https://mutiny.example/about", result["answer"])
        self.assertIn("https://mutiny.example/path_(official)", result["answer"])
        self.assertNotIn("evil.example", result["answer"])
        self.assertNotIn("javascript:", result["answer"])
        self.assertNotIn("ftp://", result["answer"])
        self.assertNotIn("//evil.example", result["answer"])
        self.assertNotIn("/not-allowed", result["answer"])
        self.assertIn("[1]", result["answer"])
        self.assertIn("[2]", result["answer"])
        self.assertNotIn("[0]", result["answer"])
        self.assertNotIn("[-1]", result["answer"])
        self.assertNotIn("[99]", result["answer"])

        angle_wrapped = research_module._sanitize_web_answer(
            "[source](<https://mutiny.example/path_(official)>)",
            result["sources"],
        )
        self.assertIn("https://mutiny.example/path_(official)", angle_wrapped)
        escaped_allowed = research_module._sanitize_web_answer(
            r"[source](https://mutiny.example/path_\(official\))",
            result["sources"],
        )
        self.assertIn("https://mutiny.example/path_(official)", escaped_allowed)

        prefix_lookalike = research_module._sanitize_web_answer(
            r"[https://allowed.example/page(evil)](https://allowed.example/page\(evil\)) "
            r"raw https://allowed.example/page(evil) and valid https://allowed.example/page",
            [{"external_url": "https://allowed.example/page"}],
        )
        self.assertIn("https://allowed.example/page", prefix_lookalike)
        self.assertNotIn("https://allowed.example/page(evil)", prefix_lookalike)
        self.assertNotIn("https://allowed.example/page\\(evil\\)", prefix_lookalike)

    async def test_web_mode_searxng_down_fails_without_fake_article(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["mutiny"]')

        async def fail_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused to SearxNG")

        transport = httpx.MockTransport(fail_handler)

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
                with self.assertRaises(ResearchError) as ctx:
                    await run_research(db, palace, llm, question="what is mutiny", mode="web")
                self.assertEqual(ctx.exception.code, "searxng_unavailable")

        writer_calls = [
            c for c in llm.calls if any("answer only from" in m.get("content", "").lower() for m in c["messages"])
        ]
        self.assertEqual(len(writer_calls), 0)

    async def test_web_mode_zero_hits_returns_gaps_and_skips_writer(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM(rewrite_reply='["obscure topic"]')

        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []}, request=req))

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
                result = await run_research(db, palace, llm, question="obscure query", mode="web")

        self.assertEqual(result["sources"], [])
        self.assertEqual(result["gaps"], ["not in retrieved notes"])
        self.assertIn("No relevant web snippets", result["answer"])
        self.assertNotIn("http://", result["answer"])
        self.assertNotIn("https://", result["answer"])

        writer_calls = [
            c for c in llm.calls if any("answer only from" in m.get("content", "").lower() for m in c["messages"])
        ]
        self.assertEqual(len(writer_calls), 0)

    async def test_web_mode_non_loopback_url_rejected(self) -> None:
        db = FakeDB()
        palace = FakePalace()
        llm = FakeLLM()

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with patch.object(config, "SEARXNG_URL", "http://external-searxng.org:8080"):
                with self.assertRaises(ResearchError) as ctx:
                    await run_research(db, palace, llm, question="test", mode="web")
                self.assertEqual(ctx.exception.code, "invalid_searxng_url")

    async def test_search_web_direct_guards(self) -> None:
        with patch.object(config, "OUTBOUND_ENABLED", False):
            with self.assertRaises(ResearchError) as ctx:
                await search_web(["test"], mode="web")
            self.assertEqual(ctx.exception.code, "outbound_disabled")

        with patch.object(config, "OUTBOUND_ENABLED", True):
            with self.assertRaises(ResearchError) as ctx:
                await search_web(["test"], mode="closed")
            self.assertEqual(ctx.exception.code, "invalid_mode")


if __name__ == "__main__":
    unittest.main()
