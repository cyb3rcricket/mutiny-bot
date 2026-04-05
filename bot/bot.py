"""MutinyBot class definition."""

import importlib
import os
from inspect import isawaitable

import discord
from discord.ext import commands
from config import (
    ALLOWED_MODELS,
    BROADCAST_CHANNEL_ID,
    DB_PATH,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    OLLAMA_API_BASE,
    intents,
)
from config import MONITORING_CHANNEL_ID
from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from scheduler.scheduler_manager import SchedulerManager
from tools.registry import AVAILABLE_TOOLS, TOOL_SCHEMAS


class MutinyBot(commands.Bot):
    """Custom bot class to register slash commands during startup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_manager = DatabaseManager(DB_PATH, self)
        self.llm_handler = LLMHandler(OLLAMA_API_BASE)
        self.scheduler_manager = SchedulerManager(self)

    async def setup_hook(self) -> None:
        tools_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
        # Only load real AI tool modules from the tools/ folder
        for filename in sorted(os.listdir(tools_dir)):
            if not filename.endswith(".py") or filename in {"__init__.py", "registry.py", "scheduler_manager.py"}:
                continue

            module_name = f"tools.{filename[:-3]}"
            module = importlib.import_module(module_name)
            setup_fn = getattr(module, "setup", None)
            if callable(setup_fn):
                maybe_result = setup_fn(self)
                if isawaitable(maybe_result):
                    await maybe_result
            print(f"Loaded tool module: {filename}")

        await self.db_manager.setup_database()
        await self.scheduler_manager.start_scheduler()
        # Sync slash commands so they are available in Discord.
        await self.tree.sync()
        self.scheduler_manager.start_broadcast_task()
        await self.load_extension("cogs.chat")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.tools")
        await self.load_extension("cogs.monitoring")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Run once when the bot has connected to Discord successfully."""
        print("MutinyBot is online and ready to disrupt!")