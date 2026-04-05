"""Admin cog for MutinyBot slash commands."""

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import ALLOWED_MODELS


MAX_PERSONALITY_SNIPPET_CHARS = 300
MAX_SYSTEM_PROMPT_CHARS = 10_000


class AdminCog(commands.Cog):
    """Cog for administrative slash commands."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def _has_admin_permissions(interaction: discord.Interaction) -> bool:
        """Allow only users with Manage Guild or Administrator permissions."""
        if not interaction.guild:
            return False
        if not isinstance(interaction.user, discord.Member):
            return False

        perms = interaction.user.guild_permissions
        return bool(perms and (perms.manage_guild or perms.administrator))

    @app_commands.command(name="model", description="Switch MutinyBot's AI model")
    @app_commands.default_permissions(manage_guild=True)
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
        self, interaction: discord.Interaction, model_choice: app_commands.Choice[str]
    ) -> None:
        """Switch the active local model used for replies."""
        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server or Administrator permission to use this command.",
                ephemeral=True,
            )
            return

        await self.bot.db_manager.update_config("model", model_choice.value)
        await interaction.response.send_message(
            f"Mainframe re-routed. Now using: {model_choice.value}"
        )

    @app_commands.command(name="personality", description="Set MutinyBot system personality prompt")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(prompt_text="New system prompt for MutinyBot")
    async def personality_command(self, interaction: discord.Interaction, prompt_text: str) -> None:
        """Update system prompt steering via ChatOps."""
        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server or Administrator permission to use this command.",
                ephemeral=True,
            )
            return

        cleaned_prompt = prompt_text.strip()
        if not cleaned_prompt:
            await interaction.response.send_message(
                "Prompt cannot be empty.", ephemeral=True
            )
            return

        if len(cleaned_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            await interaction.response.send_message(
                f"Prompt is too long. Maximum length is {MAX_SYSTEM_PROMPT_CHARS} characters.",
                ephemeral=True,
            )
            return

        await self.bot.db_manager.update_config("system_prompt", cleaned_prompt)
        await interaction.response.send_message(
            "Personality updated successfully.",
        )

    @app_commands.command(name="status", description="Show current model, personality, and DB size")
    @app_commands.default_permissions(manage_guild=True)
    async def status_command(self, interaction: discord.Interaction) -> None:
        """Show bot runtime status in an embed (dashboard replacement)."""
        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server or Administrator permission to use this command.",
                ephemeral=True,
            )
            return

        active_model = await self.bot.db_manager.get_current_model()
        system_prompt = await self.bot.db_manager.get_system_prompt()
        db_size = self.bot.db_manager.format_db_size()

        prompt_snippet = (
            system_prompt
            if len(system_prompt) <= MAX_PERSONALITY_SNIPPET_CHARS
            else f"{system_prompt[:MAX_PERSONALITY_SNIPPET_CHARS]}..."
        )

        embed = discord.Embed(
            title="MutinyBot Status",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Active Model", value=active_model, inline=False)
        embed.add_field(name="Personality Snippet", value=prompt_snippet, inline=False)
        embed.add_field(name="Database Size", value=db_size, inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: Any) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(AdminCog(bot))