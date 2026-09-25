"""One conversation turn. Inference stays out of the HTTP routes."""

from __future__ import annotations

from typing import Any

from database.db import DatabaseManager, InputTooLong
from llm.llm_handler import LLMError, LLMHandler


class ChatConflict(Exception):
    pass


class ChatNotFound(Exception):
    pass


async def _message_view(db: DatabaseManager, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row.get("timestamp") or row.get("created_at"),
        "model": row.get("model"),
        "status": row.get("status") or "complete",
        "request_id": row.get("request_id"),
        "sources": await db.list_sources(message_id=row["id"]),
    }


async def send_message(
    db: DatabaseManager,
    llm: LLMHandler,
    *,
    thread_id: str,
    request_id: str,
    content: str,
) -> dict[str, Any]:
    thread = await db.get_thread(thread_id)
    if thread is None:
        raise ChatNotFound()
    request_id = request_id.strip()
    if not request_id:
        raise ValueError("request_id is required.")

    existing_user = await db.get_message_by_request(thread_id, request_id, "user")
    existing_assistant = await db.get_message_by_request(thread_id, request_id, "assistant")
    if existing_user and existing_assistant:
        return {
            "user_message": await _message_view(db, existing_user),
            "assistant_message": await _message_view(db, existing_assistant),
        }
    if await db.has_active_turn(thread_id):
        raise ChatConflict()

    model = await db.get_current_model()
    user = await db.insert_message(
        thread_id,
        "user",
        content,
        request_id=request_id,
        status="complete",
    )
    assistant = await db.insert_message(
        thread_id,
        "assistant",
        "",
        model=model,
        request_id=request_id,
        status="pending",
        enforce_limit=False,
    )
    try:
        history = await db.messages_for_context(thread_id, limit=12)
        prompt = await db.get_system_prompt()
        # Pending assistant text must not be sent back to the model as a turn.
        history = [item for item in history if item.get("content")]
        text = await llm.generate_response(
            model=model,
            messages=[{"role": "system", "content": prompt}, *history],
            tools=None,
        )
    except (LLMError, InputTooLong):
        await db.update_message_status(assistant["id"], "failed", "The local model could not complete this turn.")
        raise
    except Exception:
        await db.update_message_status(assistant["id"], "failed", "The local model could not complete this turn.")
        raise LLMError("model_unavailable", "The local model could not be reached.", retryable=True)
    await db.update_message_status(assistant["id"], "complete", text)
    stored_assistant = await db.get_message(assistant["id"])
    return {
        "user_message": await _message_view(db, user),
        "assistant_message": await _message_view(db, stored_assistant or assistant),
    }
