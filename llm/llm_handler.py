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

    async def generate_response(self, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> str:
        # NUCLEAR SYSTEM PROMPT – this MUST be the VERY FIRST message every single time
        SYSTEM_PROMPT = """You are MutinyBot, a direct, no-nonsense, zero-fluff Discord assistant built for Tommi.
You have real tools (morning briefing, news monitors, Eisenhower task prioritization, MemPalace, population lookup, etc.).
NEVER output raw JSON, function calls, tool schemas, or {"type": "function"...}.
NEVER say "simulate", "hypothetical", "for security reasons", "tool error", or mention knowledge cutoffs.
If you need to use a tool, use it internally and respond ONLY with the final clean answer.
Respond naturally and concisely in plain English. No code blocks unless explicitly asked."""

        # Force clean context - ensure the first message is our ironclad prompt
        filtered_messages = [msg for msg in messages if msg.get("role") != "system"]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + filtered_messages
        
        # Ensure model has provider prefix for litellm
        if not model.startswith("ollama/"):
            model = f"ollama/{model}"
        
        completion_kwargs = {
            "model": model,
            "messages": messages,
            "api_base": self.api_base,
            "stream": False,
        }
        if tools:
            completion_kwargs["tools"] = tools
            # We don't strictly force auto if it causes issues, but we'll include it.
            completion_kwargs["tool_choice"] = "auto"

        # Call LiteLLM
        try:
            response = await litellm.acompletion(**completion_kwargs)
        except Exception as e:
            logger.error(f"LiteLLM error: {e}")
            return "I encountered a core processor fault while generating a response (could not reach the local ai model)."

        ai_message = self._extract_first_message(response)
        if ai_message is None:
            return "I could not generate a response right now."

        # NUCLEAR SANITIZER – kill any tool call leakage
        if getattr(ai_message, "tool_calls", None):
            # Execute tool(s) silently
            tool_call = ai_message.tool_calls[0]
            function_data = getattr(tool_call, "function", None)
            tool_name = getattr(function_data, "name", "")
            raw_arguments = getattr(function_data, "arguments", "{}") or "{}"
            
            try:
                tool_result = await self.execute_tool(tool_name, raw_arguments)
            except asyncio.TimeoutError:
                tool_result = f"Tool '{tool_name}' timed out."
                
            tool_result_str = str(tool_result)
            if len(tool_result_str) > MAX_TOOL_RESULT_CHARS:
                trunc_len = MAX_TOOL_RESULT_CHARS - len(TOOL_RESULT_TRUNCATION_SUFFIX)
                tool_result_str = tool_result_str[:trunc_len] + TOOL_RESULT_TRUNCATION_SUFFIX
            
            # Re-create a tool call dict that liteLLM expects in the assistant's context
            history_tool_call = {
                "id": getattr(tool_call, "id", f"call_{tool_name}"),
                "type": getattr(tool_call, "type", "function"),
                "function": {
                    "name": tool_name,
                    "arguments": raw_arguments,
                },
            }
            
            # Now make a second call with the tool result to get final clean answer
            messages.append({"role": "assistant", "content": getattr(ai_message, "content", "") or "", "tool_calls": [history_tool_call]})
            messages.append({"role": "tool", "tool_call_id": history_tool_call["id"], "name": tool_name, "content": tool_result_str})
            
            final_response = await litellm.acompletion(
                model=model,
                messages=messages,
                api_base=self.api_base,
            )
            final_msg = self._extract_first_message(final_response)
            clean_text = (getattr(final_msg, "content", "") or "").strip()
        else:
            clean_text = (getattr(ai_message, "content", "") or "").strip()

        # FINAL SAFETY NET – strip any remaining JSON garbage
        import re
        clean_text = re.sub(r'```json\s*\{.*?\}\s*```', '', clean_text, flags=re.DOTALL).strip()
        
        if clean_text.startswith("{") and clean_text.endswith("}"):
            clean_text = "Sorry, internal error. Let me answer normally: I encountered a json leak and purged it."

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
            )
        except Exception:
            logger.exception("History summarization completion failed")
            return ""
        ai_message = self._extract_first_message(response)
        if ai_message is None:
            return ""
        ai_text = ai_message.content
        return ai_text if isinstance(ai_text, str) else str(ai_text or "")