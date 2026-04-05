"""Scheduler manager for MutinyBot APScheduler and broadcast operations."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import tasks

from config import BROADCAST_CHANNEL_ID

logger = logging.getLogger("mutiny_bot")


class SchedulerManager:
    """Handles scheduling and broadcast queue for the bot."""

    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler()
        # Set the scheduler on the bot for tools to access
        self.bot.scheduler = self.scheduler

    async def start_scheduler(self):
        """Start the scheduler if not already running."""
        if not self.scheduler.running:
            self.scheduler.start()

    def start_broadcast_task(self):
        """Start the broadcast queue checking task."""
        if not self.check_broadcast_queue.is_running():
            self.check_broadcast_queue.start()

    @tasks.loop(seconds=2)
    async def check_broadcast_queue(self) -> None:
        """Send queued manual broadcast messages to the configured Discord channel."""
        if BROADCAST_CHANNEL_ID <= 0:
            return

        broadcast = await self.bot.db_manager.get_next_broadcast()
        if not broadcast:
            return

        message_id, content = broadcast
        if not content:
            await self.bot.db_manager.delete_broadcast(message_id)
            return

        channel = self.bot.get_channel(BROADCAST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(BROADCAST_CHANNEL_ID)
            except Exception:
                logger.exception("Failed to fetch broadcast channel %d", BROADCAST_CHANNEL_ID)
                return

        try:
            await channel.send(content)
        except Exception:
            logger.exception("Failed to send broadcast message to channel %d", BROADCAST_CHANNEL_ID)
            return

        await self.bot.db_manager.delete_broadcast(message_id)

    @check_broadcast_queue.before_loop
    async def before_check_broadcast_queue(self) -> None:
        """Wait for bot readiness before polling broadcast queue."""
        await self.bot.wait_until_ready()

    async def get_active_jobs(self) -> list:
        """Return a list of active jobs with basic metadata.

        Each job is represented as a dict containing at least: id, name, next_run_time, trigger, and func.
        """
        jobs_info = []
        try:
            jobs = list(self.scheduler.get_jobs())
        except Exception:
            return jobs_info

        for job in jobs:
            try:
                func_name = None
                if hasattr(job, "func") and job.func is not None:
                    func_name = getattr(job.func, "__name__", str(job.func))
                else:
                    func_name = getattr(job, "func_ref", None)

                jobs_info.append(
                    {
                        "id": getattr(job, "id", None),
                        "name": getattr(job, "name", None),
                        "next_run_time": getattr(job, "next_run_time", None),
                        "trigger": str(getattr(job, "trigger", None)),
                        "func": func_name,
                    }
                )
            except Exception:
                # Skip problematic job entries but continue
                continue

        return jobs_info