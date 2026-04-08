import logging
import os
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.mcp_server import tool_add_drawer, tool_status
from mempalace.searcher import search_memories


class MemoryPalaceCog(commands.Cog):
    """Cog for MemPalace-backed memory operations."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.palace_path = os.path.expanduser("~/.mutiny/palace")
        self.kg_db_path = os.path.join(self.palace_path, "knowledge_graph.sqlite3")
        self.search_memories = search_memories
        self.knowledge_graph_cls = KnowledgeGraph
        self.logger = logging.getLogger("mutiny_bot.memory_palace")
        self.graph = self.knowledge_graph_cls(self.kg_db_path)

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        """Store every non-bot message in MemPalace."""
        if message.author.bot:
            return

        try:
            # Prepare metadata
            metadata = {
                "author": message.author.name,
                "channel": message.channel.name,
                "timestamp": message.created_at.isoformat(),
                "guild": message.guild.name,
            }

            # Store the conversation chunk - tool_add_drawer automatically creates wing/room if needed
            # Keep custom palace target working on MemPalace variants that read path from env.
            import os
            os.environ["MEMPALACE_PALACE_PATH"] = self.palace_path

            tool_add_drawer(
                wing=message.guild.name,
                room=message.channel.name,
                content=message.content,
                source_file=str(metadata.get("table", "")),
                added_by="bot",
            )
        except Exception as e:
            self.logger.error(f"Error storing message in MemPalace: {e}")

    @app_commands.command(name="palace_status", description="Show MemPalace statistics")
    async def palace_status_command(self, interaction: discord.Interaction) -> None:
        """Display palace stats including wing count, total memories, and KG triples."""
        try:
            self.logger.info("Executing palace_status command")
            # Set the palace path for tool_status
            import os
            os.environ["MEMPALACE_PALACE_PATH"] = self.palace_path
            self.logger.info(f"Set MEMPALACE_PALACE_PATH to {self.palace_path}")
            
            status = tool_status()
            self.logger.info(f"tool_status returned: {status}")
            
            if "error" in status:
                self.logger.error(f"tool_status error: {status['error']}")
                await interaction.response.send_message(f"Error: {status['error']}", ephemeral=True)
                return
                
            embed = discord.Embed(
                title="MemPalace Status",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Total Drawers", value=status.get("total_drawers", 0), inline=True)
            embed.add_field(name="Wings", value=len(status.get("wings", {})), inline=True)
            embed.add_field(name="Palace Path", value=status.get("palace_path", "Unknown"), inline=False)
            
            self.logger.info("Sending palace status embed")
            await interaction.response.send_message(embed=embed)
            self.logger.info("Palace status command completed successfully")
        except Exception as e:
            self.logger.error(f"Error getting palace status: {e}", exc_info=True)
            await interaction.response.send_message("Error retrieving palace status.", ephemeral=True)

    @app_commands.command(name="wake_up", description="Get wake up context from MemPalace")
    async def wake_up_command(self, interaction: discord.Interaction) -> None:
        """Reply with wake up context injected into a short message."""
        try:
            context = self.get_memory_context("wake up")
            message = f"Waking up with context: {context}"
            await interaction.response.send_message(message)
        except Exception as e:
            self.logger.error(f"Error getting wake up context: {e}")
            await interaction.response.send_message("Error retrieving wake up context.", ephemeral=True)

    def get_memory_context(self, user_input: str, guild: Optional[str] = None) -> str:
        """Get formatted memory context for LLM prompts (~300-500 tokens max)."""
        try:
            # Build wake-up context using tool_status and search_memories
            status = tool_status()
            wake_up = f"Palace has {status.get('total_drawers', 0)} drawers across {len(status.get('wings', {}))} wings."

            # Search relevant memories
            search_kwargs = {
                "palace_path": self.palace_path,
                "query": user_input,
            }
            if guild:
                search_kwargs["wing"] = guild

            memories_result = self.search_memories(**search_kwargs)
            memories = memories_result.get("results", [])

            # Format memories
            memory_texts = []
            if isinstance(memories, list):
                for mem in memories[:5]:  # Limit to top 5
                    if isinstance(mem, dict):
                        text = mem.get("text", "").strip()
                    else:
                        text = str(mem).strip()
                    if text:
                        memory_texts.append(text[:200])  # Truncate each

            formatted_memories = "\n".join(f"- {text}" for text in memory_texts)

            # Combine and limit total length (~1500 chars for ~300-500 tokens)
            context = f"Wake up context: {wake_up}\n\nRelevant memories:\n{formatted_memories}"
            if len(context) > 1500:
                context = context[:1500] + "..."

            return context
        except Exception as e:
            self.logger.error(f"Error getting memory context: {e}")
            return "Memory context unavailable."


async def setup(bot: Any) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(MemoryPalaceCog(bot))
