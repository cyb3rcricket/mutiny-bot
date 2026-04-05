"""LLM handler for MutinyBot using litellm and Ollama."""

import json
from inspect import isawaitable
from typing import AsyncIterator, Optional

import litellm
from tools.registry import AVAILABLE_TOOLS
from config import MAX_HISTORY_MESSAGES


class LLMHandler:
    """Handles LLM interactions with litellm and Ollama."""

    def __init__(self, api_base: str):
        self.api_base = api_base

    async def generate_response(self, model: str, messages: list, tools: Optional[list] = None) -> str:
        """Generate a response from the LLM, handling tool calls if present."""
        completion_kwargs = {"model": model, "messages": messages, "api_base": self.api_base, "stream": False}
        if tools:
            completion_kwargs["tools"] = tools

        # If the conversation is long, summarize earlier history to keep context small
        if len(messages) > MAX_HISTORY_MESSAGES:
            summary = await self.summarize_history(model, messages)
            messages = [messages[0]] + [{"role": "system", "content": f"Previous conversation summary: {summary}"}] + messages[-8:]
            completion_kwargs["messages"] = messages

        response = await litellm.acompletion(**completion_kwargs)

        ai_message = response.choices[0].message
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

                tool_function = AVAILABLE_TOOLS.get(tool_name)
                if tool_function is None:
                    result = f"Tool not found: {tool_name}"
                else:
                    try:
                        maybe_result = tool_function(**parsed_arguments)
                        result = await maybe_result if isawaitable(maybe_result) else maybe_result
                    except Exception as tool_error:
                        result = f"Tool execution failed: {tool_error}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": str(result),
                    }
                )

            final_response = await litellm.acompletion(
                model=model,
                api_base=self.api_base,
                messages=messages,
            )
            ai_message = final_response.choices[0].message

        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")

    # Streaming responses are handled outside this handler now; remove astream_response.

    async def summarize_history(self, model: str, full_history: list) -> str:
        """Summarize the conversation history excluding the last 8 messages.

        Returns a short one-sentence summary using the same style as other LLM calls.
        """
        # Exclude the last 8 messages to keep the summary focused on earlier context
        history_to_summarize = full_history[:-8] if len(full_history) > 8 else []

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

        response = await litellm.acompletion(model=model, messages=messages, api_base=self.api_base, stream=False)
        ai_message = response.choices[0].message
        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")