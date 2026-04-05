"""Scheduler manager: AI-driven tools for automation control via chat."""

from contextvars import ContextVar, Token
from datetime import datetime
from inspect import isawaitable
from typing import Any, Optional, Tuple, cast

import aiosqlite

from config import AUTOMATION_TIMEZONE, BROADCAST_CHANNEL_ID, DB_PATH
from tools.registry import AVAILABLE_TOOLS, ai_tool

CURRENT_TOOL_USER_ID: ContextVar[Optional[str]] = ContextVar("current_tool_user_id", default=None)
CURRENT_TOOL_IS_ADMIN: ContextVar[bool] = ContextVar("current_tool_is_admin", default=False)
CURRENT_TOOL_SCHEDULER: ContextVar[Any] = ContextVar("current_tool_scheduler", default=None)


def setup(bot: Any) -> None:
    """Module setup hook kept for extension loader compatibility."""
    _ = bot


def set_tool_request_context(
    user_id: Optional[str],
    is_admin: bool,
    scheduler: Any,
) -> Tuple[Token[Any], Token[Any], Token[Any]]:
    """Set per-request context for AI tool authorization and scheduler access."""
    user_token = CURRENT_TOOL_USER_ID.set(user_id)
    admin_token = CURRENT_TOOL_IS_ADMIN.set(bool(is_admin))
    scheduler_token = CURRENT_TOOL_SCHEDULER.set(scheduler)
    return user_token, admin_token, scheduler_token


def reset_tool_request_context(tokens: Tuple[Token[Any], Token[Any], Token[Any]]) -> None:
    """Reset per-request context for AI tool authorization and scheduler access."""
    user_token, admin_token, scheduler_token = tokens
    CURRENT_TOOL_USER_ID.reset(user_token)
    CURRENT_TOOL_IS_ADMIN.reset(admin_token)
    CURRENT_TOOL_SCHEDULER.reset(scheduler_token)


def _require_admin_automation_access() -> Optional[str]:
    """Return an error string when the current tool caller is not authorized."""
    if CURRENT_TOOL_IS_ADMIN.get():
        return None

    return "Error: You are not authorized to manage automations. Manage Server or Administrator permission is required."


def _get_scheduler_from_context() -> Tuple[Optional[Any], Optional[str]]:
    """Return scheduler from request context or an authorization-style error."""
    scheduler = CURRENT_TOOL_SCHEDULER.get()
    if scheduler is None:
        return None, "Error: Scheduler context is unavailable for this request."
    return scheduler, None


async def _invoke_registered_tool(tool_name: str) -> str:
    """Execute a registered tool and normalize result to string."""
    if tool_name not in AVAILABLE_TOOLS:
        return f"Error: Tool '{tool_name}' not found."

    result = AVAILABLE_TOOLS[tool_name]()
    result_text = await result if isawaitable(result) else result
    return str(result_text or "")


async def _enqueue_broadcast(content: str) -> None:
    """Queue one broadcast message for delivery by the scheduler broadcast loop."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO broadcast_queue (content) VALUES (?)",
            (content,),
        )
        await db.commit()


async def execute_and_broadcast(tool_name: str) -> str:
    """Execute a registered tool and enqueue its result for Discord broadcast."""
    if not isinstance(BROADCAST_CHANNEL_ID, int) or BROADCAST_CHANNEL_ID <= 0:
        return "Error: Invalid or unconfigured BROADCAST_CHANNEL_ID."

    try:
        result_text = await _invoke_registered_tool(tool_name)
        if result_text.startswith("Error:"):
            return result_text

        await _enqueue_broadcast(f"🤖 **AUTOMATED TASK: {tool_name}**\n\n{result_text}")
        return f"Successfully executed and queued broadcast for '{tool_name}'."
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
                "description": "Hour (0-23) in configured automation timezone",
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
    """Schedule a tool to run daily at a specific time in configured timezone."""
    authorization_error = _require_admin_automation_access()
    if authorization_error:
        return authorization_error

    scheduler, scheduler_error = _get_scheduler_from_context()
    if scheduler_error:
        return scheduler_error
    scheduler = cast(Any, scheduler)

    if tool_name not in AVAILABLE_TOOLS:
        return f"Error: Tool '{tool_name}' not found in available tools."

    if tool_name in {"schedule_daily_automation", "list_active_automations", "stop_automation"}:
        return f"Error: Tool '{tool_name}' cannot be scheduled recursively."

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "Error: Invalid hour (0-23) or minute (0-59)."

    try:
        job_id = f"auto_{tool_name}_{hour}_{minute}_{datetime.now().timestamp()}"
        scheduler.add_job(
            execute_and_broadcast,
            "cron",
            hour=hour,
            minute=minute,
            timezone=AUTOMATION_TIMEZONE,
            args=(tool_name,),
            id=job_id,
            replace_existing=False,
        )
        return (
            f"✅ Scheduled '{tool_name}' to run daily at {hour:02d}:{minute:02d} "
            f"{AUTOMATION_TIMEZONE} time. Job ID: {job_id}"
        )
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
    authorization_error = _require_admin_automation_access()
    if authorization_error:
        return authorization_error

    scheduler, scheduler_error = _get_scheduler_from_context()
    if scheduler_error:
        return scheduler_error
    scheduler = cast(Any, scheduler)

    try:
        jobs = scheduler.get_jobs()
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
    authorization_error = _require_admin_automation_access()
    if authorization_error:
        return authorization_error

    scheduler, scheduler_error = _get_scheduler_from_context()
    if scheduler_error:
        return scheduler_error
    scheduler = cast(Any, scheduler)

    try:
        scheduler.remove_job(job_id)
        return f"✅ Successfully stopped automation: {job_id}"
    except Exception as e:
        return f"Error stopping automation: {e}"
