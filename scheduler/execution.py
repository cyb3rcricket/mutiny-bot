"""Stable scheduler entrypoint.

APScheduler pickles this function's import path. Live services are bound at
process start and are never stored in the job database.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.tool_runner import SCHEDULABLE_TOOL_NAMES, ToolRejected, assert_schedulable
from database.db import DatabaseManager
from tools.registry import AVAILABLE_TOOLS

logger = logging.getLogger("mutiny_bot.scheduler")

_db: DatabaseManager | None = None
_TOOL_TIMEOUT_SECONDS = 30.0
_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


def bind_database(db: DatabaseManager | None) -> None:
    global _db
    _db = db


def _arguments_are_primitive(value: Any) -> bool:
    if isinstance(value, _PRIMITIVE_TYPES):
        return True
    if isinstance(value, list):
        return all(_arguments_are_primitive(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _arguments_are_primitive(item) for key, item in value.items())
    return False


async def run_scheduled_job(tool_name: str, arguments: dict[str, Any] | None = None, job_id: str | None = None) -> str:
    """Execute one persisted job and store its output. Returns the run id."""
    if _db is None:
        raise RuntimeError("Scheduler database is not bound.")
    arguments = dict(arguments or {})
    if not _arguments_are_primitive(arguments):
        run = await _db.create_run(tool_name=str(tool_name), arguments={}, job_id=job_id, status="running")
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="invalid_arguments",
            output="Job arguments must be plain values.",
        )
        return str(run["id"])

    run = await _db.create_run(
        tool_name=str(tool_name),
        arguments=arguments,
        job_id=job_id,
        status="running",
    )
    if tool_name not in SCHEDULABLE_TOOL_NAMES:
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="policy_denied",
            output="This tool is not allowed to run on a schedule.",
        )
        return str(run["id"])
    try:
        assert_schedulable(tool_name)
    except ToolRejected:
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="policy_denied",
            output="This tool is not allowed to run on a schedule.",
        )
        return str(run["id"])

    tool = AVAILABLE_TOOLS.get(tool_name)
    if tool is None:
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="tool_missing",
            output="This tool is not available.",
        )
        return str(run["id"])

    try:
        result = tool(**arguments)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=_TOOL_TIMEOUT_SECONDS)
        text = "" if result is None else str(result)
        await _db.finish_run(run["id"], status="complete", output=text)
    except asyncio.TimeoutError:
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="timeout",
            output="The tool timed out.",
        )
    except Exception:
        logger.exception("Scheduled tool failed")
        await _db.finish_run(
            run["id"],
            status="failed",
            error_code="tool_failed",
            output="The tool failed.",
        )
    return str(run["id"])
