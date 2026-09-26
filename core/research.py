"""Closed-mode research pipeline for local facts and memories."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter
from memory.service import recall

logger = logging.getLogger("mutiny_bot.research")

_UNKNOWN_PATTERNS = [
    re.compile(r"\bmissing\b", re.I),
    re.compile(r"\bnot\s+(?:found\s+)?in\s+(?:the\s+|retrieved\s+|provided\s+|any\s+)?notes\b", re.I),
    re.compile(
        r"\bnotes?\s+(?:do\s+not|does\s+not|don't|doesn't)\s+(?:contain|mention|state|provide|specify|include|have|say|indicate|cover|address|give)\b",
        re.I,
    ),
    re.compile(
        r"\bnotes?\s+(?:contain|contains|provide|provides|have|has)\s+no\s+(?:information|mention|details?|record)\b",
        re.I,
    ),
    re.compile(r"\bnotes?\s+lack\b", re.I),
    re.compile(
        r"\b(?:do\s+not|does\s+not|don't|doesn't)\s+contain\s+(?:the\s+answer|any\s+information|information)\b",
        re.I,
    ),
    re.compile(r"\b(?:no|insufficient|not\s+enough)\s+information\b", re.I),
    re.compile(r"\bno\s+mention\b", re.I),
    re.compile(r"\bnot\s+(?:mentioned|stated|provided|specified|included|covered|given)\b", re.I),
    re.compile(r"\bcan(?:not|\s+not|'t)\s+(?:be\s+answered|answer|be\s+determined|determine|tell)\b", re.I),
    re.compile(r"\bcan(?:not|\s+not|'t)\s+find\b", re.I),
    re.compile(r"\bcould(?:not|\s+not|n't)\s+find\b", re.I),
    re.compile(r"\b(?:do\s+not|does\s+not|don't|doesn't)\s+know\b", re.I),
    re.compile(r"\bunknown\b", re.I),
    re.compile(r"\bnot\s+in\s+retrieved\s+notes\b", re.I),
]


def _parse_rewrite_queries(raw: str) -> list[str] | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None

    # Strip markdown code block if present
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except Exception:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
            except Exception:
                return None
        else:
            return None

    if isinstance(data, list):
        phrases: list[str] = []
        seen: set[str] = set()
        for x in data:
            if isinstance(x, (str, int, float)):
                val = str(x).strip().strip("'\"").strip()
                if val and val.lower() not in seen:
                    seen.add(val.lower())
                    phrases.append(val)
        if phrases:
            return phrases[:3]
    return None


def _detect_unknown_in_answer(answer: str) -> bool:
    return any(p.search(answer) for p in _UNKNOWN_PATTERNS)


async def run_research(
    db: DatabaseManager | Any,
    palace: PalaceAdapter | Any,
    llm: LLMHandler | Any,
    *,
    question: str,
    mode: str = "closed",
    model: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Execute a closed-corpus research run over local saved facts and memories."""
    if mode != "closed":
        raise ValueError(
            f"Research mode '{mode}' is not implemented (wiki/web not implemented). Mutiny research currently supports 'closed' mode only."
        )

    resolved_model = (
        model.strip() if isinstance(model, str) and model.strip() else await db.get_current_model()
    )

    clean_question = question.strip() if isinstance(question, str) else ""
    if not clean_question:
        return {
            "answer": "No relevant notes or memories were found matching your question.",
            "sources": [],
            "queries": [],
            "gaps": ["not in retrieved notes"],
            "model": resolved_model,
            "writer": "local",
            "mode": "closed",
            "question": question,
        }

    # Query rewrite (optional best-effort)
    queries = [clean_question]
    try:
        rewrite_prompt = (
            "Given the user's research question, provide 1 to 3 short keyword search phrases "
            "suitable for querying a personal knowledge base.\n"
            "Respond ONLY with a JSON array of strings, for example: [\"phrase 1\", \"phrase 2\"]. "
            "Do not include any explanation or extra text."
        )
        raw_rewrite = await llm.generate_response(
            model=resolved_model,
            messages=[
                {"role": "system", "content": rewrite_prompt},
                {"role": "user", "content": clean_question},
            ],
            tools=None,
        )
        parsed = _parse_rewrite_queries(raw_rewrite)
        if parsed:
            queries = parsed
    except Exception:
        logger.debug("Research query rewrite failed; falling back to original question.", exc_info=True)
        queries = [clean_question]

    # Retrieve with the existing recall() path for each query (deduped by record_id + excerpt)
    seen_keys: set[tuple[str | None, str]] = set()
    sources: list[dict[str, Any]] = []
    max_sources = max(0, int(limit))

    for q in queries:
        q_clean = q.strip()
        if not q_clean:
            continue

        # Check if query has terms of length > 2 (the threshold memory.service uses for facts)
        has_fact_terms = any(len(part) > 2 for part in q_clean.lower().split())

        recalled = await recall(db, palace, query=q_clean, limit=max_sources)
        raw_sources = recalled.get("sources", []) if isinstance(recalled, dict) else []
        for src in raw_sources:
            if not isinstance(src, dict):
                continue
            kind = src.get("kind")
            if kind not in {"fact", "memory"}:
                continue
            # If query had no terms > 2, recall() returned all facts by default (not matched by keyword)
            if kind == "fact" and not has_fact_terms:
                continue

            record_id = src.get("record_id")
            rec_id_str = str(record_id) if record_id is not None else None
            raw_excerpt = src.get("excerpt")
            excerpt = str(raw_excerpt).strip() if raw_excerpt is not None else ""
            if not excerpt:
                continue

            dedupe_key = (rec_id_str, excerpt)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            sources.append(
                {
                    "kind": kind,
                    "title": str(src.get("title") or ("Saved fact" if kind == "fact" else "Palace")),
                    "excerpt": excerpt,
                    "record_id": rec_id_str,
                    "external_url": None,
                }
            )
            if len(sources) >= max_sources:
                break
        if len(sources) >= max_sources:
            break

    # If zero sources: answer must state nothing was found; gaps must be non-empty; do not call writer
    if not sources:
        return {
            "answer": "No relevant notes or memories were found matching your question.",
            "sources": [],
            "queries": queries,
            "gaps": ["not in retrieved notes"],
            "model": resolved_model,
            "writer": "local",
            "mode": "closed",
            "question": question,
        }

    # Writer prompt
    numbered_notes = "\n".join(f"[{idx}] {src['excerpt']}" for idx, src in enumerate(sources, start=1))
    system_prompt = (
        "Answer only from the numbered notes below. If missing, say so. Do not invent sources or URLs."
    )
    user_prompt = f"Notes:\n{numbered_notes}\n\nQuestion: {question}"

    answer = await llm.generate_response(
        model=resolved_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=None,
    )

    gaps = ["not in retrieved notes"] if _detect_unknown_in_answer(answer) else []

    return {
        "answer": answer,
        "sources": sources,
        "queries": queries,
        "gaps": gaps,
        "model": resolved_model,
        "writer": "local",
        "mode": "closed",
        "question": question,
    }
