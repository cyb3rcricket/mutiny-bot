"""SQLite facts are authoritative. Palace indexing is best-effort."""

from __future__ import annotations

import asyncio
from typing import Any

from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter


def _keyword_facts(facts: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    terms = [part for part in query.lower().split() if len(part) > 2]
    if not terms:
        return facts[:limit]
    matched = [fact for fact in facts if any(term in fact["content"].lower() for term in terms)]
    return matched[:limit]


async def remember(
    db: DatabaseManager,
    palace: PalaceAdapter,
    *,
    content: str,
    request_id: str,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    existing = await db.get_fact_by_key(request_id)
    if existing:
        return _public_fact(existing)
    saved = await db.save_fact(
        content,
        source_message_id=source_message_id,
        idempotency_key=request_id,
        palace_status="pending",
    )
    status = await asyncio.to_thread(palace.index_fact, str(saved["id"]), saved["content"])
    reference = f"fact:{saved['id']}" if status == "indexed" else None
    await db.set_fact_palace_status(int(saved["id"]), status, reference)
    saved["palace_status"] = status
    saved["palace_reference"] = reference
    return _public_fact(saved)


async def recall(
    db: DatabaseManager,
    palace: PalaceAdapter,
    *,
    query: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    listed = await db.list_facts(limit=200)
    facts = _keyword_facts(listed["items"], query, limit)
    palace_hits = []
    if query and palace.available:
        palace_hits = await asyncio.to_thread(palace.search, query, limit=limit)
    sources = [_fact_source(fact) for fact in facts]
    sources.extend(_hit_source(hit) for hit in palace_hits)
    lines = [fact["content"] for fact in facts]
    lines.extend(hit.text for hit in palace_hits)
    if not lines:
        text = "No saved facts matched."
    else:
        text = "\n".join(f"- {line}" for line in lines)
    if query and not palace.available:
        text = "Palace retrieval is unavailable. Showing saved facts only.\n" + text
    return {"output": text, "sources": sources, "palace_available": palace.available}


async def ask_notes(
    db: DatabaseManager,
    palace: PalaceAdapter,
    llm: LLMHandler,
    *,
    question: str,
    thread_id: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    recalled = await recall(db, palace, query=question, limit=limit)
    context_parts = [recalled["output"]]
    sources = list(recalled["sources"])
    if thread_id:
        history = await db.messages_for_context(thread_id, limit=limit)
        for item in history:
            excerpt = str(item["content"])[:500]
            if not excerpt:
                continue
            context_parts.append(f"{item['role']}: {excerpt}")
            sources.append(
                {
                    "kind": "message",
                    "title": item["role"],
                    "excerpt": excerpt,
                    "record_id": None,
                    "external_url": None,
                }
            )
    model = await db.get_current_model()
    answer = await llm.generate_response(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Answer only from the notes below. If they do not contain the answer, say so. "
                    "Do not invent sources."
                ),
            },
            {"role": "user", "content": f"Notes:\n{chr(10).join(context_parts)}\n\nQuestion: {question}"},
        ],
        tools=None,
    )
    return {"output": answer, "sources": sources}


def _public_fact(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fact["id"],
        "content": fact["content"],
        "created_at": fact["created_at"],
        "palace_status": fact.get("palace_status") or "unavailable",
    }


def _fact_source(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "fact",
        "title": "Saved fact",
        "excerpt": fact["content"],
        "record_id": str(fact["id"]),
        "external_url": None,
    }


def _hit_source(hit: Any) -> dict[str, Any]:
    return {
        "kind": "memory",
        "title": hit.title or "Palace",
        "excerpt": hit.text,
        "record_id": hit.record_id,
        "external_url": None,
    }
