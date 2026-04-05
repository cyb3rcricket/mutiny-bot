"""Tools cog for MutinyBot tool management commands."""

from discord.ext import commands


class ToolsCog(commands.Cog):
    """Cog for tool management commands."""

    def __init__(self, bot):
        self.bot = bot

    # Placeholder for future tool management commands


async def setup(bot):
    """Add the cog to the bot."""
    await bot.add_cog(ToolsCog(bot))