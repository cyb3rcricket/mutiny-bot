"""Tools cog for MutinyBot tool management commands."""

from typing import Any

from discord.ext import commands


class ToolsCog(commands.Cog):
    """Cog for tool management commands."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # Placeholder for future tool management commands


async def setup(bot: Any) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(ToolsCog(bot))