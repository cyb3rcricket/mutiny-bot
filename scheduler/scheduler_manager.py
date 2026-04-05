"""Scheduler manager for MutinyBot APScheduler and broadcast operations."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import tasks

from config import BROADCAST_CHANNEL_ID


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
                return

        try:
            await channel.send(content)
        except Exception:
            return

        await self.bot.db_manager.delete_broadcast(message_id)

    @check_broadcast_queue.before_loop
    async def before_check_broadcast_queue(self) -> None:
        """Wait for bot readiness before polling broadcast queue."""
        await self.bot.wait_until_ready()