"""JSON API for the local console. Routes validate and delegate."""

from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import BIND_HOST, OUTBOUND_ENABLED, PORT
from core.chat import ChatConflict, ChatNotFound, send_message
from core.research import run_research
from core.tool_runner import MANUAL_TOOL_NAMES, ToolRejected, assert_manual_execution
from database.db import InputTooLong
from llm.llm_handler import LLMError
from llm.models import discover_models, selected_model_is_available
from memory.service import ask_notes, recall, remember
from scheduler.scheduler_manager import SchedulerUnavailable
from web.security import SESSION_COOKIE, json_error

router = APIRouter(prefix="/api")


class ThreadCreate(BaseModel):
    title: str | None = None


class MessageCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class SettingsPatch(BaseModel):
    model: str | None = None
    system_prompt: str | None = None
    automation_timezone: str | None = None


class FactCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    source_message_id: str | None = None


class ToolRunCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class DailySchedule(BaseModel):
    type: str
    time: str
    timezone: str


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    schedule: DailySchedule


class JobPatch(BaseModel):
    paused: bool


def _state(request: Request):
    return request.app.state.services


@router.get("/session")
async def session(request: Request) -> JSONResponse:
    response = JSONResponse(
        {
            "session": "local",
            "capabilities": ["chat", "memory", "jobs", "briefing"],
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.session_token,
        httponly=True,
        samesite="strict",
        path="/",
        secure=False,
    )
    return response


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    services = _state(request)
    palace = services.palace
    return {
        "bind_host": BIND_HOST,
        "port": PORT,
        "ollama_target": services.llm.api_base,
        "ollama_available": discover_models().status == "ok",
        "memory_backend": "mempalace" if palace.available else "sqlite",
        "memory_degraded_reason": palace.degraded_reason,
        "scheduler_available": services.scheduler.available,
        "outbound_enabled": bool(OUTBOUND_ENABLED),
        "outbound_features": [],
        "dangerous_tools_enabled": False,
    }


@router.get("/models")
async def models(request: Request, refresh: bool = False) -> dict[str, Any]:
    from llm.models import select_default_model

    discovery = discover_models(force_refresh=refresh)
    preferred = select_default_model(discovery.models)
    selected = await _state(request).db.get_current_model()
    return {
        "models": [{"name": name, "preferred": name == preferred} for name in discovery.models],
        "selected_model": selected,
        "selected_model_available": selected_model_is_available(selected, discovery),
        "discovery_status": discovery.status,
        "checked_at": discovery.checked_at,
    }


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, str]:
    db = _state(request).db
    return {
        "model": await db.get_current_model(),
        "system_prompt": await db.get_system_prompt(),
        "automation_timezone": await db.get_config("automation_timezone", "America/Chicago"),
    }


@router.patch("/settings")
async def patch_settings(request: Request, body: SettingsPatch) -> JSONResponse:
    db = _state(request).db
    if body.model is not None:
        discovery = discover_models(force_refresh=True)
        if discovery.status == "failed":
            return json_error(503, "discovery_failed", "Installed models could not be listed.", retryable=True)
        if body.model not in discovery.models:
            return json_error(422, "model_unavailable", "That model is not installed locally.")
        await db.update_config("model", body.model)
    if body.system_prompt is not None:
        if len(body.system_prompt) > 10_000:
            return json_error(422, "input_too_long", "The personality is too long.")
        await db.update_config("system_prompt", body.system_prompt)
    if body.automation_timezone is not None:
        try:
            ZoneInfo(body.automation_timezone)
        except ZoneInfoNotFoundError:
            return json_error(422, "invalid_timezone", "Use an IANA timezone name.")
        await db.update_config("automation_timezone", body.automation_timezone)
    return JSONResponse(await get_settings(request))


@router.get("/threads")
async def list_threads(request: Request, limit: int = 50, before: str | None = None) -> dict[str, Any]:
    return await _state(request).db.list_threads(limit=min(limit, 100), before=before)


@router.post("/threads")
async def create_thread(request: Request, body: ThreadCreate) -> dict[str, Any]:
    return await _state(request).db.create_thread(body.title)


@router.get("/threads/{thread_id}/messages")
async def list_messages(request: Request, thread_id: str, limit: int = 100, before: str | None = None) -> JSONResponse:
    if await _state(request).db.get_thread(thread_id) is None:
        return json_error(404, "not_found", "That conversation does not exist.")
    payload = await _state(request).db.list_messages(thread_id, limit=min(limit, 200), before=before)
    items = []
    for row in payload["items"]:
        items.append(
            {
                "id": row["id"],
                "thread_id": row["thread_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["timestamp"],
                "model": row["model"],
                "status": row["status"],
                "request_id": row["request_id"],
                "sources": await _state(request).db.list_sources(message_id=row["id"]),
            }
        )
    return JSONResponse({"items": items, "next_cursor": payload["next_cursor"]})


@router.post("/threads/{thread_id}/messages")
async def create_message(request: Request, thread_id: str, body: MessageCreate) -> JSONResponse:
    services = _state(request)
    try:
        result = await send_message(
            services.db,
            services.llm,
            thread_id=thread_id,
            request_id=body.request_id,
            content=body.content,
        )
    except ChatNotFound:
        return json_error(404, "not_found", "That conversation does not exist.")
    except ChatConflict:
        return json_error(409, "turn_in_progress", "This conversation already has a turn in progress.")
    except InputTooLong as exc:
        return json_error(422, "input_too_long", str(exc))
    except ValueError:
        return json_error(422, "invalid_request", "The message could not be accepted.")
    except LLMError as exc:
        return json_error(503, exc.code, exc.message, retryable=exc.retryable)
    return JSONResponse(result)


@router.delete("/threads/{thread_id}/messages")
async def delete_messages(request: Request, thread_id: str) -> Response:
    db = _state(request).db
    if await db.get_thread(thread_id) is None:
        return json_error(404, "not_found", "That conversation does not exist.")
    await db.clear_thread_messages(thread_id)
    return Response(status_code=204)


@router.post("/threads/{thread_id}/reset-context")
async def reset_context(request: Request, thread_id: str) -> JSONResponse:
    db = _state(request).db
    if await db.get_thread(thread_id) is None:
        return json_error(404, "not_found", "That conversation does not exist.")
    updated = await db.reset_context(thread_id)
    return JSONResponse(updated or {})


@router.get("/memory/facts")
async def list_facts(request: Request, limit: int = 50, before: str | None = None) -> dict[str, Any]:
    payload = await _state(request).db.list_facts(limit=min(limit, 100), before=before)
    payload["items"] = [
        {
            "id": item["id"],
            "content": item["content"],
            "created_at": item["created_at"],
            "palace_status": item.get("palace_status"),
        }
        for item in payload["items"]
    ]
    return payload


@router.post("/memory/facts")
async def create_fact(request: Request, body: FactCreate) -> JSONResponse:
    services = _state(request)
    try:
        fact = await remember(
            services.db,
            services.palace,
            content=body.content,
            request_id=body.request_id,
            source_message_id=body.source_message_id,
        )
    except InputTooLong as exc:
        return json_error(422, "input_too_long", str(exc))
    return JSONResponse(fact)


@router.get("/tools")
async def list_tools() -> dict[str, Any]:
    catalog = [
        {
            "name": "get_morning_briefing",
            "description": "Local machine briefing",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "network": False,
            "schedulable": True,
            "mutation": False,
        },
        {
            "name": "list_active_automations",
            "description": "List scheduled jobs",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "network": False,
            "schedulable": False,
            "mutation": False,
        },
        {
            "name": "recall",
            "description": "Search saved facts and palace notes",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": [],
            },
            "network": False,
            "schedulable": False,
            "mutation": False,
        },
        {
            "name": "ask_notes",
            "description": "Ask a question over saved notes",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["question"],
            },
            "network": False,
            "schedulable": False,
            "mutation": False,
        },
        {
            "name": "research",
            "description": "Execute closed-corpus research over local facts and memories",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "mode": {"type": "string", "default": "closed"},
                    "limit": {"type": "integer"},
                },
                "required": ["question"],
            },
            "network": False,
            "schedulable": False,
            "mutation": False,
        },
    ]
    return {"tools": [tool for tool in catalog if tool["name"] in MANUAL_TOOL_NAMES]}


@router.post("/tools/{name}/runs")
async def run_tool(request: Request, name: str, body: ToolRunCreate) -> JSONResponse:
    if name not in MANUAL_TOOL_NAMES or "/" in name or ".." in name:
        return json_error(404, "not_found", "That tool is not available.")
    try:
        assert_manual_execution(name)
    except ToolRejected:
        return json_error(404, "not_found", "That tool is not available.")
    services = _state(request)
    existing = await services.db.get_run_by_request(body.request_id)
    if existing:
        return JSONResponse(existing)
    run = await services.db.create_run(
        tool_name=name,
        arguments=_safe_arguments(body.arguments),
        request_id=body.request_id,
        status="running",
    )
    try:
        output, sources, error_code, final_arguments = await _execute_manual(services, name, body.arguments)
    except InputTooLong as exc:
        await services.db.finish_run(run["id"], status="failed", error_code="input_too_long", output=str(exc))
        return json_error(422, "input_too_long", str(exc))
    except LLMError as exc:
        await services.db.finish_run(run["id"], status="failed", error_code=exc.code, output=exc.message)
        return json_error(503, exc.code, exc.message, retryable=exc.retryable)
    except Exception:
        if name != "research":
            raise
        await services.db.finish_run(
            run["id"],
            status="failed",
            error_code="research_failed",
            output="Research could not be completed.",
        )
        return json_error(500, "research_failed", "Research could not be completed.")

    try:
        for source in sources:
            await services.db.add_source(run_id=run["id"], **source)
    except Exception:
        await services.db.finish_run(
            run["id"],
            status="failed",
            output=output,
            error_code="source_persistence_failed",
            arguments=final_arguments,
        )
        return json_error(500, "source_persistence_failed", "Sources could not be saved.")

    await services.db.finish_run(
        run["id"],
        status="failed" if error_code else "complete",
        output=output,
        error_code=error_code,
        arguments=final_arguments,
    )
    stored = await services.db.get_run(run["id"])
    return JSONResponse(stored or {})


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> JSONResponse:
    run = await _state(request).db.get_run(run_id)
    if run is None:
        return json_error(404, "not_found", "That run does not exist.")
    return JSONResponse(run)


@router.get("/jobs")
async def list_jobs(request: Request) -> dict[str, Any]:
    scheduler = _state(request).scheduler
    cursor = await _state(request).db._get_db()
    row = await (
        await cursor.execute("SELECT value FROM bot_config WHERE key = 'legacy_job_report'")
    ).fetchone()
    report_raw = str(row[0]) if row and row[0] else ""
    try:
        report = json.loads(report_raw) if report_raw else []
    except json.JSONDecodeError:
        report = []
    pending = scheduler.legacy_jobs_pending or any(item.get("status") in {"disabled", "unsupported"} for item in report)
    return {
        "items": scheduler.list_jobs() if scheduler.available else [],
        "scheduler_available": scheduler.available,
        "legacy_jobs_pending": pending,
        "unavailable_reason": scheduler.unavailable_reason,
    }


@router.get("/jobs/{job_id}/runs")
async def job_runs(request: Request, job_id: str, limit: int = 50, before: str | None = None) -> dict[str, Any]:
    return await _state(request).db.list_runs_for_job(job_id, limit=min(limit, 100), before=before)


@router.post("/jobs")
async def create_job(request: Request, body: JobCreate) -> JSONResponse:
    if body.schedule.type != "daily":
        return json_error(422, "invalid_schedule", "Only a daily time is supported.")
    scheduler = _state(request).scheduler
    try:
        job = await scheduler.add_daily_job(
            name=body.name,
            tool_name=body.tool_name,
            arguments=body.arguments,
            time_of_day=body.schedule.time,
            timezone=body.schedule.timezone,
        )
    except SchedulerUnavailable as exc:
        code = "scheduler_unavailable" if not scheduler.available else "invalid_job"
        status = 503 if not scheduler.available else 422
        return json_error(status, code, str(exc) or "The job could not be created.")
    return JSONResponse(job)


@router.patch("/jobs/{job_id}")
async def patch_job(request: Request, job_id: str, body: JobPatch) -> JSONResponse:
    scheduler = _state(request).scheduler
    try:
        job = scheduler.pause_job(job_id) if body.paused else scheduler.resume_job(job_id)
    except SchedulerUnavailable:
        return json_error(404, "not_found", "That job does not exist.")
    except Exception:
        return json_error(404, "not_found", "That job does not exist.")
    return JSONResponse(job)


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> Response:
    scheduler = _state(request).scheduler
    try:
        scheduler.remove_job(job_id)
    except Exception:
        return json_error(404, "not_found", "That job does not exist.")
    return Response(status_code=204)


async def _execute_manual(services: Any, name: str, arguments: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None, dict[str, Any] | None]:
    if name == "get_morning_briefing":
        if arguments:
            return "This tool does not take arguments.", [], "invalid_arguments", None
        import tools.morning_brief

        text = await tools.morning_brief.get_morning_briefing()
        return str(text), [], None, None
    if name == "list_active_automations":
        jobs = services.scheduler.list_jobs()
        if not jobs:
            return "No active automations scheduled.", [], None, None
        lines = [f"{job['id']} next={job['next_run_at'] or 'paused'}" for job in jobs]
        return "\n".join(lines), [], None, None
    if name == "recall":
        result = await recall(
            services.db,
            services.palace,
            query=str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 20),
        )
        return result["output"], result["sources"], None, None
    if name == "ask_notes":
        question = str(arguments.get("question") or "").strip()
        if not question:
            return "A question is required.", [], "invalid_arguments", None
        result = await ask_notes(
            services.db,
            services.palace,
            services.llm,
            question=question,
            thread_id=arguments.get("thread_id"),
            limit=int(arguments.get("limit") or 8),
        )
        return result["output"], result["sources"], None, None
    if name == "research":
        question = str(arguments.get("question") or "").strip()
        if not question:
            return "A question is required.", [], "invalid_arguments", None
        mode = str(arguments.get("mode") or "closed").strip()
        if mode != "closed":
            return (
                f"Research mode '{mode}' is not implemented (wiki/web not implemented). Mutiny research currently supports 'closed' mode only.",
                [],
                "unsupported_mode",
                None,
            )
        try:
            limit = int(arguments.get("limit") or 8)
        except (ValueError, TypeError):
            limit = 8
        result = await run_research(
            services.db,
            services.palace,
            services.llm,
            question=question,
            mode=mode,
            limit=limit,
        )
        arguments_payload = {
            "question": result["question"],
            "mode": result["mode"],
            "queries": result["queries"],
            "gaps": result["gaps"],
            "model": result["model"],
            "writer": result["writer"],
        }
        return result["answer"], result["sources"], None, arguments_payload
    return "That tool is not available.", [], "tool_not_allowed", None


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(arguments)
    except TypeError:
        return {}
    return arguments
