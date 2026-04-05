"""Scheduler manager: AI-driven tools for automation control via chat."""

import os
from datetime import datetime

from tools.registry import AVAILABLE_TOOLS, ai_tool

# Global reference to bot instance, set during module setup.
BOT_INSTANCE = None


def setup(bot) -> None:
    """Register the bot instance for scheduler access."""
    global BOT_INSTANCE
    BOT_INSTANCE = bot


async def execute_and_broadcast(tool_name: str) -> str:
    """Execute a registered tool and broadcast its result to Discord."""
    if BOT_INSTANCE is None:
        return "Error: Bot instance not available."

    if tool_name not in AVAILABLE_TOOLS:
        return f"Error: Tool '{tool_name}' not found."

    channel_id_str = os.getenv("BROADCAST_CHANNEL_ID", "0")
    try:
        channel_id = int(channel_id_str)
    except (ValueError, TypeError):
        return "Error: BROADCAST_CHANNEL_ID is not configured."

    if channel_id <= 0:
        return "Error: Invalid BROADCAST_CHANNEL_ID."

    try:
        result = AVAILABLE_TOOLS[tool_name]()
        result_text = await result if hasattr(result, "__await__") else result
        result_text = str(result_text or "")

        channel = BOT_INSTANCE.get_channel(channel_id)
        if channel is None:
            channel = await BOT_INSTANCE.fetch_channel(channel_id)

        if channel:
            await channel.send(f"🤖 **AUTOMATED TASK: {tool_name}**\n\n{result_text}")
            return f"Successfully executed and broadcast '{tool_name}'."
    except Exception as e:
        return f"Error executing tool: {e}"


@ai_tool(
    name="schedule_daily_automation",
    description="Schedule a registered AI tool to run daily at a specific time",
    parameters={
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Name of the tool to schedule (e.g., 'get_morning_briefing')",
            },
            "hour": {
                "type": "integer",
                "description": "Hour (0-23) in America/Chicago timezone",
            },
            "minute": {
                "type": "integer",
                "description": "Minute (0-59)",
            },
        },
        "required": ["tool_name", "hour", "minute"],
    },
)
async def schedule_daily_automation(tool_name: str, hour: int, minute: int) -> str:
    """Schedule a tool to run daily at a specific time (America/Chicago TZ)."""
    if BOT_INSTANCE is None:
        return "Error: Bot instance not available."

    if tool_name not in AVAILABLE_TOOLS:
        return f"Error: Tool '{tool_name}' not found in available tools."

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "Error: Invalid hour (0-23) or minute (0-59)."

    try:
        job_id = f"auto_{tool_name}_{hour}_{minute}_{datetime.now().timestamp()}"
        BOT_INSTANCE.scheduler.add_job(
            execute_and_broadcast,
            "cron",
            hour=hour,
            minute=minute,
            timezone="America/Chicago",
            args=(tool_name,),
            id=job_id,
            replace_existing=False,
        )
        return f"✅ Scheduled '{tool_name}' to run daily at {hour:02d}:{minute:02d} America/Chicago time. Job ID: {job_id}"
    except Exception as e:
        return f"Error scheduling automation: {e}"


@ai_tool(
    name="list_active_automations",
    description="List all currently active scheduled automations",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def list_active_automations() -> str:
    """Return a formatted list of all active scheduled jobs."""
    if BOT_INSTANCE is None:
        return "Error: Bot instance not available."

    try:
        jobs = BOT_INSTANCE.scheduler.get_jobs()
        if not jobs:
            return "No active automations scheduled."

        lines = ["📋 **ACTIVE AUTOMATIONS:**"]
        for job in jobs:
            next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "Unknown"
            lines.append(f"• **{job.id}** → Next run: {next_run}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error listing automations: {e}"


@ai_tool(
    name="stop_automation",
    description="Stop and remove a scheduled automation by job ID",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The unique ID of the automation job to stop",
            },
        },
        "required": ["job_id"],
    },
)
async def stop_automation(job_id: str) -> str:
    """Stop and remove a scheduled job."""
    if BOT_INSTANCE is None:
        return "Error: Bot instance not available."

    try:
        BOT_INSTANCE.scheduler.remove_job(job_id)
        return f"✅ Successfully stopped automation: {job_id}"
    except Exception as e:
        return f"Error stopping automation: {e}"
