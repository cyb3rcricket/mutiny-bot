"""Monitoring cog: slash commands for jobs, history, and status."""

import asyncio
import hashlib
import importlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import re
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import discord
from discord import app_commands
from discord.ext import commands
from discord import ui
from typing import cast, Any, Optional

from config import MONITORING_CHANNEL_ID, LOG_PATHS, ALLOWED_MODELS, BOT_OWNER_ID
from llm.models import get_installed_models
from tools.registry import AVAILABLE_TOOLS, TOOL_SCHEMAS
from tools.news_monitor import execute_news_monitor, get_fresh_news
from tools.scheduler_manager import execute_and_broadcast
from scheduler.scheduler_manager import resume_job


def parse_schedule_time(time_str: str):
    """Parse time string and return trigger. Supports 'daily at HH:MM', 'hourly', 'every N minutes'."""
    time_str = time_str.lower().strip()
    
    if time_str == "hourly":
        return IntervalTrigger(hours=1)
    
    if time_str.startswith("every ") and time_str.endswith(" minutes"):
        try:
            minutes = int(time_str.split()[1])
            return IntervalTrigger(minutes=minutes)
        except:
            pass
    
    if time_str.startswith("daily at "):
        try:
            hh_mm = time_str.split(" at ")[1]
            hour, minute = map(int, hh_mm.split(":"))
            return CronTrigger(hour=hour, minute=minute)
        except:
            pass
    
    if time_str.startswith("weekly on ") and " at " in time_str:
        try:
            parts = time_str.split(" at ")
            day_part = parts[0].split(" on ")[1]
            hh_mm = parts[1]
            hour, minute = map(int, hh_mm.split(":"))
            # Map day names to numbers
            days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
            day = days.get(day_part.lower())
            if day is not None:
                return CronTrigger(day_of_week=day, hour=hour, minute=minute)
        except:
            pass
    
    raise ValueError(f"Unsupported time format: {time_str}. Try 'daily at 02:00', 'hourly', 'every 30 minutes', or 'weekly on monday at 02:00'")


async def switch_model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    models = get_installed_models()
    return [app_commands.Choice(name=model, value=model) for model in models if current.lower() in model.lower()]


def collect_local_system_snapshot() -> dict[str, Any]:
    """Build a small local machine status snapshot using stdlib only."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = platform.node() or "unknown-host"
    python_version = platform.python_version()
    platform_name = f"{platform.system()} {platform.release()}"

    load_text = "N/A"
    load_values = None
    if hasattr(os, "getloadavg"):
        try:
            load1, load5, load15 = os.getloadavg()
            load_text = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
            load_values = (load1, load5, load15)
        except OSError:
            load_text = "N/A"

    disk_free = None
    disk_total = None
    disk_text = "N/A"
    try:
        disk = shutil.disk_usage("/")
        free_gb = disk.free / (1024 ** 3)
        total_gb = disk.total / (1024 ** 3)
        disk_text = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total"
        disk_free = free_gb
        disk_total = total_gb
    except Exception:
        disk_text = "N/A"

    return {
        "time": now,
        "hostname": hostname,
        "platform": platform_name,
        "python": python_version,
        "cpu_load": load_text,
        "cpu_values": load_values,
        "disk": disk_text,
        "disk_free": disk_free,
        "disk_total": disk_total,
    }


def generate_daily_insight(snapshot: dict[str, Any]) -> str:
    """Generate a fun daily insight from system snapshot."""
    insights = []

    if snapshot["cpu_values"]:
        load1 = snapshot["cpu_values"][0]
        if load1 > 2:
            insights.append("🚀 Your CPU is working overtime today!")
        elif load1 > 1:
            insights.append("⚡ Your system is quite active right now.")
        elif load1 < 0.5:
            insights.append("😴 Your CPU is taking it easy today.")

    if snapshot["disk_free"] is not None:
        free_percent = (snapshot["disk_free"] / snapshot["disk_total"]) * 100
        if free_percent < 10:
            insights.append("💾 Your disk space is running low!")
        elif free_percent > 80:
            insights.append("🗂️ You have plenty of disk space available.")

    if snapshot["hostname"] != "unknown-host":
        insights.append(f"🏠 Running on {snapshot['hostname']} today.")

    # Add some general fun facts
    insights.extend([
        "🤖 Your bot has been running smoothly!",
        "📊 System monitoring is active and healthy.",
        "🔧 All systems are nominal.",
    ])

    # Pick one randomly or first
    import random
    return random.choice(insights) if insights else "Everything looks good today!"


def get_docker_containers() -> list[dict[str, str]]:
    """Get list of running Docker containers with stats."""
    containers = []
    try:
        # Get container list
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return []

        lines = result.stdout.strip().split('\n')
        if not lines or not lines[0].strip():
            return []

        # Get stats
        stats_result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}"],
            capture_output=True,
            text=True,
            timeout=10
        )
        stats = {}
        if stats_result.returncode == 0:
            for line in stats_result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 3:
                        stats[parts[0]] = {"cpu": parts[1], "mem": parts[2]}

        for line in lines:
            if line.strip():
                parts = line.split('\t')
                if len(parts) >= 1:
                    name = parts[0]
                    image = parts[1] if len(parts) > 1 else "N/A"
                    status = parts[2] if len(parts) > 2 else "N/A"
                    ports = parts[3] if len(parts) > 3 else "N/A"
                    container_stats = stats.get(name, {"cpu": "N/A", "mem": "N/A"})
                    containers.append({
                        "name": name,
                        "image": image,
                        "status": status,
                        "ports": ports,
                        "cpu": container_stats["cpu"],
                        "mem": container_stats["mem"]
                    })
    except Exception:
        pass
    return containers


def ping_host(host: str) -> dict[str, str]:
    try:
        # Use ping with 4 packets, timeout 5 seconds
        result = subprocess.run(
            ["ping", "-c", "4", "-W", "5", host],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout + result.stderr

        if result.returncode == 0:
            # Parse output
            lines = output.split('\n')
            packet_loss = "0%"
            latency = "N/A"
            for line in lines:
                if "packet loss" in line:
                    # Extract percentage
                    parts = line.split()
                    for part in parts:
                        if "%" in part:
                            packet_loss = part
                            break
                elif "min/avg/max" in line or "round-trip" in line:
                    # Extract avg latency
                    if "min/avg/max" in line:
                        parts = line.split()
                        for part in parts:
                            if "/" in part:
                                latencies = part.split("/")
                                if len(latencies) >= 2:
                                    latency = f"{latencies[1]} ms"
                                break
                    elif "round-trip" in line:
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part == "min/avg/max/stddev" and i + 1 < len(parts):
                                latencies = parts[i+1].split("/")
                                if len(latencies) >= 2:
                                    latency = f"{latencies[1]} ms"
                                break
            return {"status": "success", "latency": latency, "packet_loss": packet_loss}
        else:
            return {"status": "error", "message": f"Ping failed: {output.strip()}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Ping timed out"}
    except Exception as e:
        return {"status": "error", "message": f"Error: {str(e)}"}


def get_system_info() -> dict[str, str]:
    info = {}

    # CPU Load
    load_text = "N/A"
    if hasattr(os, "getloadavg"):
        try:
            load1, load5, load15 = os.getloadavg()
            load_text = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
        except OSError:
            pass
    info["cpu"] = f"Load (1m, 5m, 15m): {load_text}"

    # RAM
    ram_text = "N/A"
    try:
        import psutil
        mem = psutil.virtual_memory()
        ram_text = f"{mem.used / (1024**3):.1f} GB used / {mem.total / (1024**3):.1f} GB total ({mem.percent:.1f}%)"
    except ImportError:
        # Fallback to reading /proc/meminfo on Linux
        if platform.system() == "Linux":
            try:
                with open("/proc/meminfo", "r") as f:
                    lines = f.readlines()
                    total = next((int(line.split()[1]) for line in lines if line.startswith("MemTotal")), 0) * 1024
                    available = next((int(line.split()[1]) for line in lines if line.startswith("MemAvailable")), 0) * 1024
                    used = total - available
                    percent = (used / total) * 100 if total > 0 else 0
                    ram_text = f"{used / (1024**3):.1f} GB used / {total / (1024**3):.1f} GB total ({percent:.1f}%)"
            except Exception:
                pass
    info["ram"] = ram_text

    # Disk
    disk_text = "N/A"
    try:
        disk = shutil.disk_usage("/")
        free_gb = disk.free / (1024 ** 3)
        total_gb = disk.total / (1024 ** 3)
        disk_text = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total"
    except Exception:
        pass
    info["disk"] = disk_text

    # Top processes
    processes_text = "N/A"
    try:
        import psutil
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                processes.append((proc.info['name'] or 'unknown', proc.info['cpu_percent'] or 0, proc.info['memory_percent'] or 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Sort by CPU + memory
        processes.sort(key=lambda x: x[1] + x[2], reverse=True)
        top = processes[:5]
        processes_text = "\n".join([f"{name}: CPU {cpu:.1f}%, RAM {mem:.1f}%" for name, cpu, mem in top])
    except ImportError:
        processes_text = "psutil not available"
    info["processes"] = processes_text

    return info


class DockerRestartView(discord.ui.View):
    def __init__(self, containers, bot):
        super().__init__()
        self.bot = bot
        for container in containers:
            name = container["name"]
            if not self._is_safe_container_name(name):
                continue
            button = discord.ui.Button(label=f"Restart {name}", style=discord.ButtonStyle.danger, custom_id=f"restart_{name}")
            button.callback = self._build_restart_callback(name)
            self.add_item(button)

    @staticmethod
    def _is_safe_container_name(name: str) -> bool:
        # Docker names should be simple identifiers; reject anything shell-like.
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name or ""))

    def _build_restart_callback(self, container_name: str):
        async def _callback(interaction: discord.Interaction):
            await self.restart_container(interaction, container_name)

        return _callback

    async def restart_container(self, interaction: discord.Interaction, container_name: str):
        if not MonitoringCog._has_admin_permissions(interaction):
            await interaction.response.send_message("You need Manage Server permission to restart containers.", ephemeral=True)
            return

        if not container_name or not self._is_safe_container_name(container_name):
            await interaction.response.send_message("Invalid or unauthorized container name.", ephemeral=True)
            return

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: subprocess.run(["docker", "restart", container_name], capture_output=True, text=True, timeout=30)
            )
            if result.returncode == 0:
                await interaction.response.send_message(f"✅ Container '{container_name}' restarted successfully.", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Failed to restart '{container_name}': {result.stderr.strip()}", ephemeral=True)
        except subprocess.TimeoutExpired:
            await interaction.response.send_message(f"❌ Restart timed out for '{container_name}'.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error restarting '{container_name}': {str(e)}", ephemeral=True)


class RestartConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def confirm_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not MonitoringCog._has_admin_permissions(interaction):
            await interaction.response.send_message("You need Manage Server permission to restart the bot.", ephemeral=True)
            return

        await interaction.response.send_message("🔄 Restarting bot...", ephemeral=True)
        # Give time for the message to send
        await asyncio.sleep(1)
        # Restart the bot
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Restart canceled.", ephemeral=True)


class JobCancelView(discord.ui.View):
    def __init__(self, jobs, bot):
        super().__init__()
        self.bot = bot
        for job in jobs:
            job_id = str(job.get("id", "")).strip()
            if not job_id:
                continue
            job_name = job.get("name", "Unknown")
            button = discord.ui.Button(label=f"Cancel {job_name}", style=discord.ButtonStyle.red)
            button.callback = self._build_cancel_callback(job_id)
            self.add_item(button)

    def _build_cancel_callback(self, job_id: str):
        async def _callback(interaction: discord.Interaction):
            await self.cancel_job(interaction, job_id)

        return _callback

    async def cancel_job(self, interaction: discord.Interaction, job_id: str):
        if not MonitoringCog._has_admin_permissions(interaction):
            await interaction.response.send_message("You need Manage Server permission to cancel jobs.", ephemeral=True)
            return

        try:
            self.bot.scheduler_manager.scheduler.remove_job(job_id)
            await interaction.response.send_message(f"✅ Job '{job_id}' canceled successfully.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to cancel job '{job_id}': {str(e)}", ephemeral=True)


class MonitoringCog(commands.Cog):
    """Cog exposing monitoring slash commands."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.palace_path = os.path.expanduser("~/.mutiny/palace")

    @staticmethod
    def _has_admin_permissions(interaction: discord.Interaction) -> bool:
        """Allow only users with Manage Guild or Administrator permissions."""
        if not interaction.guild:
            return False
        if not isinstance(interaction.user, discord.Member):
            return False

        perms = interaction.user.guild_permissions
        return bool(perms and (perms.manage_guild or perms.administrator))

    @staticmethod
    def _is_bot_owner(interaction: discord.Interaction) -> bool:
        """Allow only the bot owner."""
        return interaction.user and interaction.user.id == BOT_OWNER_ID

    def _check_channel(self, interaction: discord.Interaction) -> bool:
        if MONITORING_CHANNEL_ID and interaction.channel and interaction.channel.id != MONITORING_CHANNEL_ID:
            return False
        return True

    async def _reject_unavailable(self, interaction: discord.Interaction) -> None:
        """Return a generic unavailable response without leaking policy details."""
        await interaction.response.send_message(
            "This command is unavailable in the current context.",
            ephemeral=True,
        )

    async def _reset_user_history(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)
        await self.bot.db_manager.clear_chat_history(user_id)
        await interaction.response.send_message("Chat history has been wiped and started fresh.")

    @staticmethod
    def _memory_text(item: Any) -> str:
        """Normalize MemPalace search results to displayable text."""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            value = item.get("content") or item.get("text") or item.get("memory")
            if value:
                return str(value)
            return str(item)
        return str(item)

    @staticmethod
    def _safe_guild_name(interaction: discord.Interaction) -> str:
        if interaction.guild and interaction.guild.name:
            return interaction.guild.name
        return "DirectMessages"

    @staticmethod
    def _safe_room_name(interaction: discord.Interaction) -> str:
        if interaction.channel:
            channel_name = getattr(interaction.channel, "name", None)
            if channel_name:
                return str(channel_name)
        return "direct-message"

    @staticmethod
    def _get_mempalace_search() -> Any:
        module = importlib.import_module("mempalace.searcher")
        return getattr(module, "search_memories")

    @staticmethod
    def _get_mempalace_add_drawer() -> Any:
        import inspect
        import os
        module = importlib.import_module("mempalace.mcp_server")
        tool_add_drawer = getattr(module, "tool_add_drawer")
        _ADD_DRAWER_PARAMS = set(inspect.signature(tool_add_drawer).parameters)

        def _add_drawer(*, palace_path: str, wing: str, room: str, content: str, metadata: dict[str, Any]) -> None:
            # Keep custom palace target working on MemPalace variants that read path from env.
            os.environ["MEMPALACE_PALACE_PATH"] = palace_path

            if "palace_path" in _ADD_DRAWER_PARAMS:
                kwargs: dict[str, Any] = {
                    "palace_path": palace_path,
                    "wing": wing,
                    "room": room,
                    "content": content,
                }
                if "metadata" in _ADD_DRAWER_PARAMS:
                    kwargs["metadata"] = metadata
                result = tool_add_drawer(**kwargs)
            else:
                result = tool_add_drawer(
                    wing=wing,
                    room=room,
                    content=content,
                    source_file=str(metadata.get("table", "")),
                    added_by="bot",
                )

            if isinstance(result, dict) and result.get("success") is False:
                if result.get("reason") == "duplicate":
                    return
                raise RuntimeError(result.get("error") or "Failed to add drawer")

        return _add_drawer

    def _search_memories(self, query: str, wing: str, room: Optional[str] = None) -> list[Any]:
        search_fn = self._get_mempalace_search()
        kwargs: dict[str, Any] = {
            "palace_path": self.palace_path,
            "wing": wing,
        }
        if room:
            kwargs["room"] = room
        results = search_fn(query, **kwargs)
        if isinstance(results, list):
            return results
        return []

    @app_commands.command(name="botstatus", description="Show bot status ⚙️")
    @app_commands.default_permissions(manage_guild=True)
    async def botstatus(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view bot status.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Get data from managers
        jobs = await self.bot.scheduler_manager.get_active_jobs()
        history = await self.bot.db_manager.get_chat_history(limit=50)

        # Calculate uptime (simple approximation)
        import time
        uptime_seconds = int(time.time() - self.bot.start_time) if hasattr(self.bot, 'start_time') else 0
        uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        embed = discord.Embed(title="🤖 Bot Status", color=0x3498db)
        embed.add_field(name="⏱️ Uptime", value=uptime_str, inline=True)
        embed.add_field(name="📋 Active Jobs", value=str(len(jobs)), inline=True)
        embed.add_field(name="💬 History Items", value=str(len(history)), inline=True)
        embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="jobs", description="List active scheduled jobs 🔔")
    @app_commands.default_permissions(manage_guild=True)
    async def jobs(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view jobs.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        jobs = await self.bot.scheduler_manager.get_active_jobs()

        embed = discord.Embed(title="🔔 Active Jobs", color=0x3498db)
        if not jobs:
            embed.description = "No scheduled jobs found."
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.followup.send(embed=embed)
        else:
            for job in jobs[:10]:  # Limit to 10
                name = job.get("name", "Unknown")
                next_run = job.get("next_run_time", "N/A")
                if next_run and hasattr(next_run, 'strftime'):
                    next_run = next_run.strftime("%Y-%m-%d %H:%M")
                embed.add_field(name=f"📅 {name}", value=f"Next: {next_run}", inline=False)

            embed.set_footer(text="Mutiny Bot • Local Only")
            view = JobCancelView(jobs[:10], self.bot)
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="history", description="Show recent chat history 📜")
    @app_commands.default_permissions(manage_guild=True)
    async def history(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view history.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        history = await self.bot.db_manager.get_chat_history(limit=10)

        embed = discord.Embed(title="📜 Recent Chat History", color=0x3498db)
        if not history:
            embed.description = "No history available."
        else:
            for i, item in enumerate(history, 1):
                role = item.get("role", "unknown")
                content = item.get("content", "")[:100] + "..." if len(item.get("content", "")) > 100 else item.get("content", "")
                emoji = "👤" if role == "user" else "🤖"
                embed.add_field(name=f"{i}. {emoji} {role}", value=content, inline=False)

        embed.set_footer(text="Mutiny Bot • Local Only")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="clear-history", description="Clear chat history for current user 🗑️")
    @app_commands.default_permissions(manage_guild=True)
    async def clear_history(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to clear history.",
                ephemeral=True,
            )
            return

        await self._reset_user_history(interaction)

    @app_commands.command(name="reset", description="Reset chat history for the current user ♻️")
    @app_commands.default_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to reset history.",
                ephemeral=True,
            )
            return

        await self._reset_user_history(interaction)

    @app_commands.command(name="quick-run", description="Run a tool immediately 🛠️")
    @app_commands.default_permissions(manage_guild=True)
    async def quick_run(self, interaction: discord.Interaction, tool_name: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to run tools.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can run tools directly.",
                ephemeral=True,
            )
            return

        tool_func = AVAILABLE_TOOLS.get(tool_name)
        if not tool_func:
            embed = discord.Embed(
                title="❌ Tool Not Found",
                description=f"No tool named '{tool_name}' found in AVAILABLE_TOOLS.",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.response.send_message(embed=embed)
            return

        await interaction.response.defer()

        try:
            result = tool_func()
            if asyncio.iscoroutine(result):
                result = await result
            result_str = str(result)[:2000]  # Limit to 2000 chars for embed
            embed = discord.Embed(
                title=f"✅ Tool Result: {tool_name}",
                description=result_str,
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            error_str = str(e)[:2000]
            embed = discord.Embed(
                title=f"❌ Tool Error: {tool_name}",
                description=error_str,
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="system", description="Show system information 💻")
    @app_commands.default_permissions(manage_guild=True)
    async def system(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view system info.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can view system information.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        info = get_system_info()

        embed = discord.Embed(title="💻 System Information", color=0x3498db)
        embed.add_field(name="🖥️ CPU", value=info["cpu"], inline=False)
        embed.add_field(name="🧠 RAM", value=info["ram"], inline=False)
        embed.add_field(name="💾 Disk", value=info["disk"], inline=False)
        embed.add_field(name="📊 Top Processes", value=info["processes"], inline=False)
        embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="remember", description="Save a fact permanently 📝")
    @app_commands.default_permissions(manage_guild=True)
    async def remember(self, interaction: discord.Interaction, fact: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to save facts.",
                ephemeral=True,
            )
            return

        wing = self._safe_guild_name(interaction)
        room = "remembered-facts"
        metadata = {
            "author": interaction.user.name if interaction.user else "unknown",
            "channel": self._safe_room_name(interaction),
            "timestamp": datetime.utcnow().isoformat(),
            "guild": wing,
            "type": "fact",
        }

        try:
            add_drawer = self._get_mempalace_add_drawer()
            add_drawer(
                palace_path=self.palace_path,
                wing=wing,
                room=room,
                content=fact,
                metadata=metadata,
            )
            await interaction.response.send_message(f"✅ Fact saved: {fact}")
        except Exception:
            await interaction.response.send_message(
                "❌ Failed to save fact.",
                ephemeral=True,
            )

    @app_commands.command(name="recall", description="Show all saved facts 📚")
    @app_commands.default_permissions(manage_guild=True)
    async def recall(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view facts.",
                ephemeral=True,
            )
            return

        wing = self._safe_guild_name(interaction)
        facts: list[str] = []
        try:
            fact_items = self._search_memories(query="", wing=wing, room="remembered-facts")
            facts = [self._memory_text(item) for item in fact_items if self._memory_text(item).strip()]
        except Exception:
            facts = []

        embed = discord.Embed(title="📚 Saved Facts", color=0x3498db)
        if not facts:
            embed.description = "No facts saved yet."
        else:
            for i, fact in enumerate(facts, 1):
                embed.add_field(name=f"{i}.", value=fact, inline=False)

        embed.set_footer(text="Mutiny Bot • Local Only")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Ping a host and show latency 📡")
    @app_commands.default_permissions(manage_guild=True)
    async def ping(self, interaction: discord.Interaction, host: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to ping hosts.",
                ephemeral=True,
            )
            return

        # Validate host to prevent command injection
        import re
        if not re.match(r'^[a-zA-Z0-9.-]+$', host) or '..' in host or host.startswith('-'):
            await interaction.response.send_message(
                "Invalid host format. Only alphanumeric characters, dots, and dashes are allowed.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can ping hosts.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        result = await asyncio.get_event_loop().run_in_executor(None, ping_host, host)

        if result["status"] == "success":
            embed = discord.Embed(
                title=f"📡 Ping: {host}",
                color=0x3498db
            )
            embed.add_field(name="Latency", value=result["latency"], inline=True)
            embed.add_field(name="Packet Loss", value=result["packet_loss"], inline=True)
            embed.set_footer(text="Mutiny Bot • Local Only")
        else:
            embed = discord.Embed(
                title=f"❌ Ping Failed: {host}",
                description=result["message"],
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="generate-script", description="Generate a bash script using AI 🤖")
    @app_commands.default_permissions(manage_guild=True)
    async def generate_script(self, interaction: discord.Interaction, task: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to generate scripts.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can generate scripts.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        model = await self.bot.db_manager.get_current_model()
        messages = [{"role": "user", "content": f"Generate a clean, well-commented bash script for: {task}. Make it safe and efficient."}]
        
        try:
            script = await self.bot.llm_handler.generate_response(model, messages)
            embed = discord.Embed(
                title=f"🤖 Generated Script: {task}",
                description=f"```bash\n{script}\n```",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Script Generation Failed",
                description=f"Error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="docker", description="List running Docker containers 🐳")
    @app_commands.default_permissions(manage_guild=True)
    async def docker(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view Docker containers.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can view Docker containers.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        containers = await asyncio.get_event_loop().run_in_executor(None, get_docker_containers)

        embed = discord.Embed(title="🐳 Running Docker Containers", color=0x3498db)
        if not containers:
            embed.description = "No running containers found."
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.followup.send(embed=embed)
        else:
            for container in containers[:10]:  # Limit to 10
                name = container["name"]
                image = container["image"]
                status = container["status"]
                cpu = container["cpu"]
                mem = container["mem"]
                embed.add_field(
                    name=f"📦 {name} ({image})",
                    value=f"Status: {status}\nCPU: {cpu}\nMemory: {mem}",
                    inline=False
                )

            embed.set_footer(text="Mutiny Bot • Local Only")
            view = DockerRestartView(containers[:10], self.bot)
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="logs", description="Show recent logs for a service 📄")
    @app_commands.default_permissions(manage_guild=True)
    async def logs(self, interaction: discord.Interaction, service: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view logs.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can view system logs.",
                ephemeral=True,
            )
            return

        log_path = LOG_PATHS.get(service.lower())
        if not log_path:
            embed = discord.Embed(
                title="❌ Service Not Configured",
                description=f"No log path configured for service '{service}'. Available services: {', '.join(LOG_PATHS.keys())}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.response.send_message(embed=embed)
            return

        await interaction.response.defer()

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(["tail", "-20", log_path], capture_output=True, text=True, timeout=10)
            )
            if result.returncode == 0:
                logs_content = result.stdout.strip()
                if not logs_content:
                    logs_content = "Log file is empty."
                embed = discord.Embed(
                    title=f"📄 Recent Logs: {service}",
                    description=f"```{logs_content}```",
                    color=0x3498db
                )
                embed.set_footer(text="Mutiny Bot • Local Only")
            else:
                embed = discord.Embed(
                    title=f"❌ Failed to Read Logs: {service}",
                    description=f"Error: {result.stderr.strip()}",
                    color=0x3498db
                )
                embed.set_footer(text="Mutiny Bot • Local Only")
        except subprocess.TimeoutExpired:
            embed = discord.Embed(
                title=f"❌ Timeout Reading Logs: {service}",
                description="Log reading timed out.",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except FileNotFoundError:
            embed = discord.Embed(
                title=f"❌ Log File Not Found: {service}",
                description=f"Path: {log_path}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except PermissionError:
            embed = discord.Embed(
                title=f"❌ Permission Denied: {service}",
                description=f"Cannot read {log_path}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title=f"❌ Error Reading Logs: {service}",
                description=f"Unexpected error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="switch-model", description="Switch the AI model 🤖")
    @app_commands.autocomplete(model_name=switch_model_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def switch_model(self, interaction: discord.Interaction, model_name: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to switch models.",
                ephemeral=True,
            )
            return

        if model_name not in ALLOWED_MODELS:
            embed = discord.Embed(
                title="❌ Invalid Model",
                description=f"Model '{model_name}' is not in the allowed models list.\n\nAllowed models: {', '.join(ALLOWED_MODELS)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.response.send_message(embed=embed)
            return

        await self.bot.db_manager.update_config("model", model_name)
        await interaction.response.send_message(f"🤖 Model switched to {model_name}")

    @app_commands.command(name="restart-bot", description="Restart the bot with confirmation 🔄")
    @app_commands.default_permissions(manage_guild=True)
    async def restart_bot(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to restart the bot.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can restart the bot.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔄 Restart Bot",
            description="Are you sure you want to restart the bot? This will disconnect it temporarily.",
            color=0x3498db
        )
        embed.set_footer(text="Mutiny Bot • Local Only")
        view = RestartConfirmView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="snooze-job", description="Pause a job for specified hours ⏰")
    @app_commands.default_permissions(manage_guild=True)
    async def snooze_job(self, interaction: discord.Interaction, job_id: str, hours: int = 1) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to snooze jobs.",
                ephemeral=True,
            )
            return

        scheduler = self.bot.scheduler_manager.scheduler
        job = scheduler.get_job(job_id)
        if not job:
            embed = discord.Embed(
                title="❌ Job Not Found",
                description=f"No job found with ID '{job_id}'.",
                color=0xff0000
            )
            await interaction.response.send_message(embed=embed)
            return

        try:
            job.pause()
            # Schedule resume using a named function — lambdas cannot be pickled
            # by the SQLAlchemy jobstore.
            resume_time = datetime.now() + timedelta(hours=hours)
            scheduler.add_job(
                func=resume_job,
                trigger="date",
                run_date=resume_time,
                id=f"resume_{job_id}",
                name=f"Resume job {job_id}",
                args=(job_id,),
            )
            embed = discord.Embed(
                title="⏰ Job Snoozed",
                description=f"Job '{job.name}' (ID: {job_id}) has been paused for {hours} hour(s).\n\nIt will resume automatically at {resume_time.strftime('%Y-%m-%d %H:%M:%S')}.",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Failed to Snooze Job",
                description=f"Error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="daily-insight", description="Get a fun daily system insight 💡")
    @app_commands.default_permissions(manage_guild=True)
    async def daily_insight(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view daily insights.",
                ephemeral=True,
            )
            return

        # Acknowledge immediately to avoid Discord interaction timeout (10062).
        await interaction.response.defer()

        snapshot = collect_local_system_snapshot()
        insight = generate_daily_insight(snapshot)

        embed = discord.Embed(
            title="💡 Daily System Insight",
            description=insight,
            color=0x3498db
        )
        embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="explain-error", description="Get LLM explanation for an error message 🐛")
    @app_commands.default_permissions(manage_guild=True)
    async def explain_error(self, interaction: discord.Interaction, error_message: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to explain errors.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can get error explanations.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        model = await self.bot.db_manager.get_current_model()
        system_prompt = "You are a helpful programming assistant. Explain the following error message in simple terms and suggest how to fix it. Keep your response concise and practical."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Error: {error_message}"}
        ]
        
        try:
            explanation = await self.bot.llm_handler.generate_response(model, messages)
            embed = discord.Embed(
                title="🐛 Error Explanation",
                description=explanation,
                color=0x3498db
            )
            embed.add_field(name="Original Error", value=error_message[:500] + "..." if len(error_message) > 500 else error_message, inline=False)
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Failed to Explain Error",
                description=f"Error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="ask-notes", description="Ask questions about remembered facts and chat history 📝")
    @app_commands.default_permissions(manage_guild=True)
    async def ask_notes(self, interaction: discord.Interaction, question: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to ask notes.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can query notes and history.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        wing = self._safe_guild_name(interaction)
        room = self._safe_room_name(interaction)

        # Get facts and channel history from MemPalace.
        try:
            fact_items = self._search_memories(query=question, wing=wing, room="remembered-facts")
        except Exception:
            fact_items = []

        try:
            history_items = self._search_memories(query=question, wing=wing, room=room)
        except Exception:
            history_items = []

        facts = [self._memory_text(item) for item in fact_items if self._memory_text(item).strip()]
        history = [self._memory_text(item) for item in history_items if self._memory_text(item).strip()]
        
        # Build context
        context_parts = []
        if facts:
            context_parts.append("**Remembered Facts:**\n" + "\n".join(f"- {fact}" for fact in facts[:20]))
        
        if history:
            history_text = "\n".join(f"memory: {item[:100]}" for item in history[:20])
            context_parts.append("**Recent Chat History:**\n" + history_text)
        
        context = "\n\n".join(context_parts) if context_parts else "No facts or history available."
        
        model = await self.bot.db_manager.get_current_model()
        system_prompt = "You are a helpful assistant. Based on the provided facts and chat history, give a short, direct answer to the question. If the information isn't available, say so clearly."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
        ]
        
        try:
            answer = await self.bot.llm_handler.generate_response(model, messages)
            embed = discord.Embed(
                title="📝 Answer from Notes",
                color=0x3498db
            )
            embed.add_field(name="Question", value=question, inline=False)
            embed.add_field(name="Answer", value=answer, inline=False)
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Failed to Get Answer",
                description=f"Error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="schedule", description="Schedule a recurring task ⏰")
    @app_commands.default_permissions(manage_guild=True)
    async def schedule(self, interaction: discord.Interaction, task: str, time: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to schedule tasks.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can schedule tasks.",
                ephemeral=True,
            )
            return

        try:
            trigger = parse_schedule_time(time)
        except ValueError as e:
            embed = discord.Embed(
                title="❌ Invalid Time Format",
                description=str(e),
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.response.send_message(embed=embed)
            return

        scheduler = self.bot.scheduler_manager.scheduler

        # Validate that task maps to a registered tool
        if task not in AVAILABLE_TOOLS:
            available = ", ".join(sorted(AVAILABLE_TOOLS.keys())) or "none"
            embed = discord.Embed(
                title="❌ Unknown Tool",
                description=(
                    f"**{task}** is not a registered tool.\n\n"
                    f"Available tools: {available}"
                ),
                color=0xff0000,
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
            await interaction.response.send_message(embed=embed)
            return

        # Sanitize task name for use in the job ID (alphanumeric + underscores only)
        safe_task = re.sub(r"[^A-Za-z0-9_]", "_", task)
        job = scheduler.add_job(
            execute_and_broadcast,
            trigger=trigger,
            id=f"auto_{safe_task}_{datetime.now().timestamp()}",
            name=f"Scheduled: {task}",
            args=(task,),
            replace_existing=True,
        )

        embed = discord.Embed(
            title="⏰ Task Scheduled",
            description=f"Successfully scheduled task: **{task}**",
            color=0x3498db
        )
        embed.add_field(name="Job ID", value=job.id, inline=True)
        embed.add_field(name="Schedule", value=time, inline=True)
        if job.next_run_time:
            embed.add_field(name="Next Run", value=job.next_run_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="brainstorm", description="Get creative ideas from AI 🧠")
    @app_commands.default_permissions(manage_guild=True)
    async def brainstorm(self, interaction: discord.Interaction, idea: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to brainstorm.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can use AI brainstorming.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        model = await self.bot.db_manager.get_current_model()
        system_prompt = "You are a creative brainstorming assistant. Generate 5-7 innovative ideas or solutions for the given topic. Number them clearly and keep each idea concise but detailed enough to be useful."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Brainstorm ideas for: {idea}"}
        ]
        
        try:
            ideas = await self.bot.llm_handler.generate_response(model, messages)
            embed = discord.Embed(
                title=f"🧠 Brainstorm: {idea}",
                description=ideas,
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Brainstorm Failed",
                description=f"Error: {str(e)}",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="list-tools", description="List all available AI tools 🛠️")
    @app_commands.default_permissions(manage_guild=True)
    async def list_tools(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to list tools.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can list AI tools.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if not TOOL_SCHEMAS:
            embed = discord.Embed(
                title="🛠️ Available AI Tools",
                description="No tools are currently registered.",
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")
        else:
            tool_list = "\n".join([
                f"**{schema['function']['name']}**: {schema['function']['description']}"
                for schema in TOOL_SCHEMAS
            ])
            embed = discord.Embed(
                title="🛠️ Available AI Tools",
                description=tool_list,
                color=0x3498db
            )
            embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Show help for all monitoring commands ❓")
    @app_commands.default_permissions(manage_guild=True)
    async def help(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to view help.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🛠️ Monitoring Commands Help",
            description="All commands require Manage Server permission and must be used in the monitoring channel.",
            color=0x3498db
        )

        # Monitoring & Status
        embed.add_field(
            name="📊 Monitoring & Status",
            value=(
                "**/botstatus** - Show bot status and configuration\n"
                "**/system** - Show system information and resources\n"
                "**/ping** - Ping a host and show latency\n"
                "**/docker** - List running Docker containers\n"
                "**/logs** - Show recent logs for a service\n"
                "**/daily-insight** - Get a fun daily system insight"
            ),
            inline=False
        )

        # Job Management
        embed.add_field(
            name="🔔 Job Management",
            value=(
                "**/jobs** - List active scheduled jobs\n"
                "**/schedule** - Schedule a recurring task\n"
                "**/snooze-job** - Pause a job for specified hours"
            ),
            inline=False
        )

        # Data Management
        embed.add_field(
            name="📚 Data Management",
            value=(
                "**/history** - Show recent chat history\n"
                "**/clear-history** - Clear chat history for current user\n"
                "**/reset** - Reset chat history for the current user\n"
                "**/remember** - Save a fact permanently\n"
                "**/recall** - Show all saved facts\n"
                "**/ask-notes** - Ask questions about remembered facts and chat history"
            ),
            inline=False
        )

        # AI Tools
        embed.add_field(
            name="🤖 AI Tools",
            value=(
                "**/quick-run** - Run a tool immediately\n"
                "**/generate-script** - Generate a bash script using AI\n"
                "**/explain-error** - Get LLM explanation for an error message\n"
                "**/brainstorm** - Get creative ideas from AI\n"
                "**/list-tools** - List all available AI tools\n"
                "**/switch-model** - Switch the AI model"
            ),
            inline=False
        )

        # Bot Control
        embed.add_field(
            name="🔧 Bot Control",
            value=(
                "**/restart-bot** - Restart the bot with confirmation\n"
                "**/sync-commands** - Sync slash commands with Discord"
            ),
            inline=False
        )

        embed.set_footer(text="Mutiny Bot • Local Only")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="post-commands", description="Post full command list to a channel 📋")
    @app_commands.default_permissions(manage_guild=True)
    async def post_commands(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to post commands.",
                ephemeral=True,
            )
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can post the full command list.",
                ephemeral=True,
            )
            return

        # Use specified channel or current channel
        target_channel = channel or interaction.channel
        if not target_channel:
            await interaction.response.send_message(
                "Could not determine target channel.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🛠️ Complete Command Reference",
            description="All slash commands require Manage Server permission and must be used in the monitoring channel. Commands have a 3-second cooldown.",
            color=0x3498db
        )

        # Monitoring & Status
        embed.add_field(
            name="📊 Monitoring & Status",
            value=(
                "**/botstatus** - Show bot status and configuration\n"
                "**/system** - Show system information and resources\n"
                "**/ping <host>** - Ping a host and show latency\n"
                "**/docker** - List running Docker containers\n"
                "**/logs <service>** - Show recent logs for a service\n"
                "**/daily-insight** - Get a fun daily system insight"
            ),
            inline=False
        )

        # Job Management
        embed.add_field(
            name="🔔 Job Management",
            value=(
                "**/jobs** - List active scheduled jobs\n"
                "**/schedule <task> <time>** - Schedule a recurring task\n"
                "**/snooze-job <job-id> <hours>** - Pause a job for specified hours\n"
                "**/add_news_monitor <channel> <name> <search_query> [frequency] [time]** - Add a news monitor job\n"
                "**/list_news_monitors** - List all active news monitors\n"
                "**/remove_news_monitor <name>** - Remove a news monitor job\n"
                "**/run_news_monitor <name>** - Manually run a news monitor job"
            ),
            inline=False
        )

        # Data Management
        embed.add_field(
            name="📚 Data Management",
            value=(
                "**/history** - Show recent chat history\n"
                "**/clear-history** - Clear chat history for current user\n"
                "**/reset** - Reset chat history for the current user\n"
                "**/remember <fact>** - Save a fact permanently\n"
                "**/recall** - Show all saved facts\n"
                "**/ask-notes <question>** - Ask questions about remembered facts and chat history"
            ),
            inline=False
        )

        # AI Tools
        embed.add_field(
            name="🤖 AI Tools",
            value=(
                "**/quick-run <tool-name>** - Run a tool immediately\n"
                "**/generate-script <task>** - Generate a bash script using AI\n"
                "**/explain-error <error>** - Get LLM explanation for an error message\n"
                "**/brainstorm <idea>** - Get creative ideas from AI\n"
                "**/list-tools** - List all available AI tools\n"
                "**/switch-model <model>** - Switch the AI model"
            ),
            inline=False
        )

        # Bot Control
        embed.add_field(
            name="🔧 Bot Control",
            value=(
                "**/restart-bot** - Restart the bot with confirmation\n"
                "**/sync-commands** - Sync slash commands with Discord"
            ),
            inline=False
        )

        # Utilities
        embed.add_field(
            name="🛠️ Utilities",
            value=(
                "**/post-commands [channel]** - Post this command reference\n"
                "**/help** - Show help for all commands"
            ),
            inline=False
        )

        embed.set_footer(text="Mutiny Bot • Local Only")

        await cast(discord.TextChannel, target_channel).send(embed=embed)
        await interaction.response.send_message("✅ Full command list posted!", ephemeral=True)

    async def post_news(self, channel_id: int, search_query: str, dedup_room: str, palace_path: str) -> None:
        """Post fresh news articles to the specified channel."""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        articles = await get_fresh_news(search_query, dedup_room, palace_path)
        for article in articles:
            embed = discord.Embed(
                title=article['title'],
                description=article['summary'],
                url=article['link']
            )
            embed.add_field(name="Published", value=article['published'])
            await channel.send(embed=embed)

    @app_commands.command(name="sync-commands", description="Sync slash commands with Discord 🔄")
    @app_commands.default_permissions(manage_guild=True)
    async def sync_commands(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._is_bot_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can sync commands.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            guild = interaction.guild
            if guild is None:
                await interaction.followup.send(
                    "❌ This command can only be used in a server.",
                    ephemeral=True,
                )
                return

            self.bot.tree.clear_commands(guild=guild)
            self.bot.tree.copy_global_to(guild=guild)
            synced = await self.bot.tree.sync(guild=guild)
            await interaction.followup.send(
                f"✅ Synced {len(synced)} slash commands to this server without duplicates.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to sync commands: {str(e)}", ephemeral=True)

    @app_commands.command(name="add_news_monitor", description="Add a news monitor job 📰")
    @app_commands.default_permissions(manage_guild=True)
    async def add_news_monitor(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        name: str,
        search_query: str,
        frequency: str = "daily",
        time: str = "08:00"
    ) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to add news monitors.",
                ephemeral=True,
            )
            return

        time_str = f"{frequency} at {time}" if frequency == "daily" else frequency
        trigger = parse_schedule_time(time_str)
        if not trigger:
            await interaction.response.send_message(
                "Invalid frequency or time format.",
                ephemeral=True,
            )
            return

        palace_path = os.path.expanduser("~/.mutiny/palace")
        dedup_room = name
        scheduler = self.bot.scheduler_manager.scheduler
        job_id = f"news_monitor_{name}"
        if scheduler.get_job(job_id):
            await interaction.response.send_message(
                f"News monitor '{name}' already exists.",
                ephemeral=True,
            )
            return

        job_data = {
            "name": name,
            "search_query": search_query,
            "channel_id": channel.id,
            "palace_path": palace_path
        }

        scheduler.add_job(
            execute_news_monitor,
            trigger=trigger,
            args=[job_data],
            id=job_id,
            name=f"News Monitor: {name}"
        )
        await interaction.response.send_message(
            f"✅ Added news monitor '{name}' for '{search_query}' in {channel.mention}"
        )

    @app_commands.command(name="list_news_monitors", description="List news monitor jobs 📋")
    @app_commands.default_permissions(manage_guild=True)
    async def list_news_monitors(self, interaction: discord.Interaction) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to list news monitors.",
                ephemeral=True,
            )
            return

        scheduler = self.bot.scheduler_manager.scheduler
        jobs = scheduler.get_jobs()
        news_jobs = [job for job in jobs if job.id.startswith("news_monitor_")]

        embed = discord.Embed(title="📰 News Monitors", color=0x3498db)
        if not news_jobs:
            embed.description = "No news monitors found."
        else:
            for job in news_jobs:
                next_run = job.next_run_time
                if next_run:
                    next_run = next_run.strftime("%Y-%m-%d %H:%M")
                else:
                    next_run = "N/A"
                embed.add_field(
                    name=job.name,
                    value=f"ID: {job.id}\nNext: {next_run}",
                    inline=False
                )
        embed.set_footer(text="Mutiny Bot • Local Only")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove_news_monitor", description="Remove a news monitor job 🗑️")
    @app_commands.default_permissions(manage_guild=True)
    async def remove_news_monitor(self, interaction: discord.Interaction, name: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to remove news monitors.",
                ephemeral=True,
            )
            return

        scheduler = self.bot.scheduler_manager.scheduler
        job_id = f"news_monitor_{name}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            await interaction.response.send_message(f"✅ Removed news monitor '{name}'")
        else:
            await interaction.response.send_message(f"❌ No news monitor found with name '{name}'")

    @app_commands.command(name="run_news_monitor", description="Manually run a news monitor job ▶️")
    @app_commands.default_permissions(manage_guild=True)
    async def run_news_monitor(self, interaction: discord.Interaction, name: str) -> None:
        if not self._check_channel(interaction):
            await self._reject_unavailable(interaction)
            return

        if not self._has_admin_permissions(interaction):
            await interaction.response.send_message(
                "You need Manage Server permission to run news monitors.",
                ephemeral=True,
            )
            return

        # Acknowledge quickly to avoid Discord interaction timeout (10062).
        await interaction.response.defer(ephemeral=True)

        scheduler = self.bot.scheduler_manager.scheduler
        job_id = f"news_monitor_{name}"
        job = scheduler.get_job(job_id)
        if not job:
            await interaction.followup.send(
                f"❌ No news monitor found with name '{name}'",
                ephemeral=True,
            )
            return

        try:
            job_data = job.args[0]
            from tools.news_monitor import execute_news_monitor
            await execute_news_monitor(job_data)
            await interaction.followup.send(
                f"✅ Manually ran monitor '{name}' — check the channel",
                ephemeral=True,
            )
        except Exception as e:
            logging.exception("Error running news monitor '%s'", name)
            await interaction.followup.send(
                f"❌ Failed to run news monitor '{name}': {str(e)}",
                ephemeral=True,
            )


async def setup(bot) -> None:
    await bot.add_cog(MonitoringCog(bot))
