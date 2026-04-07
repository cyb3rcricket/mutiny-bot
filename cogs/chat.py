"""Chat cog for MutinyBot message handling."""

import asyncio
import json
import logging
import re
from typing import Any

import discord
from discord.ext import commands

from config import ALLOWED_MODELS, DEFAULT_MODEL
from tools.scheduler_manager import reset_tool_request_context, set_tool_request_context
from tools.registry import TOOL_SCHEMAS

logger = logging.getLogger("mutiny_bot")

# Discord has a 2000-character hard limit; keep a small safety margin.
DISCORD_SAFE_MESSAGE_CHARS = 1950
# Stream responses in smaller edits to keep updates snappy.
STREAM_EDIT_CHUNK_SIZE = 900
USER_RATE_LIMIT_MESSAGES = 5
USER_RATE_LIMIT_WINDOW_SECONDS = 15.0
MAX_CONCURRENT_LLM_REQUESTS = 2
COMMAND_TEXT_RE = re.compile(r"^[\s\ufeff\u200b\u200c\u200d]*/\S+")


def is_automation_capabilities_question(user_text: str) -> bool:
    """Detect direct questions about what automations the bot can perform."""
    normalized = (user_text or "").lower()
    capability_markers = (
        "what kinds of automations",
        "what automations can you do",
        "what automation can you do",
        "what can you automate",
        "automation capabilities",
        "which automations",
    )
    return any(marker in normalized for marker in capability_markers)


def build_automation_capabilities_message() -> str:
    """Return a reliable summary of supported automation features."""
    return (
        "I can run and manage these automations:\n"
        "1. get_morning_briefing: Generate a local-only operations briefing.\n"
        "2. schedule_daily_automation: Schedule a registered tool daily at a chosen time.\n"
        "3. list_active_automations: Show all currently scheduled jobs.\n"
        "4. stop_automation: Cancel a scheduled job by its job ID.\n\n"
        "Examples:\n"
        "- 'Schedule get_morning_briefing daily at 7:00'\n"
        "- 'List my active automations'\n"
        "- 'Stop automation auto_get_morning_briefing_...'")


def _is_raw_json_response(text: str) -> bool:
    """Check if the response is raw JSON (structural/tool-like output instead of natural text)."""
    if not text or not text.strip().startswith("{"):
        return False
    
    try:
        obj = json.loads(text.strip())
        # If it parsed as JSON and has no obvious tool structure, it's likely a formatting error
        # Real tool calls would go through the LLM handler's tool_calls pathway, not here
        return isinstance(obj, dict) and not obj.get("type") and not obj.get("id")
    except (json.JSONDecodeError, ValueError):
        return False


async def _convert_json_to_natural_language(
    llm_handler: Any,
    model: str,
    json_response: str,
    original_user_message: str,
    conversation_history: list[dict[str, Any]],
) -> str:
    """Convert a malformed JSON response to natural language using the LLM."""
    try:
        # Use a clearer, more direct system prompt that emphasizes being MutinyBot
        recovery_messages = [
            {"role": "system", "content": "You are MutinyBot, an IT admin assistant. You are responding directly to a user. Respond naturally and conversationally - no JSON output. Be helpful and professional."},
            {"role": "user", "content": original_user_message},
        ]
        
        recovery_response = await llm_handler.generate_response(
            model=model,
            messages=recovery_messages,
            tools=None,  # Don't use tools for recovery
        )
        return recovery_response.strip() if recovery_response else "Hello! How can I help?"
    except Exception as e:
        logger.warning(f"Failed to recover from JSON response: {e}")
        # Fallback to a generic response
        return "Hello! How can I help?"


def split_response_chunks(text: str, max_chunk_size: int = DISCORD_SAFE_MESSAGE_CHARS) -> list[str]:
    """Split long text into Discord-safe chunks, preferring newline boundaries."""
    if not text:
        return ["I could not generate a response this time."]

    chunks: list[str] = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= max_chunk_size:
            chunks.append(remaining)
            break

        window = remaining[:max_chunk_size]

        # Prefer splitting on newlines to keep paragraphs and code blocks readable.
        split_at = window.rfind("\n")
        if split_at == -1:
            split_at = window.rfind(" ")

        # If no useful natural breakpoint was found, hard split at the limit.
        if split_at < max_chunk_size // 2:
            split_at = max_chunk_size

        candidate = remaining[:split_at]

        # Try to avoid cutting in the middle of a fenced code block when possible.
        if split_at != max_chunk_size and candidate.count("```") % 2 == 1:
            earlier_break = candidate.rfind("\n", 0, candidate.rfind("```"))
            if earlier_break > 0:
                candidate = remaining[:earlier_break]

        # Remember how many characters we intended to consume (the candidate slice).
        consumed_len = len(candidate)

        chunk = candidate.rstrip()
        if not chunk:
            # If stripping made the chunk empty, fall back to a hard max-sized slice
            chunk = remaining[:max_chunk_size]
            consumed_len = len(chunk)

        chunks.append(chunk)
        # Advance by the original candidate length (or the fallback slice length)
        remaining = remaining[consumed_len :].lstrip("\n")

    return chunks


class ChatCog(commands.Cog):
    """Cog for handling chat messages and AI responses."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._rate_limiter = commands.CooldownMapping.from_cooldown(
            USER_RATE_LIMIT_MESSAGES,
            USER_RATE_LIMIT_WINDOW_SECONDS,
            commands.BucketType.user,
        )
        self._llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)

    @staticmethod
    def _is_admin_user(message: discord.Message) -> bool:
        """Return True when the author has manage-guild or administrator permissions."""
        if not message.guild:
            return False
        if not isinstance(message.author, discord.Member):
            return False

        perms = message.author.guild_permissions
        return bool(perms and (perms.manage_guild or perms.administrator))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for incoming messages while avoiding self-replies."""
        # Ignore all bot authors to prevent bot-to-bot loops.
        if message.author.bot:
            return

        # Treat slash-like text as a command hint instead of an AI prompt.
        if COMMAND_TEXT_RE.match(message.content or ""):
            await message.channel.send(
                "Use Discord's slash-command menu for app commands like `/sync-commands` and `/post-commands`."
            )
            return

        bucket = self._rate_limiter.get_bucket(message)
        retry_after = bucket.update_rate_limit() if bucket else None
        if retry_after:
            await message.channel.send(
                f"You are sending requests too quickly. Try again in {retry_after:.1f}s."
            )
            await self.bot.process_commands(message)
            return

        user_id = str(message.author.id)

        if is_automation_capabilities_question(message.content):
            capability_reply = build_automation_capabilities_message()
            await self.bot.db_manager.insert_history_message(user_id=user_id, role="user", content=message.content)
            await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=capability_reply)
            for chunk in split_response_chunks(capability_reply):
                await message.channel.send(chunk)
            await self.bot.process_commands(message)
            return

        # Send the user's message to the AI model and return its response.
        try:
            await self.bot.db_manager.insert_history_message(user_id=user_id, role="user", content=message.content)
            user_history = await self.bot.db_manager.get_user_recent_history(user_id=user_id, limit=10)
            active_model = await self.bot.db_manager.get_current_model()
            if active_model not in ALLOWED_MODELS:
                active_model = DEFAULT_MODEL
                await self.bot.db_manager.update_config("model", DEFAULT_MODEL)

            fetched_system_prompt = await self.bot.db_manager.get_system_prompt()
            bot_config = {
                "model": active_model,
                "system_prompt": fetched_system_prompt,
            }
            messages_for_ai = [
                {"role": "system", "content": bot_config["system_prompt"]},
                *user_history,
            ]

            # Always pass tool schemas; the model decides when to call tools.
            tools = TOOL_SCHEMAS if TOOL_SCHEMAS else None
            if tools:
                context_tokens = set_tool_request_context(
                    user_id=user_id,
                    is_admin=self._is_admin_user(message),
                    scheduler=self.bot.scheduler_manager.scheduler,
                )
                try:
                    async with self._llm_semaphore:
                        async with message.channel.typing():
                            ai_text = await self.bot.llm_handler.generate_response(
                                model=active_model,
                                messages=messages_for_ai,
                                tools=tools,
                            )
                finally:
                    reset_tool_request_context(context_tokens)

                await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=ai_text)

                # Ensure tool-enabled AI responses are sent to the Discord channel.
                # Use the existing splitter to keep messages Discord-safe and readable.
                full_response = ai_text or ""
                
                # Detect and recover from JSON responses that should be natural language
                if _is_raw_json_response(full_response):
                    logger.info(f"Detected JSON response that should be natural language: {full_response[:100]}")
                    full_response = await _convert_json_to_natural_language(
                        llm_handler=self.bot.llm_handler,
                        model=active_model,
                        json_response=full_response,
                        original_user_message=message.content,
                        conversation_history=messages_for_ai,
                    )
                    # Update the stored history with the corrected response
                    await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=full_response)
                
                for chunk in split_response_chunks(full_response):
                    await message.channel.send(chunk)
            else:
                response_msg = await message.channel.send("Thinking...")

                async with self._llm_semaphore:
                    async with message.channel.typing():
                        ai_text = await self.bot.llm_handler.generate_response(
                            model=active_model,
                            messages=messages_for_ai,
                            tools=None,
                        )

                await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=ai_text)

                full_response = (ai_text or "").strip()
                if not full_response:
                    await response_msg.edit(content="I could not generate a response this time.")
                else:
                    first_chunk = True
                    for i in range(0, len(full_response), STREAM_EDIT_CHUNK_SIZE):
                        chunk = full_response[i : i + STREAM_EDIT_CHUNK_SIZE]
                        if first_chunk:
                            await response_msg.edit(content=chunk)
                            first_chunk = False
                        else:
                            await response_msg.edit(content=(response_msg.content or "") + chunk)
                        await asyncio.sleep(0.08)   # tiny pause between edits for smoothness
        except Exception:
            logger.exception("AI message handling failed")
            await message.channel.send(
                "Sorry, I hit an AI error and could not respond right now. Please try again shortly."
            )

        # Process commands so prefix commands still work when on_message is defined.
        await self.bot.process_commands(message)

    # Streaming and stop-button support removed — responses are generated synchronously via generate_response.


async def setup(bot: Any) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(ChatCog(bot))