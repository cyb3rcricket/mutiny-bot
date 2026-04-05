"""MutinyBot: a clean foundation Discord bot using discord.py."""

import importlib
import json
import logging
import os
from inspect import isawaitable

import aiosqlite
import discord
import litellm
# TURN ON THE X-RAY
litellm._turn_on_debug()
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands, tasks
from tools.registry import AVAILABLE_TOOLS, TOOL_SCHEMAS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutiny_bot")

# Load environment variables from a local .env file.
load_dotenv()

# Read the Discord bot token from environment variables.
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BROADCAST_CHANNEL_ID = int(os.getenv("BROADCAST_CHANNEL_ID", "0"))
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
DEFAULT_MODEL = "ollama/qwen2.5-coder:7b"
DEFAULT_SYSTEM_PROMPT = (
    "You are MutinyBot, a practical IT admin assistant. "
    "Be concise, technical, and action-oriented."
)
DB_PATH = "mutiny.db"
ALLOWED_MODELS = {
    "ollama/qwen2.5-coder:7b",
    "ollama/phi4-mini",
    "ollama/llama3.1",
}

if not TOKEN:
    raise ValueError(
        "DISCORD_BOT_TOKEN is missing. Add it to your .env file before starting the bot."
    )

# Start with default intents and explicitly enable message content access.
intents = discord.Intents.default()
intents.message_content = True


class MutinyBot(commands.Bot):
    """Custom bot class to register slash commands during startup."""

    async def setup_hook(self) -> None:
        tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        for filename in sorted(os.listdir(tools_dir)):
            if not filename.endswith(".py"):
                continue
            if filename in {"__init__.py", "registry.py"}:
                continue

            module_name = f"tools.{filename[:-3]}"
            module = importlib.import_module(module_name)
            setup_fn = getattr(module, "setup", None)
            if callable(setup_fn):
                maybe_result = setup_fn(bot)
                if isawaitable(maybe_result):
                    await maybe_result
            print(f"Loaded tool module: {filename}")

        await setup_database()
        if not bot.scheduler.running:
            bot.scheduler.start()
        # Sync slash commands so they are available in Discord.
        await self.tree.sync()
        if not check_broadcast_queue.is_running():
            check_broadcast_queue.start()


# Create the bot client with command prefix support.
bot = MutinyBot(command_prefix="!", intents=intents)
bot.scheduler = AsyncIOScheduler()


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
            chunk = remaining[:max_chunk_size]

        chunks.append(chunk)
        remaining = remaining[len(chunk) :].lstrip("\n")

    return chunks


async def setup_database() -> None:
    """Create the SQLite database schema and indexes if they do not exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                user_id TEXT,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_history_user_timestamp
            ON chat_history (user_id, timestamp)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO bot_config (key, value)
            VALUES ('model', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (DEFAULT_MODEL,),
        )
        await db.execute(
            """
            INSERT INTO bot_config (key, value)
            VALUES ('system_prompt', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (DEFAULT_SYSTEM_PROMPT,),
        )
        await db.commit()


async def update_config(key: str, value: str) -> None:
    """Safely insert or update a bot_config setting."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_config (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_config(key: str, default: str) -> str:
    """Read a bot_config value with fallback and automatic default persistence."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
        row = await cursor.fetchone()

    if not row or not row[0]:
        await update_config(key, default)
        return default

    return str(row[0])


async def get_current_model() -> str:
    """Read and validate the configured model from SQLite."""
    selected_model = await get_config("model", DEFAULT_MODEL)

    if selected_model not in ALLOWED_MODELS:
        await update_config("model", DEFAULT_MODEL)
        return DEFAULT_MODEL

    return selected_model


async def get_system_prompt() -> str:
    """Read the active system prompt from SQLite."""
    return await get_config("system_prompt", DEFAULT_SYSTEM_PROMPT)


def format_db_size() -> str:
    """Format mutiny.db size in KB/MB for status display."""
    if not os.path.exists(DB_PATH):
        return "0 KB"

    size_bytes = os.path.getsize(DB_PATH)
    size_kb = size_bytes / 1024
    if size_kb >= 1024:
        return f"{size_kb / 1024:.2f} MB"
    return f"{size_kb:.2f} KB"


async def insert_history_message(user_id: str, role: str, content: str) -> None:
    """Persist one conversation message for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await db.commit()


async def get_recent_history(user_id: str, limit: int = 10) -> list[dict[str, str]]:
    """Read the most recent messages for a user in chronological order."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT role, content
            FROM (
                SELECT role, content, timestamp, rowid
                FROM chat_history
                WHERE user_id = ?
                ORDER BY timestamp DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, rowid ASC
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()

    return [{"role": row[0], "content": row[1]} for row in rows]


@tasks.loop(seconds=2)
async def check_broadcast_queue() -> None:
    """Send queued manual broadcast messages to the configured Discord channel."""
    if BROADCAST_CHANNEL_ID <= 0:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, content FROM broadcast_queue ORDER BY id ASC LIMIT 1"
        )
        row = await cursor.fetchone()

    if not row:
        return

    message_id = int(row[0])
    content = str(row[1] or "").strip()
    if not content:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM broadcast_queue WHERE id = ?", (message_id,))
            await db.commit()
        return

    channel = bot.get_channel(BROADCAST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(BROADCAST_CHANNEL_ID)
        except Exception:
            return

    try:
        await channel.send(content)
    except Exception:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM broadcast_queue WHERE id = ?", (message_id,))
        await db.commit()


@check_broadcast_queue.before_loop
async def before_check_broadcast_queue() -> None:
    """Wait for bot readiness before polling broadcast queue."""
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    """Run once when the bot has connected to Discord successfully."""
    print("MutinyBot is online and ready to disrupt!")


@bot.tree.command(name="model", description="Switch MutinyBot's AI model")
@app_commands.choices(
    model_choice=[
        app_commands.Choice(
            name="ollama/qwen2.5-coder:7b",
            value="ollama/qwen2.5-coder:7b",
        ),
        app_commands.Choice(
            name="ollama/phi4-mini",
            value="ollama/phi4-mini",
        ),
        app_commands.Choice(
            name="ollama/llama3.1",
            value="ollama/llama3.1",
        ),
    ]
)
async def model_command(
    interaction: discord.Interaction, model_choice: app_commands.Choice[str]
) -> None:
    """Switch the active local model used for replies."""
    await update_config("model", model_choice.value)
    await interaction.response.send_message(
        f"Mainframe re-routed. Now using: {model_choice.value}"
    )


@bot.tree.command(name="personality", description="Set MutinyBot system personality prompt")
@app_commands.describe(prompt_text="New system prompt for MutinyBot")
async def personality_command(interaction: discord.Interaction, prompt_text: str) -> None:
    """Update system prompt steering via ChatOps."""
    cleaned_prompt = prompt_text.strip()
    if not cleaned_prompt:
        await interaction.response.send_message(
            "Prompt cannot be empty.", ephemeral=True
        )
        return

    await update_config("system_prompt", cleaned_prompt)
    await interaction.response.send_message(
        "Personality updated successfully.",
    )


@bot.tree.command(name="status", description="Show current model, personality, and DB size")
async def status_command(interaction: discord.Interaction) -> None:
    """Show bot runtime status in an embed (dashboard replacement)."""
    active_model = await get_current_model()
    system_prompt = await get_system_prompt()
    db_size = format_db_size()

    prompt_snippet = system_prompt if len(system_prompt) <= 300 else f"{system_prompt[:300]}..."

    embed = discord.Embed(
        title="MutinyBot Status",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Active Model", value=active_model, inline=False)
    embed.add_field(name="Personality Snippet", value=prompt_snippet, inline=False)
    embed.add_field(name="Database Size", value=db_size, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_message(message: discord.Message) -> None:
    """Listen for incoming messages while avoiding self-replies."""
    # Ignore messages from this bot to prevent response loops.
    if message.author == bot.user:
        return

    user_id = str(message.author.id)

    if is_automation_capabilities_question(message.content):
        capability_reply = build_automation_capabilities_message()
        await insert_history_message(user_id=user_id, role="user", content=message.content)
        await insert_history_message(user_id=user_id, role="assistant", content=capability_reply)
        for chunk in split_response_chunks(capability_reply):
            await message.channel.send(chunk)
        await bot.process_commands(message)
        return

    # Send the user's message to the AI model and return its response.
    try:
        await insert_history_message(user_id=user_id, role="user", content=message.content)
        user_history = await get_recent_history(user_id=user_id, limit=10)
        active_model = await get_config("model", DEFAULT_MODEL)
        if active_model not in ALLOWED_MODELS:
            active_model = DEFAULT_MODEL
            await update_config("model", DEFAULT_MODEL)

        fetched_system_prompt = await get_config("system_prompt", DEFAULT_SYSTEM_PROMPT)
        bot_config = {
            "model": active_model,
            "system_prompt": fetched_system_prompt,
        }
        messages_for_ai = [
            {"role": "system", "content": bot_config["system_prompt"]},
            *user_history,
        ]

        completion_kwargs = {"model": bot_config["model"], "messages": messages_for_ai}
        completion_kwargs["api_base"] = OLLAMA_API_BASE
        if TOOL_SCHEMAS and should_enable_tools(message.content):
            completion_kwargs["tools"] = TOOL_SCHEMAS

        async with message.channel.typing():
            response = await litellm.acompletion(**completion_kwargs)

            ai_message = response.choices[0].message
            tool_calls = getattr(ai_message, "tool_calls", None) or []
            tool_executed = False

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

                # Keep assistant tool-call request in local context for the follow-up call.
                messages_for_ai.append(
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
                        tool_executed = True
                        try:
                            maybe_result = tool_function(**parsed_arguments)
                            result = await maybe_result if isawaitable(maybe_result) else maybe_result
                        except Exception as tool_error:
                            result = f"Tool execution failed: {tool_error}"

                    messages_for_ai.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": str(result),
                        }
                    )

                if tool_executed:
                    final_response = await litellm.acompletion(
                        model=bot_config["model"],
                        api_base=OLLAMA_API_BASE,
                        messages=messages_for_ai,
                    )
                    ai_message = final_response.choices[0].message

        ai_text = ai_message.content
        ai_text = ai_text if isinstance(ai_text, str) else str(ai_text or "")
        await insert_history_message(user_id=user_id, role="assistant", content=ai_text)

        for chunk in split_response_chunks(ai_text):
            await message.channel.send(chunk)
    except Exception as error:
        logger.exception("AI message handling failed")
        await message.channel.send(
            "Sorry, I hit an AI error and could not respond right now. "
            f"({type(error).__name__}: {error})"
        )

    # Process commands so prefix commands still work when on_message is defined.
    await bot.process_commands(message)


if __name__ == "__main__":
    # Connect the bot to Discord using the secure token from .env.
    bot.run(TOKEN)
