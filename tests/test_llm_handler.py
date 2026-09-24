"""Unit tests for LLMHandler error handling and tool-call safety."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from llm.llm_handler import (
    LLMError,
    LLMHandler,
    MAX_TOOL_RESULT_CHARS,
    TOOL_RESULT_TRUNCATION_SUFFIX,
)


def _completion_response(content: str = "", tool_calls=None) -> SimpleNamespace:
    """Build a minimal LiteLLM-like completion response object."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class LLMHandlerAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Validate robust fallback behavior in LLM interactions."""

    async def test_generate_response_returns_fallback_on_primary_failure(self) -> None:
        handler = LLMHandler("http://127.0.0.1:11434")
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]

        with patch(
            "llm.llm_handler.litellm.acompletion",
            new=AsyncMock(side_effect=Exception("offline SENTINEL prompt should not be logged")),
        ):
            with self.assertRaises(LLMError) as raised:
                await handler.generate_response(
                    model="ollama/qwen2.5-coder:7b",
                    messages=messages,
                    tools=None,
                )

        self.assertEqual(raised.exception.code, "model_unavailable")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("SENTINEL", raised.exception.message)

    async def test_tool_timeout_generates_tool_result_and_returns_final_response(self) -> None:
        async def slow_tool() -> str:
            await asyncio.sleep(0)
            return "done"

        tool_call = SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="slow_tool", arguments="{}"),
        )

        first_response = _completion_response(content="", tool_calls=[tool_call])
        final_response = _completion_response(content="final answer")

        handler = LLMHandler(
            "http://127.0.0.1:11434",
            tool_functions={"slow_tool": slow_tool},
        )

        completion_mock = AsyncMock(side_effect=[first_response, final_response])
        async def _timeout_and_close(awaitable, timeout):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError()

        wait_for_mock = AsyncMock(side_effect=_timeout_and_close)

        with patch("llm.llm_handler.litellm.acompletion", new=completion_mock):
            with patch("llm.llm_handler.asyncio.wait_for", new=wait_for_mock):
                result = await handler.generate_response(
                    model="ollama/qwen2.5-coder:7b",
                    messages=[{"role": "user", "content": "run tool"}],
                    tools=[{"type": "function", "function": {"name": "slow_tool"}}],
                )

        self.assertEqual(result, "final answer")
        self.assertEqual(completion_mock.await_count, 2)

        second_call_messages = completion_mock.call_args_list[1].kwargs["messages"]
        tool_messages = [msg for msg in second_call_messages if msg.get("role") == "tool"]
        self.assertTrue(any("timed out" in str(msg.get("content", "")) for msg in tool_messages))

    async def test_summarize_history_returns_empty_on_failure(self) -> None:
        handler = LLMHandler("http://127.0.0.1:11434")
        history = [{"role": "user", "content": "hello"}] * 10

        with patch(
            "llm.llm_handler.litellm.acompletion",
            new=AsyncMock(side_effect=Exception("unavailable")),
        ):
            summary = await handler.summarize_history(
                model="ollama/qwen2.5-coder:7b",
                full_history=history,
            )

        self.assertEqual(summary, "")

    async def test_tool_result_is_truncated_before_final_completion(self) -> None:
        oversized_result = "z" * (MAX_TOOL_RESULT_CHARS + 500)

        async def large_tool() -> str:
            return oversized_result

        tool_call = SimpleNamespace(
            id="call_2",
            type="function",
            function=SimpleNamespace(name="large_tool", arguments="{}"),
        )

        first_response = _completion_response(content="", tool_calls=[tool_call])
        final_response = _completion_response(content="ok")

        handler = LLMHandler(
            "http://127.0.0.1:11434",
            tool_functions={"large_tool": large_tool},
        )

        completion_mock = AsyncMock(side_effect=[first_response, final_response])

        with patch("llm.llm_handler.litellm.acompletion", new=completion_mock):
            result = await handler.generate_response(
                model="ollama/qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "run"}],
                tools=[{"type": "function", "function": {"name": "large_tool"}}],
            )

        self.assertEqual(result, "ok")
        second_call_messages = completion_mock.call_args_list[1].kwargs["messages"]
        tool_message = [msg for msg in second_call_messages if msg.get("role") == "tool"][0]
        self.assertLessEqual(len(tool_message["content"]), MAX_TOOL_RESULT_CHARS)
        self.assertTrue(tool_message["content"].endswith(TOOL_RESULT_TRUNCATION_SUFFIX))

    async def test_multiple_tool_calls_all_execute_before_final_completion(self) -> None:
        async def tool_a() -> str:
            return "result-a"

        async def tool_b() -> str:
            return "result-b"

        tool_calls = [
            SimpleNamespace(
                id="call_a",
                type="function",
                function=SimpleNamespace(name="tool_a", arguments="{}"),
            ),
            SimpleNamespace(
                id="call_b",
                type="function",
                function=SimpleNamespace(name="tool_b", arguments="{}"),
            ),
        ]

        first_response = _completion_response(content="", tool_calls=tool_calls)
        final_response = _completion_response(content="all done")

        handler = LLMHandler(
            "http://127.0.0.1:11434",
            tool_functions={"tool_a": tool_a, "tool_b": tool_b},
        )

        completion_mock = AsyncMock(side_effect=[first_response, final_response])

        with patch("llm.llm_handler.litellm.acompletion", new=completion_mock):
            result = await handler.generate_response(
                model="ollama/qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "run both"}],
                tools=[{"type": "function", "function": {"name": "tool_a"}}],
            )

        self.assertEqual(result, "all done")
        second_call_messages = completion_mock.call_args_list[1].kwargs["messages"]
        tool_messages = [msg for msg in second_call_messages if msg.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual({msg["tool_call_id"] for msg in tool_messages}, {"call_a", "call_b"})

    async def test_legitimate_json_is_preserved(self) -> None:
        handler = LLMHandler("http://127.0.0.1:11434")
        payload = '{"status": "ok", "items": [1, 2]}'
        with patch(
            "llm.llm_handler.litellm.acompletion",
            new=AsyncMock(return_value=_completion_response(content=payload)),
        ):
            result = await handler.generate_response(
                model="ollama/qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "return json"}],
                tools=None,
            )
        self.assertEqual(result, payload)

    async def test_tool_outside_allowlist_is_not_executed(self) -> None:
        called = {"count": 0}

        async def secret_tool() -> str:
            called["count"] += 1
            return "secret"

        tool_call = SimpleNamespace(
            id="call_secret",
            type="function",
            function=SimpleNamespace(name="secret_tool", arguments="{}"),
        )
        handler = LLMHandler("http://127.0.0.1:11434", tool_functions={"secret_tool": secret_tool})
        completion_mock = AsyncMock(
            side_effect=[
                _completion_response(content="", tool_calls=[tool_call]),
                _completion_response(content="refused"),
            ]
        )
        with patch("llm.llm_handler.litellm.acompletion", new=completion_mock):
            result = await handler.generate_response(
                model="ollama/qwen2.5-coder:7b",
                messages=[{"role": "user", "content": "run it"}],
                tools=[{"type": "function", "function": {"name": "get_morning_briefing"}}],
            )
        self.assertEqual(result, "refused")
        self.assertEqual(called["count"], 0)


if __name__ == "__main__":
    unittest.main()
