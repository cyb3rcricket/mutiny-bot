"""Local API security, chat persistence, and explicit tools."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from core.chat import ChatConflict, send_message
from database.db import DatabaseManager, InputTooLong
from llm.llm_handler import LLMError
from web.app import create_app


class FakeLLM:
    api_base = "http://127.0.0.1:11434"

    def __init__(self) -> None:
        self.calls = 0
        self.reply = ""
        self.rewrite_reply = None

    async def generate_response(self, model, messages, tools=None):
        self.calls += 1
        if tools:
            raise AssertionError("ordinary chat must not be given tools")
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        if "search phrase" in system.lower():
            if self.rewrite_reply is not None:
                return self.rewrite_reply
            return '["garage"]'
        if self.reply:
            return self.reply
        user = [item for item in messages if item["role"] == "user"][-1]["content"]
        if user == "FAIL":
            raise LLMError("model_unavailable", "The local model could not be reached.", retryable=True)
        return f"Answer: {user}"


class LocalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmp.name, "app.db")
        self.llm = FakeLLM()
        self.app = create_app(
            db_path=self._db_path,
            scheduler_db_path=os.path.join(self._tmp.name, "sched.db"),
            legacy_scheduler_db_path=os.path.join(self._tmp.name, "legacy.db"),
            palace_path=os.path.join(self._tmp.name, "palace"),
            llm=self.llm,
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.client.get("/api/session")
        self.mutation = {
            "Origin": "http://127.0.0.1:8765",
            "X-Mutiny-Request": "1",
            "Content-Type": "application/json",
        }

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_api_module_is_static_and_session_is_explicit(self) -> None:
        fresh = TestClient(self.app, base_url="http://127.0.0.1:8765")
        module = fresh.get("/api.js")
        self.assertEqual(module.status_code, 200)
        self.assertIn("function api", module.text)
        self.assertNotIn("mutiny_session", module.headers.get("set-cookie", ""))
        script = fresh.get("/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn('api("/api/session")', script.text)

    def test_host_origin_and_session_guards(self) -> None:
        evil = TestClient(self.app, base_url="http://evil.example")
        denied = evil.get("/api/status")
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(denied.json()["error"]["code"], "invalid_host")

        fresh = TestClient(self.app, base_url="http://127.0.0.1:8765")
        missing = fresh.post("/api/threads", headers=self.mutation, json={})
        self.assertEqual(missing.status_code, 401)

        cross = self.client.post(
            "/api/threads",
            headers={**self.mutation, "Origin": "http://evil.example"},
            json={"title": "nope"},
        )
        self.assertEqual(cross.status_code, 403)
        plain = self.client.post(
            "/api/threads",
            headers={**self.mutation, "Content-Type": "text/plain"},
            content="hello",
        )
        self.assertEqual(plain.status_code, 415)

    def test_chat_persists_long_answers_and_duplicate_requests(self) -> None:
        created = self.client.post("/api/threads", headers=self.mutation, json={})
        self.assertEqual(created.status_code, 200)
        thread_id = created.json()["id"]
        self.llm.reply = "Y" * 9000
        sent = self.client.post(
            f"/api/threads/{thread_id}/messages",
            headers=self.mutation,
            json={"request_id": "req-1", "content": "Hello there"},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        body = sent.json()
        self.assertEqual(len(body["assistant_message"]["content"]), 9000)
        self.assertEqual(body["assistant_message"]["sources"], [])
        self.assertEqual(body["user_message"]["content"], "Hello there")
        again = self.client.post(
            f"/api/threads/{thread_id}/messages",
            headers=self.mutation,
            json={"request_id": "req-1", "content": "Hello there"},
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.llm.calls, 1)
        listed = self.client.get(f"/api/threads/{thread_id}/messages")
        self.assertEqual(len(listed.json()["items"]), 2)
        self.assertEqual(len(listed.json()["items"][1]["content"]), 9000)

    def test_inference_failure_is_structured(self) -> None:
        thread_id = self.client.post("/api/threads", headers=self.mutation, json={"title": "t"}).json()["id"]
        self.llm.reply = ""
        failed = self.client.post(
            f"/api/threads/{thread_id}/messages",
            headers=self.mutation,
            json={"request_id": "bad", "content": "FAIL"},
        )
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.json()["error"]["code"], "model_unavailable")
        self.assertNotIn("Traceback", failed.text)
        history = self.client.get(f"/api/threads/{thread_id}/messages").json()["items"]
        self.assertEqual(history[-1]["status"], "failed")

    def test_memory_tools_jobs_and_absent_dangerous_routes(self) -> None:
        remembered = self.client.post(
            "/api/memory/facts",
            headers=self.mutation,
            json={"request_id": "fact-1", "content": "The garage code is 1234"},
        )
        self.assertEqual(remembered.status_code, 200, remembered.text)
        self.assertIn(remembered.json()["palace_status"], {"unavailable", "failed", "indexed"})
        again = self.client.post(
            "/api/memory/facts",
            headers=self.mutation,
            json={"request_id": "fact-1", "content": "The garage code is 1234"},
        )
        self.assertEqual(again.json()["id"], remembered.json()["id"])

        recalled = self.client.post(
            "/api/tools/recall/runs",
            headers=self.mutation,
            json={"request_id": "recall-1", "arguments": {"query": "garage"}},
        )
        self.assertEqual(recalled.status_code, 200, recalled.text)
        self.assertIn("1234", recalled.json()["output"])
        self.assertTrue(recalled.json()["sources"])

        self.llm.reply = "The code is 1234."
        asked = self.client.post(
            "/api/tools/ask_notes/runs",
            headers=self.mutation,
            json={"request_id": "ask-1", "arguments": {"question": "What is the garage code?"}},
        )
        self.assertEqual(asked.status_code, 200, asked.text)
        self.assertIn("1234", asked.json()["output"])
        self.assertTrue(asked.json()["sources"])

        briefing = self.client.post(
            "/api/tools/get_morning_briefing/runs",
            headers=self.mutation,
            json={"request_id": "brief-1", "arguments": {}},
        )
        self.assertEqual(briefing.status_code, 200, briefing.text)
        self.assertIn("Morning Briefing", briefing.json()["output"])

        names = {tool["name"] for tool in self.client.get("/api/tools").json()["tools"]}
        self.assertEqual(names, {"get_morning_briefing", "list_active_automations", "recall", "ask_notes", "research"})
        for blocked in ("ping", "docker", "shell", "restart"):
            missing = self.client.post(
                f"/api/tools/{blocked}/runs",
                headers=self.mutation,
                json={"request_id": f"no-{blocked}", "arguments": {}},
            )
            self.assertEqual(missing.status_code, 404)
        traversal = self.client.post(
            "/api/tools/..%2Fping/runs",
            headers=self.mutation,
            json={"request_id": "no-traversal", "arguments": {}},
        )
        self.assertNotEqual(traversal.status_code, 200)

        created = self.client.post(
            "/api/jobs",
            headers=self.mutation,
            json={
                "name": "Morning",
                "tool_name": "get_morning_briefing",
                "arguments": {},
                "schedule": {"type": "daily", "time": "07:30", "timezone": "UTC"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        job_id = created.json()["id"]
        paused = self.client.patch(f"/api/jobs/{job_id}", headers=self.mutation, json={"paused": True})
        self.assertTrue(paused.json()["paused"])
        removed = self.client.delete(f"/api/jobs/{job_id}", headers=self.mutation)
        self.assertEqual(removed.status_code, 204)
        status = self.client.get("/api/status")
        self.assertFalse(status.json()["dangerous_tools_enabled"])
        self.assertFalse(status.json()["outbound_enabled"])
        self.assertIn("127.0.0.1", status.json()["ollama_target"])

    def test_research_tool_run_contract_and_refusals(self) -> None:
        from core.tool_runner import SCHEDULABLE_TOOL_NAMES

        # 1. Verify research is NOT in SCHEDULABLE_TOOL_NAMES
        self.assertNotIn("research", SCHEDULABLE_TOOL_NAMES)

        # 2. Scheduling research tool must fail
        sched_attempt = self.client.post(
            "/api/jobs",
            headers=self.mutation,
            json={
                "name": "Scheduled Research",
                "tool_name": "research",
                "arguments": {"question": "What is Mutiny?"},
                "schedule": {"type": "daily", "time": "08:00", "timezone": "UTC"},
            },
        )
        self.assertEqual(sched_attempt.status_code, 422)

        # 3. GET /api/tools includes research with correct parameters and policy
        tools_list = self.client.get("/api/tools").json()["tools"]
        research_tool = next((t for t in tools_list if t["name"] == "research"), None)
        self.assertIsNotNone(research_tool)
        self.assertFalse(research_tool["network"])
        self.assertFalse(research_tool["schedulable"])
        self.assertIn("question", research_tool["parameters"]["required"])

        # 4. Save a fact to query
        self.client.post(
            "/api/memory/facts",
            headers=self.mutation,
            json={"request_id": "fact-res-1", "content": "Mutiny is a personal local AI console."},
        )

        # 5. Successful research run in closed mode
        self.llm.rewrite_reply = '["personal local AI"]'
        self.llm.reply = "According to retrieved notes, Mutiny is a personal local AI console."
        res = self.client.post(
            "/api/tools/research/runs",
            headers=self.mutation,
            json={
                "request_id": "run-res-1",
                "arguments": {"question": "What is Mutiny?", "mode": "closed"},
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["tool_name"], "research")
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["output"], "According to retrieved notes, Mutiny is a personal local AI console.")
        self.assertTrue(len(data["sources"]) > 0)
        for src in data["sources"]:
            self.assertEqual(src["run_id"], data["id"])
            self.assertIsNone(src["external_url"])
            self.assertIn(src["kind"], {"fact", "memory"})
            self.assertTrue(src["excerpt"])

        # Verify arguments_json contract: {question, mode, queries, gaps, model, writer} without essay
        args = data["arguments"]
        self.assertEqual(args["question"], "What is Mutiny?")
        self.assertEqual(args["mode"], "closed")
        self.assertEqual(args["writer"], "local")
        self.assertIsInstance(args["queries"], list)
        self.assertIsInstance(args["gaps"], list)
        self.assertIn("model", args)
        self.assertNotIn("answer", args)
        self.assertNotIn(data["output"], str(args))

        # Idempotency check: same request_id returns existing run
        again = self.client.post(
            "/api/tools/research/runs",
            headers=self.mutation,
            json={
                "request_id": "run-res-1",
                "arguments": {"question": "What is Mutiny?", "mode": "closed"},
            },
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["id"], data["id"])

        # 6. mode="web" and mode="wiki" fail closed (status=failed, no sources, no egress)
        web_res = self.client.post(
            "/api/tools/research/runs",
            headers=self.mutation,
            json={
                "request_id": "run-web-1",
                "arguments": {"question": "What is Mutiny?", "mode": "web"},
            },
        )
        self.assertEqual(web_res.status_code, 200, web_res.text)
        web_data = web_res.json()
        self.assertEqual(web_data["status"], "failed")
        self.assertEqual(web_data["sources"], [])
        self.assertIn("not implemented", web_data["output"].lower())

        wiki_res = self.client.post(
            "/api/tools/research/runs",
            headers=self.mutation,
            json={
                "request_id": "run-wiki-1",
                "arguments": {"question": "What is Mutiny?", "mode": "wiki"},
            },
        )
        self.assertEqual(wiki_res.status_code, 200, wiki_res.text)
        wiki_data = wiki_res.json()
        self.assertEqual(wiki_data["status"], "failed")
        self.assertEqual(wiki_data["sources"], [])
        self.assertIn("not implemented", wiki_data["output"].lower())

        # Missing question fails
        empty_res = self.client.post(
            "/api/tools/research/runs",
            headers=self.mutation,
            json={
                "request_id": "run-empty-1",
                "arguments": {"question": "   "},
            },
        )
        self.assertEqual(empty_res.status_code, 200)
        self.assertEqual(empty_res.json()["status"], "failed")

    def _run_row(self, request_id: str) -> tuple[str, str | None, str | None]:
        with sqlite3.connect(self._db_path) as connection:
            row = connection.execute(
                "SELECT status, error_code, output FROM runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_research_unexpected_failure_is_persisted_and_sanitized(self) -> None:
        request_id = "run-unexpected-1"
        with patch(
            "web.routes.api.run_research",
            new=AsyncMock(side_effect=RuntimeError("private traceback detail")),
        ):
            response = self.client.post(
                "/api/tools/research/runs",
                headers=self.mutation,
                json={"request_id": request_id, "arguments": {"question": "What is Mutiny?"}},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "research_failed")
        self.assertNotIn("private traceback detail", response.text)
        self.assertEqual(self._run_row(request_id), ("failed", "research_failed", "Research could not be completed."))

    def test_research_known_failures_keep_structured_error_handlers(self) -> None:
        cases = [
            (InputTooLong("too long"), 422, "input_too_long"),
            (LLMError("model_unavailable", "local model failed", retryable=True), 503, "model_unavailable"),
        ]
        for index, (failure, status, code) in enumerate(cases):
            request_id = f"run-known-failure-{index}"
            with patch("web.routes.api.run_research", new=AsyncMock(side_effect=failure)):
                response = self.client.post(
                    "/api/tools/research/runs",
                    headers=self.mutation,
                    json={"request_id": request_id, "arguments": {"question": "What is Mutiny?"}},
                )
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertEqual(self._run_row(request_id)[0:2], ("failed", code))

    def test_research_sources_are_saved_before_run_completes(self) -> None:
        self.client.post(
            "/api/memory/facts",
            headers=self.mutation,
            json={"request_id": "fact-order-1", "content": "Mutiny is local."},
        )
        self.llm.rewrite_reply = '["Mutiny local"]'
        self.llm.reply = "The notes say Mutiny is local."
        db = self.app.state.services.db
        original_add_source = db.add_source
        observed_statuses: list[str] = []

        async def observe_source_insert(**kwargs):
            current = await db.get_run(kwargs["run_id"])
            observed_statuses.append(current["status"])
            return await original_add_source(**kwargs)

        with patch.object(db, "add_source", new=observe_source_insert):
            response = self.client.post(
                "/api/tools/research/runs",
                headers=self.mutation,
                json={
                    "request_id": "run-order-1",
                    "arguments": {"question": "What is Mutiny?"},
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "complete")
        self.assertEqual(observed_statuses, ["running"])

    def test_research_get_returns_sanitized_output_and_same_sources(self) -> None:
        for index, content in enumerate(
            ("Mutiny first grounded fact.", "Mutiny second grounded fact."),
            start=1,
        ):
            self.client.post(
                "/api/memory/facts",
                headers=self.mutation,
                json={"request_id": f"fact-citation-{index}", "content": content},
            )
        self.llm.rewrite_reply = '["Mutiny grounded"]'
        self.llm.reply = (
            "Grounded answer [https://writer.invalid/source](https://writer.invalid/source) "
            "http://bare.invalid [1] [2] [0] [99]"
        )

        with patch.object(self.llm, "generate_response", new=AsyncMock(wraps=self.llm.generate_response)) as writer_recorder:
            response = self.client.post(
                "/api/tools/research/runs",
                headers=self.mutation,
                json={
                    "request_id": "run-citation-1",
                    "arguments": {"question": "What is grounded?"},
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        posted = response.json()
        fetched_response = self.client.get(f"/api/runs/{posted['id']}")
        self.assertEqual(fetched_response.status_code, 200)
        fetched = fetched_response.json()

        self.assertEqual(fetched["output"], posted["output"])
        self.assertNotIn("http://", fetched["output"].lower())
        self.assertNotIn("https://", fetched["output"].lower())
        self.assertIn("[1]", fetched["output"])
        self.assertIn("[2]", fetched["output"])
        self.assertNotIn("[0]", fetched["output"])
        self.assertNotIn("[99]", fetched["output"])
        self.assertEqual(
            [source["record_id"] for source in fetched["sources"]],
            [source["record_id"] for source in posted["sources"]],
        )
        self.assertEqual(len(fetched["sources"]), 2)
        self.assertTrue(all(source["external_url"] is None for source in fetched["sources"]))
        writer_messages = next(
            call.kwargs["messages"]
            for call in writer_recorder.await_args_list
            if any(
                message.get("role") == "system"
                and "Answer only from the numbered notes" in message.get("content", "")
                for message in call.kwargs["messages"]
            )
        )
        writer_user_prompt = next(
            message["content"] for message in writer_messages if message.get("role") == "user"
        )
        notes_prompt, _question_prompt = writer_user_prompt.split("\n\nQuestion: ", 1)
        expected_notes_prompt = "Notes:\n" + "\n".join(
            f"[{index}] {source['excerpt']}"
            for index, source in enumerate(fetched["sources"], start=1)
        )
        self.assertEqual(notes_prompt, expected_notes_prompt)

    def test_research_source_save_failure_marks_run_failed(self) -> None:
        self.client.post(
            "/api/memory/facts",
            headers=self.mutation,
            json={"request_id": "fact-source-failure-1", "content": "Mutiny is local."},
        )
        self.llm.rewrite_reply = '["Mutiny local"]'
        self.llm.reply = "The notes say Mutiny is local."
        db = self.app.state.services.db

        with patch.object(db, "add_source", new=AsyncMock(side_effect=RuntimeError("disk detail"))):
            response = self.client.post(
                "/api/tools/research/runs",
                headers=self.mutation,
                json={
                    "request_id": "run-source-failure-1",
                    "arguments": {"question": "What is Mutiny?"},
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "source_persistence_failed")
        self.assertNotIn("disk detail", response.text)
        status, error_code, output = self._run_row("run-source-failure-1")
        self.assertEqual((status, error_code), ("failed", "source_persistence_failed"))
        self.assertEqual(output, "The notes say Mutiny is local.")


class ChatConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_turn_rejects_a_second_request(self) -> None:
        db = DatabaseManager(":memory:")
        await db.setup_database()
        thread = await db.create_thread("busy")
        await db.insert_message(
            thread["id"],
            "assistant",
            "",
            status="pending",
            request_id="inflight",
            enforce_limit=False,
        )
        with self.assertRaises(ChatConflict):
            await send_message(db, FakeLLM(), thread_id=thread["id"], request_id="next", content="hello")
        await db.close()

    async def test_run_source_order_follows_insert_rowid_when_timestamps_and_ids_tie(self) -> None:
        class UUIDStub:
            def __init__(self, value: str) -> None:
                self.hex = value

        db = DatabaseManager(":memory:")
        await db.setup_database()
        run = await db.create_run(tool_name="research", request_id="source-order-run")

        with patch(
            "database.db.uuid.uuid4",
            side_effect=[UUIDStub("f" * 32), UUIDStub("0" * 32)],
        ), patch("database.db.utc_now", return_value="2026-09-25T00:00:00+00:00"):
            await db.add_source(
                run_id=run["id"],
                kind="fact",
                title="First",
                excerpt="First excerpt",
                record_id="first",
            )
            await db.add_source(
                run_id=run["id"],
                kind="fact",
                title="Second",
                excerpt="Second excerpt",
                record_id="second",
            )

        sources = await db.list_sources(run_id=run["id"])
        self.assertEqual([source["record_id"] for source in sources], ["first", "second"])
        await db.close()


if __name__ == "__main__":
    unittest.main()
