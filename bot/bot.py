"""MutinyBot class definition."""

import importlib
import logging
import os
from inspect import isawaitable
from typing import Any

import discord
from discord.ext import commands
from discord import app_commands
import time
from collections import defaultdict
from config import DB_PATH, OLLAMA_API_BASE, intents
from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from scheduler.scheduler_manager import SchedulerManager
from tools.registry import AVAILABLE_TOOLS


logger = logging.getLogger("mutiny_bot")


class MutinyBot(commands.Bot):
    """Custom bot class to register slash commands during startup."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.db_manager = DatabaseManager(DB_PATH, self)
        self.llm_handler = LLMHandler(OLLAMA_API_BASE, tool_functions=AVAILABLE_TOOLS)
        self.scheduler_manager = SchedulerManager(self)
        self._app_commands_synced = False
        # Simple cooldown tracking: user_id -> last_command_time
        self.command_cooldowns = defaultdict(float)

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
            logger.info("Loaded tool module: %s", filename)

        await self.db_manager.setup_database()
        await self.scheduler_manager.start_scheduler()
        self.scheduler_manager.start_broadcast_task()
        await self.load_extension("cogs.chat")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.tools")
        await self.load_extension("cogs.monitoring")
        await self.load_extension("cogs.memory_palace_cog")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Global cooldown check for slash commands."""
        if isinstance(interaction, discord.Interaction) and interaction.type == discord.InteractionType.application_command:
            user_id = interaction.user.id
            current_time = time.time()
            last_used = self.command_cooldowns[user_id]
            
            if current_time - last_used < 3.0:
                # Cooldown active, raise error to be handled by on_app_command_error
                raise app_commands.CommandOnCooldown(
                    cooldown=app_commands.Cooldown(1, 3.0),
                    retry_after=3.0 - (current_time - last_used)
                )
            
            self.command_cooldowns[user_id] = current_time
        
        return True

    async def on_ready(self) -> None:
        """Run once when the bot has connected to Discord successfully."""
        logger.info("MutinyBot is online and ready to disrupt!")

        # Publish commands in guild scope for immediate availability and no duplication.
        if not self._app_commands_synced:
            if not self.guilds:
                logger.info("No guilds available for command sync")
            for guild in self.guilds:
                try:
                    self.tree.clear_commands(guild=guild)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info(
                        "Synced %d guild-scoped app commands to '%s' (%s)",
                        len(synced),
                        guild.name,
                        guild.id,
                    )
                except Exception:
                    logger.exception(
                        "Guild app command sync failed for '%s' (%s)",
                        guild.name,
                        guild.id,
                    )

            # Remove any legacy global commands to prevent duplicate listings.
            try:
                self.tree.clear_commands(guild=None)
                cleared = await self.tree.sync()
                logger.info("Cleared global app commands (remaining global: %d)", len(cleared))
            except Exception:
                logger.exception("Global app command cleanup failed")

            self._app_commands_synced = True

        # One-time palace initialization check
        palace_cog: Any = self.get_cog("MemoryPalaceCog")
        if palace_cog and hasattr(palace_cog, 'palace_path'):
            logger.info("MemPalace initialized at %s", palace_cog.palace_path)

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        """Handle slash command errors gracefully."""
        # Handle cooldown errors specifically
        if isinstance(error, app_commands.CommandOnCooldown):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Please wait {error.retry_after:.1f} seconds before using this command again.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Please wait {error.retry_after:.1f} seconds before using this command again.",
                    ephemeral=True
                )
            return

        # Log other errors and send generic message
        logger.error("Slash command error: %s", error, exc_info=True)

        # Send a friendly error message
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong, try again",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Something went wrong, try again",
                ephemeral=True
            )

    async def close(self) -> None:
        """Close long-lived services cleanly before bot shutdown."""
        try:
            if self.scheduler_manager.check_broadcast_queue.is_running():
                self.scheduler_manager.check_broadcast_queue.cancel()

            if self.scheduler_manager.scheduler.running:
                self.scheduler_manager.scheduler.shutdown(wait=False)

            await self.db_manager.close()
        finally:
            await super().close()