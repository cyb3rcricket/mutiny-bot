import logging
import os
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from mempalace.knowledge_graph import KnowledgeGraph
    from mempalace.mcp_server import tool_add_drawer, tool_status
    from mempalace.searcher import search_memories
except Exception as mempalace_import_error:
    KnowledgeGraph = None
    tool_add_drawer = None
    tool_status = None
    search_memories = None
    _MEMPALACE_IMPORT_ERROR = mempalace_import_error
else:
    _MEMPALACE_IMPORT_ERROR = None


class MemoryPalaceCog(commands.Cog):
    """Cog for MemPalace-backed memory operations."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.palace_path = os.path.expanduser("~/.mutiny/palace")
        self.kg_db_path = os.path.join(self.palace_path, "knowledge_graph.sqlite3")
        self.search_memories = search_memories
        self.knowledge_graph_cls = KnowledgeGraph
        self.logger = logging.getLogger("mutiny_bot.memory_palace")
        self.mempalace_available = bool(
            self.search_memories and self.knowledge_graph_cls and tool_add_drawer and tool_status
        )
        self.graph = self.knowledge_graph_cls(self.kg_db_path) if self.knowledge_graph_cls else None
        if not self.mempalace_available:
            self.logger.warning("MemPalace is unavailable: %s", _MEMPALACE_IMPORT_ERROR)

    @staticmethod
    def _has_admin_permissions(interaction: discord.Interaction) -> bool:
        """Allow only users with Manage Guild or Administrator permissions."""
        if not interaction.guild:
            return False
        if not isinstance(interaction.user, discord.Member):
            return False

        perms = interaction.user.guild_permissions
        return bool(perms and (perms.manage_guild or perms.administrator))

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        """Store every non-bot guild message in MemPalace."""
        if message.author.bot:
            return
        if not message.guild:
            return
        if not self.mempalace_available:
            return
        assert tool_add_drawer is not None

        try:
            guild_name = message.guild.name
            channel_name = getattr(message.channel, "name", str(message.channel.id))

            # Prepare metadata
            metadata = {
                "author": message.author.name,
                "channel": channel_name,
                "timestamp": message.created_at.isoformat(),
                "guild": guild_name,
            }

            # Store the conversation chunk - tool_add_drawer automatically creates wing/room if needed
            # Keep custom palace target working on MemPalace variants that read path from env.
            import os
            os.environ["MEMPALACE_PALACE_PATH"] = self.palace_path

            tool_add_drawer(
                wing=guild_name,
                room=channel_name,
                content=message.content,
                source_file=str(metadata.get("table", "")),
                added_by="bot",
            )
        except Exception as e:
            self.logger.error(f"Error storing message in MemPalace: {e}")

    @app_commands.command(name="palace_status", description="Show MemPalace statistics")
    async def palace_status_command(self, interaction: discord.Interaction) -> None:
        """Display palace stats including wing count, total memories, and KG triples."""
        if not MemoryPalaceCog._has_admin_permissions(interaction):
            await interaction.response.send_message("You need Manage Server permission to view palace status.", ephemeral=True)
            return

        if not self.mempalace_available:
            await interaction.response.send_message("MemPalace is unavailable. Check dependency compatibility.", ephemeral=True)
            return
        assert tool_status is not None

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
        if not MemoryPalaceCog._has_admin_permissions(interaction):
            await interaction.response.send_message("You need Manage Server permission to wake up the palace.", ephemeral=True)
            return

        if not self.mempalace_available:
            await interaction.response.send_message("MemPalace is unavailable. Check dependency compatibility.", ephemeral=True)
            return

        try:
            context = self.get_memory_context("wake up")
            message = f"Waking up with context: {context}"
            await interaction.response.send_message(message)
        except Exception as e:
            self.logger.error(f"Error getting wake up context: {e}")
            await interaction.response.send_message("Error retrieving wake up context.", ephemeral=True)

    def get_memory_context(self, user_input: str, guild: Optional[str] = None) -> str:
        """Get formatted memory context for LLM prompts (~300-500 tokens max)."""
        if not self.mempalace_available:
            return "Memory context unavailable."
        assert tool_status is not None
        assert self.search_memories is not None

        try:
            # Build wake-up context using tool_status and search_memories
            status = tool_status()
            wake_up = f"Palace has {status.get('total_drawers', 0)} drawers across {len(status.get('wings', {}))} wings."

            # Search relevant memories
            search_kwargs: dict[str, Any] = {
                "palace_path": self.palace_path,
            }
            if guild:
                search_kwargs["wing"] = guild

            memories_result = self.search_memories(user_input, **search_kwargs)
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

    def is_article_posted(self, url: str, dedup_room: str, palace_path: str) -> bool:
        """Check if article URL is already posted in MemPalace."""
        if not self.mempalace_available:
            return False
        assert self.search_memories is not None

        try:
            os.environ["MEMPALACE_PALACE_PATH"] = palace_path
            results = self.search_memories(url, palace_path=palace_path, wing="news-monitor", room=dedup_room)
            return bool(results)
        except Exception as e:
            self.logger.error(f"Error checking if article posted: {e}")
            return False

    def mark_article_posted(self, article_dict: dict, dedup_room: str, palace_path: str) -> None:
        """Mark article as posted in MemPalace using its link."""
        if not self.mempalace_available:
            return
        assert tool_add_drawer is not None

        try:
            os.environ["MEMPALACE_PALACE_PATH"] = palace_path
            url = article_dict.get("link", "")
            if not url:
                self.logger.warning("No link in article_dict to mark as posted")
                return
            tool_add_drawer(
                wing="news-monitor",
                room=dedup_room,
                content=url,
                added_by="news_monitor"
            )
        except Exception as e:
            self.logger.error(f"Error marking article posted: {e}")


async def setup(bot: Any) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(MemoryPalaceCog(bot))
