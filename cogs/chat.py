"""Chat cog for MutinyBot message handling."""

import logging
import asyncio

import discord
from discord.ext import commands

from config import ALLOWED_MODELS, DEFAULT_MODEL
from tools.registry import TOOL_SCHEMAS


# Characters per edit — slow enough to stay well inside Discord's rate limit.
CHUNK_SIZE = 900

logger = logging.getLogger("mutiny_bot")


def should_enable_tools(user_text: str) -> bool:
    """Enable tool-calling only when user intent looks automation-related."""
    normalized = (user_text or "").lower()
    automation_markers = (
        "automation",
        "schedule",
        "scheduled",
        "daily",
        "every day",
        "morning brief",
        "morning briefing",
        "briefing",
        "list active",
        "active automations",
        "stop automation",
        "cancel automation",
        "job id",
    )
    return any(marker in normalized for marker in automation_markers)


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


def split_response_chunks(text: str, max_chunk_size: int = 1950) -> list[str]:
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

        chunk = candidate.rstrip()
        if not chunk:
            candidate = remaining[:max_chunk_size]
            chunk = candidate

        chunks.append(chunk)
        # Advance by candidate length (before rstrip) so stripped trailing
        # whitespace is not re-processed in the next iteration.
        remaining = remaining[len(candidate) :].lstrip("\n")

    return chunks


class ChatCog(commands.Cog):
    """Cog for handling chat messages and AI responses."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for incoming messages while avoiding self-replies."""
        # Ignore messages from this bot to prevent response loops.
        if message.author == self.bot.user:
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
            user_history = await self.bot.db_manager.get_recent_history(user_id=user_id, limit=10)
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

            tools = TOOL_SCHEMAS if TOOL_SCHEMAS and should_enable_tools(message.content) else None
            if tools:
                async with message.channel.typing():
                    ai_text = await self.bot.llm_handler.generate_response(
                        model=active_model,
                        messages=messages_for_ai,
                        tools=tools
                    )
                await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=ai_text)
                # Send the tool-assisted response to the channel.
                for chunk in split_response_chunks(ai_text):
                    await message.channel.send(chunk)
            else:
                response_msg = await message.channel.send("Thinking...")

                async with message.channel.typing():
                    ai_text = await self.bot.llm_handler.generate_response(
                        model=active_model,
                        messages=messages_for_ai,
                        tools=None,
                    )

                await self.bot.db_manager.insert_history_message(user_id=user_id, role="assistant", content=ai_text)

                full_response = ai_text or ""
                if not full_response:
                    await response_msg.edit(content="I could not generate a response this time.")
                else:
                    assembled = ""
                    for i in range(0, len(full_response), CHUNK_SIZE):
                        assembled += full_response[i : i + CHUNK_SIZE]
                        await response_msg.edit(content=assembled)
                        await asyncio.sleep(0.25)   # 4 edits/second — safely under Discord's 5/second limit
        except Exception as error:
            logger.exception("AI message handling failed")
            await message.channel.send(
                "Sorry, I hit an AI error and could not respond right now. "
                f"({type(error).__name__}: {error})"
            )

        # Process commands so prefix commands still work when on_message is defined.
        await self.bot.process_commands(message)

    # Streaming and stop-button support removed — responses are generated synchronously via generate_response.


async def setup(bot):
    """Add the cog to the bot."""
    await bot.add_cog(ChatCog(bot))