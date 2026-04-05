"""Monitoring cog: slash commands for jobs, history, and status."""

import discord
from discord import app_commands
from discord.ext import commands

from config import MONITORING_CHANNEL_ID


class Monitoring(commands.Cog):
    """Cog exposing monitoring slash commands."""

    def __init__(self, bot):
        self.bot = bot

    def _check_channel(self, interaction: discord.Interaction) -> bool:
        if MONITORING_CHANNEL_ID:
            if interaction.channel is None or interaction.channel.id != MONITORING_CHANNEL_ID:
                return False
        return True

    @app_commands.command(name="jobs", description="List active scheduled jobs")
    async def jobs(self, interaction: discord.Interaction):
        if not self._check_channel(interaction):
            await interaction.response.send_message("This command can only be used in the monitoring channel.", ephemeral=True)
            return

        await interaction.response.defer()
        jobs = await self.bot.scheduler_manager.get_active_jobs()

        if not jobs:
            embed = discord.Embed(title="Active Jobs 🔔", description="No scheduled jobs found.", color=0x2ecc71)
        else:
            embed = discord.Embed(title="Active Jobs 🔔", color=0x2ecc71)
            for job in jobs:
                # job may be dict-like or object; be permissive
                jid = job.get("id") if isinstance(job, dict) else getattr(job, "id", str(job))
                jname = job.get("name") if isinstance(job, dict) else getattr(job, "name", str(job))
                jnext = job.get("next_run_time") if isinstance(job, dict) else getattr(job, "next_run_time", None)
                desc = f"Next run: {jnext}" if jnext else "Next run: N/A"
                embed.add_field(name=f"{jid} — {jname}", value=desc, inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="history", description="Show recent chat history (admin) 📜")
    async def history(self, interaction: discord.Interaction):
        if not self._check_channel(interaction):
            await interaction.response.send_message("This command can only be used in the monitoring channel.", ephemeral=True)
            return

        await interaction.response.defer()
        history = await self.bot.db_manager.get_chat_history()

        embed = discord.Embed(title="Chat History 📜", color=0x3498db)
        if not history:
            embed.description = "No history available."
            await interaction.followup.send(embed=embed)
            return

        # Show up to 10 recent items
        for i, item in enumerate(history[:10], 1):
            # item may be dict or tuple (role, content)
            if isinstance(item, dict):
                role = item.get("role", "?")
                content = item.get("content", "")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                role, content = item[0], item[1]
            else:
                role = "assistant"
                content = str(item)

            short = (content[:200] + "…") if len(content) > 200 else content
            embed.add_field(name=f"{i}. {role}", value=short or "(empty)", inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="botstatus", description="Show system status and job counts ⚙️ (botstatus)")
    async def botstatus(self, interaction: discord.Interaction):
        if not self._check_channel(interaction):
            await interaction.response.send_message("This command can only be used in the monitoring channel.", ephemeral=True)
            return

        await interaction.response.defer()
        jobs = await self.bot.scheduler_manager.get_active_jobs()
        history = await self.bot.db_manager.get_chat_history(limit=50) if hasattr(self.bot.db_manager, "get_chat_history") else None

        embed = discord.Embed(title="System Status ⚙️", color=0xf1c40f)
        embed.add_field(name="Scheduled Jobs", value=str(len(jobs) if jobs is not None else 0), inline=True)
        if history is not None:
            embed.add_field(name="Recent History Items", value=str(len(history)), inline=True)

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Monitoring(bot))
