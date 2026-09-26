"""Local API security, chat persistence, and explicit tools."""

import os
import tempfile
import unittest

from starlette.testclient import TestClient

from core.chat import ChatConflict, send_message
from database.db import DatabaseManager
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
        self.llm = FakeLLM()
        self.app = create_app(
            db_path=os.path.join(self._tmp.name, "app.db"),
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


if __name__ == "__main__":
    unittest.main()
