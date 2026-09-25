"""LLM handler for MutinyBot using litellm and Ollama."""

import asyncio
import json
import logging
from collections.abc import Callable
from inspect import isawaitable
from typing import Any, Optional

from core.privacy import bootstrap

bootstrap()

import litellm
from config import DEFAULT_SYSTEM_PROMPT, MAX_HISTORY_MESSAGES


MAX_TAIL_MESSAGES = 8
MAX_TOOL_RESULT_CHARS = 4000
TOOL_RESULT_TRUNCATION_SUFFIX = " [truncated]"
logger = logging.getLogger("mutiny_bot.llm")


class LLMError(Exception):
    """Structured inference failure. The message is safe to show in the UI."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _local_model_id(model: str) -> str:
    from config import ollama_endpoint_error
    from llm.models import is_eligible_local_model

    name = str(model or "").strip()
    if name.startswith("ollama/"):
        name = name[len("ollama/") :]
    if not is_eligible_local_model(name):
        raise LLMError("model_not_local", "Only a local Ollama model can be used.", retryable=False)
    return f"ollama/{name}"


def _allowed_tool_names(tools: Optional[list[dict[str, Any]]]) -> set[str]:
    names: set[str] = set()
    for schema in tools or []:
        function = schema.get("function") if isinstance(schema, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


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

    async def generate_response(self, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> str:
        from config import ollama_endpoint_error

        endpoint_error = ollama_endpoint_error(self.api_base)
        if endpoint_error:
            raise LLMError("ollama_not_local", "Ollama must stay on a loopback address.", retryable=False)
        model = _local_model_id(model)
        allowed_tools = _allowed_tool_names(tools)

        # Use the system message already present in messages (set by the caller from the DB).
        # If none was provided, inject the default so there is always a system prompt.
        if not any(msg.get("role") == "system" for msg in messages):
            messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}] + list(messages)

        completion_kwargs = {
            "model": model,
            "messages": messages,
            "api_base": self.api_base,
            "stream": False,
            "max_tokens": 800,
            "timeout": 45.0,
        }
        if tools:
            completion_kwargs["tools"] = tools
            # We don't strictly force auto if it causes issues, but we'll include it.
            completion_kwargs["tool_choice"] = "auto"

        # Call LiteLLM
        try:
            response = await litellm.acompletion(**completion_kwargs)
        except Exception as exc:
            logger.error("LiteLLM completion failed: %s", exc.__class__.__name__)
            raise LLMError(
                "model_unavailable",
                "The local model could not be reached.",
                retryable=True,
            ) from exc

        ai_message = self._extract_first_message(response)
        if ai_message is None:
            raise LLMError("empty_response", "The local model returned no response.", retryable=True)

        # NUCLEAR SANITIZER – kill any tool call leakage
        if getattr(ai_message, "tool_calls", None):
            history_tool_calls: list[dict[str, Any]] = []
            tool_results: list[tuple[str, str, str]] = []

            # Execute all requested tools in this turn and include each result in history.
            for index, tool_call in enumerate(ai_message.tool_calls):
                function_data = getattr(tool_call, "function", None)
                tool_name = getattr(function_data, "name", "")
                raw_arguments = getattr(function_data, "arguments", "{}") or "{}"

                if tool_name not in allowed_tools:
                    tool_result = f"Tool '{tool_name}' is not allowed."
                else:
                    try:
                        tool_result = await self.execute_tool(tool_name, raw_arguments)
                    except asyncio.TimeoutError:
                        tool_result = f"Tool '{tool_name}' timed out."

                tool_result_str = str(tool_result)
                if len(tool_result_str) > MAX_TOOL_RESULT_CHARS:
                    trunc_len = MAX_TOOL_RESULT_CHARS - len(TOOL_RESULT_TRUNCATION_SUFFIX)
                    tool_result_str = tool_result_str[:trunc_len] + TOOL_RESULT_TRUNCATION_SUFFIX

                # Re-create a tool call dict that liteLLM expects in assistant context.
                tool_call_id = getattr(tool_call, "id", "") or f"call_{tool_name}_{index}"
                history_tool_call = {
                    "id": tool_call_id,
                    "type": getattr(tool_call, "type", "function"),
                    "function": {
                        "name": tool_name,
                        "arguments": raw_arguments,
                    },
                }
                history_tool_calls.append(history_tool_call)
                tool_results.append((tool_call_id, tool_name, tool_result_str))

            # Make a second call with all tool results to get a clean final answer.
            messages.append(
                {
                    "role": "assistant",
                    "content": getattr(ai_message, "content", "") or "",
                    "tool_calls": history_tool_calls,
                }
            )
            for tool_call_id, tool_name, tool_result_str in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": tool_result_str,
                    }
                )
            
            try:
                final_response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    api_base=self.api_base,
                    max_tokens=800,
                    timeout=45.0,
                )
            except Exception as exc:
                logger.error("LiteLLM tool follow-up failed: %s", exc.__class__.__name__)
                raise LLMError(
                    "model_unavailable",
                    "The local model could not be reached.",
                    retryable=True,
                ) from exc
            
            final_msg = self._extract_first_message(final_response)
            clean_text = (getattr(final_msg, "content", "") or "").strip()
        else:
            clean_text = (getattr(ai_message, "content", "") or "").strip()

        return clean_text

    async def execute_tool(self, tool_name: str, raw_arguments: str) -> Any:
        try:
            if isinstance(raw_arguments, str):
                parsed_arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                parsed_arguments = raw_arguments
            else:
                parsed_arguments = {}
        except Exception:
            parsed_arguments = {}

        if not isinstance(parsed_arguments, dict):
            parsed_arguments = {}

        tool_function = self.tool_functions.get(tool_name)
        if tool_function is None:
            return f"Tool execution failed: Tool not found: {tool_name}"

        try:
            maybe_result = tool_function(**parsed_arguments)
            if isawaitable(maybe_result):
                result = await asyncio.wait_for(maybe_result, timeout=30.0)
            else:
                result = maybe_result
            return result
        except asyncio.TimeoutError:
            raise
        except Exception as tool_error:
            logger.exception(f"Tool execution failed: {tool_error}")
            return "Tool execution failed due to an internal error."

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
                max_tokens=150,
                timeout=15.0,
            )
        except Exception as exc:
            logger.error("History summarization failed: %s", exc.__class__.__name__)
            return ""
        ai_message = self._extract_first_message(response)
        if ai_message is None:
            return ""
        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")