"""Scheduler manager for MutinyBot APScheduler and broadcast operations."""

import asyncio
import logging
from typing import Any, cast

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import tasks

from config import BROADCAST_CHANNEL_ID, SCHEDULER_DB_PATH
from scheduler.broadcast_utils import split_broadcast_chunks

try:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
except ImportError:  # pragma: no cover - exercised in dependency-limited runtime only
    SQLAlchemyJobStore = None


class SchedulerManager:
    """Handles scheduling and broadcast queue for the bot."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._logger = logging.getLogger("mutiny_bot.scheduler")
        if SQLAlchemyJobStore is not None:
            # Keep scheduler persistence separate from chat/config DB writes.
            jobstore_url = f"sqlite:///{SCHEDULER_DB_PATH}"
            jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
            self.scheduler = AsyncIOScheduler(jobstores=jobstores)
        else:
            self._logger.warning(
                "SQLAlchemyJobStore is unavailable; using in-memory scheduler jobs. "
                "Install SQLAlchemy for persistent schedules."
            )
            self.scheduler = AsyncIOScheduler()
        # Set the scheduler on the bot for tools to access
        self.bot.scheduler = self.scheduler

    async def start_scheduler(self) -> None:
        """Start the scheduler if not already running."""
        if not self.scheduler.running:
            self.scheduler.start()

    def start_broadcast_task(self) -> None:
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
                channel = await asyncio.wait_for(
                    self.bot.fetch_channel(BROADCAST_CHANNEL_ID),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Timed out fetching broadcast channel %s",
                    BROADCAST_CHANNEL_ID,
                )
                return
            except (discord.NotFound, discord.Forbidden):
                self._logger.error(
                    "Broadcast channel %s is unavailable; dropping queued message id=%s",
                    BROADCAST_CHANNEL_ID,
                    message_id,
                )
                await self.bot.db_manager.delete_broadcast(message_id)
            except Exception:
                self._logger.exception("Failed to fetch broadcast channel %s", BROADCAST_CHANNEL_ID)
                return

        try:
            sendable_channel = cast(Any, channel)
            chunks = split_broadcast_chunks(content)
            for chunk in chunks:
                await sendable_channel.send(chunk)
        except Exception:
            self._logger.exception("Failed to send broadcast message id=%s to channel %s", message_id, BROADCAST_CHANNEL_ID)
            # Drop the failed item so one bad payload does not permanently block the queue.
            await self.bot.db_manager.delete_broadcast(message_id)
            return

        await self.bot.db_manager.delete_broadcast(message_id)

    @check_broadcast_queue.before_loop
    async def before_check_broadcast_queue(self) -> None:
        """Wait for bot readiness before polling broadcast queue."""
        await self.bot.wait_until_ready()

    async def get_active_jobs(self) -> list[dict[str, Any]]:
        """Return a list of active jobs with their ID, next run time, and name."""
        jobs_info = []
        try:
            jobs = list(self.scheduler.get_jobs())
        except Exception:
            return jobs_info

        for job in jobs:
            try:
                jobs_info.append(
                    {
                        "id": getattr(job, "id", None),
                        "name": getattr(job, "name", None),
                        "next_run_time": getattr(job, "next_run_time", None),
                    }
                )
            except Exception:
                # Skip problematic job entries but continue
                continue

        return jobs_info