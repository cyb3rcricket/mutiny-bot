"""LLM handler for MutinyBot using litellm and Ollama."""

import asyncio
import json
import logging
from collections.abc import Callable
from inspect import isawaitable
from typing import Any, Optional

import litellm
from config import MAX_HISTORY_MESSAGES


MAX_TAIL_MESSAGES = 8
MAX_TOOL_RESULT_CHARS = 4000
TOOL_RESULT_TRUNCATION_SUFFIX = " [truncated]"
logger = logging.getLogger("mutiny_bot.llm")


class LLMHandler:
    """Handles LLM interactions with litellm and Ollama."""

    def __init__(
        self,
        api_base: str,
        tool_functions: Optional[dict[str, Callable[..., Any]]] = None,
    ):
        self.api_base = api_base
        self.tool_functions = tool_functions if tool_functions is not None else {}

    @staticmethod
    def _extract_first_message(response: Any) -> Any:
        """Safely extract the first completion message object."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return None
        return getattr(choices[0], "message", None)

    async def generate_response(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list] = None,
    ) -> str:
        """Generate a response from the LLM, handling tool calls if present."""
        # Ensure model has provider prefix for litellm
        if not model.startswith("ollama/"):
            model = f"ollama/{model}"
            
        completion_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "api_base": self.api_base,
            "stream": False,
        }
        if tools:
            completion_kwargs["tools"] = tools

        # If the conversation is long, summarize earlier history to keep context small.
        # Be defensive: validate that the first message is a system prompt. If so,
        # merge the generated summary into that single system message. If not,
        # create a new system message containing the summary. If summarization
        # fails, fall back to the original messages unchanged.
        if len(messages) > MAX_HISTORY_MESSAGES:
            try:
                summary = await self.summarize_history(model, messages)
            except Exception:
                logger.exception("History summarization failed, continuing without summary")
                summary = None

            if summary:
                tail = messages[-MAX_TAIL_MESSAGES:]
                # If first message is system, merge the summary into it to avoid
                # inserting multiple system messages which some models reject.
                if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
                    first_sys = messages[0].copy()
                    first_sys["content"] = (first_sys.get("content", "") + "\n\nPrevious conversation summary: " + summary).strip()
                    messages = [first_sys] + tail
                else:
                    # No system message present; create a single system message with the summary.
                    messages = [{"role": "system", "content": f"Previous conversation summary: {summary}"}] + tail

                completion_kwargs["messages"] = messages

        try:
            response = await litellm.acompletion(**completion_kwargs)
        except Exception:
            logger.exception("Primary LLM completion failed")
            return "I could not reach the local AI model right now. Please try again shortly."

        ai_message = self._extract_first_message(response)
        if ai_message is None:
            return ""
        tool_calls = getattr(ai_message, "tool_calls", None) or []

        if tool_calls:
            history_tool_calls: list[dict[str, object]] = []
            for tool_call in tool_calls:
                function_data = getattr(tool_call, "function", None)
                history_tool_calls.append(
                    {
                        "id": getattr(tool_call, "id", ""),
                        "type": getattr(tool_call, "type", "function"),
                        "function": {
                            "name": getattr(function_data, "name", ""),
                            "arguments": getattr(function_data, "arguments", "{}"),
                        },
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": ai_message.content or "",
                    "tool_calls": history_tool_calls,
                }
            )

            for tool_call in tool_calls:
                function_data = getattr(tool_call, "function", None)
                tool_name = getattr(function_data, "name", "")
                raw_arguments = getattr(function_data, "arguments", "{}") or "{}"

                try:
                    if isinstance(raw_arguments, str):
                        parsed_arguments = json.loads(raw_arguments)
                    elif isinstance(raw_arguments, dict):
                        parsed_arguments = raw_arguments
                    else:
                        parsed_arguments = {}

                    if not isinstance(parsed_arguments, dict):
                        parsed_arguments = {}
                except json.JSONDecodeError:
                    parsed_arguments = {}

                tool_function = self.tool_functions.get(tool_name)
                if tool_function is None:
                    result = f"Tool not found: {tool_name}"
                else:
                    try:
                        maybe_result = tool_function(**parsed_arguments)
                        if isawaitable(maybe_result):
                            result = await asyncio.wait_for(maybe_result, timeout=30.0)
                        else:
                            result = maybe_result
                    except asyncio.TimeoutError:
                        result = f"Tool execution timed out: {tool_name}"
                    except Exception as tool_error:
                        result = f"Tool execution failed: {tool_error}"

                tool_result_text = str(result)
                if len(tool_result_text) > MAX_TOOL_RESULT_CHARS:
                    max_prefix_len = MAX_TOOL_RESULT_CHARS - len(TOOL_RESULT_TRUNCATION_SUFFIX)
                    tool_result_text = f"{tool_result_text[:max_prefix_len]}{TOOL_RESULT_TRUNCATION_SUFFIX}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_result_text,
                    }
                )

            try:
                final_response = await litellm.acompletion(
                    model=model,
                    api_base=self.api_base,
                    messages=messages,
                )
            except Exception:
                logger.exception("Final LLM completion failed after tool calls")
                return "I ran the tool request but could not complete the final AI response."
            ai_message = self._extract_first_message(final_response)
            if ai_message is None:
                return ""

        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")

    # Streaming responses are handled outside this handler now; remove astream_response.

    async def summarize_history(self, model: str, full_history: list[dict[str, Any]]) -> str:
        """Summarize the conversation history excluding the last 8 messages.

        Returns a short one-sentence summary using the same style as other LLM calls.
        """
        # Ensure model has provider prefix for litellm
        if not model.startswith("ollama/"):
            model = f"ollama/{model}"
            
        # Exclude the last 8 messages to keep the summary focused on earlier context
        history_to_summarize = (
            full_history[:-MAX_TAIL_MESSAGES] if len(full_history) > MAX_TAIL_MESSAGES else []
        )

        # Build a concise system prompt asking for a one-sentence summary
        system_prompt = (
            "You are a concise assistant. Provide a single short sentence summarizing the following conversation history. "
            "Be factual and brief."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            *history_to_summarize,
            {"role": "user", "content": "Summarize the conversation above in one short sentence."},
        ]

        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                api_base=self.api_base,
                stream=False,
            )
        except Exception:
            logger.exception("History summarization completion failed")
            return ""
        ai_message = self._extract_first_message(response)
        if ai_message is None:
            return ""
        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")