"""Closed-mode research pipeline for local facts and memories."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any

import config
from database.db import DatabaseManager
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter
from memory.service import recall
from tools.registry import ToolPolicy, register_ai_tool

logger = logging.getLogger("mutiny_bot.research")


def parse_markdown_document(file_path: Path, base_dir: Path) -> list[dict[str, Any]]:
    """Parse a markdown file into titled excerpt sections split on headings."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    try:
        rel_path = file_path.relative_to(base_dir).as_posix()
    except Exception:
        rel_path = file_path.name

    lines = text.splitlines()
    sections: list[dict[str, Any]] = []
    current_heading = file_path.stem
    current_lines: list[str] = []
    seen_headings: dict[str, int] = {}

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            content = "\n".join(current_lines).strip()
            if content:
                count = seen_headings.get(current_heading, 0)
                seen_headings[current_heading] = count + 1
                heading_slug = current_heading if count == 0 else f"{current_heading}-{count}"
                sections.append(
                    {
                        "kind": "document",
                        "title": current_heading,
                        "excerpt": content,
                        "record_id": f"{rel_path}#{heading_slug}",
                        "external_url": None,
                    }
                )
            current_heading = match.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)

    content = "\n".join(current_lines).strip()
    if content:
        count = seen_headings.get(current_heading, 0)
        seen_headings[current_heading] = count + 1
        heading_slug = current_heading if count == 0 else f"{current_heading}-{count}"
        sections.append(
            {
                "kind": "document",
                "title": current_heading,
                "excerpt": content,
                "record_id": f"{rel_path}#{heading_slug}",
                "external_url": None,
            }
        )
    return sections


def parse_pdf_document(file_path: Path, base_dir: Path) -> list[dict[str, Any]]:
    """Extract text from a PDF file if an extraction library is available."""
    try:
        rel_path = file_path.relative_to(base_dir).as_posix()
    except Exception:
        rel_path = file_path.name

    text = ""
    # Try pypdf
    try:
        import pypdf

        reader = pypdf.PdfReader(str(file_path))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages_text).strip()
    except Exception:
        # Try pymupdf / fitz
        try:
            import fitz

            doc = fitz.open(str(file_path))
            pages_text = [page.get_text() for page in doc]
            text = "\n\n".join(pages_text).strip()
        except Exception:
            return []

    if not text:
        return []

    sections: list[dict[str, Any]] = []
    if re.search(r"^(#{1,6})\s+(.+)$", text, re.M):
        lines = text.splitlines()
        current_heading = file_path.stem
        current_lines: list[str] = []
        seen_headings: dict[str, int] = {}
        for line in lines:
            match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if match:
                content = "\n".join(current_lines).strip()
                if content:
                    count = seen_headings.get(current_heading, 0)
                    seen_headings[current_heading] = count + 1
                    heading_slug = current_heading if count == 0 else f"{current_heading}-{count}"
                    sections.append(
                        {
                            "kind": "document",
                            "title": current_heading,
                            "excerpt": content,
                            "record_id": f"{rel_path}#{heading_slug}",
                            "external_url": None,
                        }
                    )
                current_heading = match.group(2).strip()
                current_lines = []
            else:
                current_lines.append(line)
        content = "\n".join(current_lines).strip()
        if content:
            count = seen_headings.get(current_heading, 0)
            seen_headings[current_heading] = count + 1
            heading_slug = current_heading if count == 0 else f"{current_heading}-{count}"
            sections.append(
                {
                    "kind": "document",
                    "title": current_heading,
                    "excerpt": content,
                    "record_id": f"{rel_path}#{heading_slug}",
                    "external_url": None,
                }
            )
    else:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [text.strip()]
        for idx, para in enumerate(paragraphs, start=1):
            sections.append(
                {
                    "kind": "document",
                    "title": f"{file_path.stem} (part {idx})" if len(paragraphs) > 1 else file_path.stem,
                    "excerpt": para,
                    "record_id": f"{rel_path}#part-{idx}",
                    "external_url": None,
                }
            )
    return sections


def load_documents(docs_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and index all .md and .pdf files in docs_path split on headings."""
    path_val = docs_path if docs_path is not None else getattr(config, "DOCS_PATH", "./research_docs")
    base_path = Path(os.path.expanduser(str(path_val)))
    if not base_path.is_dir():
        return []

    documents: list[dict[str, Any]] = []
    try:
        for doc_file in sorted(base_path.rglob("*")):
            if not doc_file.is_file():
                continue
            suffix = doc_file.suffix.lower()
            if suffix == ".md":
                documents.extend(parse_markdown_document(doc_file, base_path))
            elif suffix == ".pdf":
                documents.extend(parse_pdf_document(doc_file, base_path))
    except Exception:
        logger.debug("Error scanning documents in %s", base_path, exc_info=True)
    return documents


def keyword_match_documents(
    documents: list[dict[str, Any]], query: str, limit: int = 8
) -> list[dict[str, Any]]:
    """Match indexed document sections by keyword terms (terms > 2 chars)."""
    terms = [part for part in query.lower().split() if len(part) > 2]
    if not terms:
        return []
    matched = [
        doc
        for doc in documents
        if any(term in doc["excerpt"].lower() or term in doc["title"].lower() for term in terms)
    ]
    return matched[:limit]


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


_CLOSED_URL_PATTERN = re.compile(r"(?i)https?://[^\s<>\[\]\"'`()]*")
_CLOSED_CITATION_PATTERN = re.compile(r"\[(-?\d+)\]")
_CLOSED_URL_MARKER = "\ue000mutiny-url\ue001"


def _sanitize_closed_answer(answer: Any, sources: list[dict[str, Any]]) -> str:
    """Remove untrusted URLs and out-of-range source references from closed output."""

    def sanitize_text(value: Any) -> str:
        text = str(value or "")

        def mark_url(match: re.Match[str]) -> str:
            token = match.group(0)
            trailing = ""
            while token and token[-1] in ".,;:!?":
                trailing = token[-1] + trailing
                token = token[:-1]
            return _CLOSED_URL_MARKER + trailing

        text = _CLOSED_URL_PATTERN.sub(mark_url, text)
        marker = re.escape(_CLOSED_URL_MARKER)
        text = re.sub(
            rf"\[([^\]]*)\]\(\s*[<\"'`]?{marker}([.,;:!?])?[>\"'`]?\s*\)",
            r"\1\2",
            text,
        )
        wrappers = (
            (r"<", r">"),
            (r"\(", r"\)"),
            (r"\[", r"\]"),
            ('"', '"'),
            ("'", "'"),
            ("`", "`"),
        )
        for _ in range(2):
            for opening, closing in wrappers:
                text = re.sub(
                    rf"{opening}\s*{marker}\s*([.,;:!?])?\s*{closing}",
                    r"\1",
                    text,
                )
        text = text.replace(_CLOSED_URL_MARKER, "")

        source_count = len(sources)

        def keep_citation(match: re.Match[str]) -> str:
            try:
                reference = int(match.group(1))
            except (TypeError, ValueError):
                return ""
            return match.group(0) if 1 <= reference <= source_count else ""

        return _CLOSED_CITATION_PATTERN.sub(keep_citation, text)

    def has_substantive_text(text: str) -> bool:
        without_citations = _CLOSED_CITATION_PATTERN.sub("", text)
        return any(character.isalnum() for character in without_citations)

    sanitized = sanitize_text(answer).strip()
    if has_substantive_text(sanitized):
        return sanitized

    for source in sources:
        excerpt = sanitize_text(source.get("excerpt", "")).strip()
        if has_substantive_text(excerpt):
            return excerpt[:500].rstrip()
    return "No usable retrieved excerpt remains."


async def run_research(
    db: DatabaseManager | Any,
    palace: PalaceAdapter | Any,
    llm: LLMHandler | Any,
    *,
    question: str,
    mode: str = "closed",
    model: str | None = None,
    limit: int = 8,
    docs_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute a closed-corpus research run over local saved facts, memories, and documents."""
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
    query_candidates = [clean_question]
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
            query_candidates = parsed
    except Exception:
        logger.debug("Research query rewrite failed; falling back to original question.", exc_info=True)
        query_candidates = [clean_question]

    # Index local documents on each run (cheap)
    indexed_docs = load_documents(docs_path)

    # Retrieve with the existing recall() path + document index for each query (deduped by record_id + excerpt)
    seen_keys: set[tuple[str | None, str]] = set()
    sources: list[dict[str, Any]] = []
    executed_queries: list[str] = []
    max_sources = max(0, int(limit))

    for q in query_candidates:
        q_clean = q.strip()
        if not q_clean:
            continue

        # Check if query has terms of length > 2 (the threshold memory.service uses for facts)
        has_terms = any(len(part) > 2 for part in q_clean.lower().split())

        recalled = await recall(db, palace, query=q_clean, limit=max_sources)
        raw_sources = recalled.get("sources", []) if isinstance(recalled, dict) else []
        doc_sources = keyword_match_documents(indexed_docs, q_clean, limit=max_sources) if has_terms else []
        executed_queries.append(q_clean)

        combined_sources = list(raw_sources) + doc_sources
        for src in combined_sources:
            if not isinstance(src, dict):
                continue
            kind = src.get("kind")
            if kind not in {"fact", "memory", "document"}:
                continue
            # If query had no terms > 2, recall() returned all facts by default (not matched by keyword)
            if kind == "fact" and not has_terms:
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

            fallback_title = "Saved fact" if kind == "fact" else ("Palace" if kind == "memory" else "Document")
            sources.append(
                {
                    "kind": kind,
                    "title": str(src.get("title") or fallback_title),
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
            "queries": executed_queries,
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
    answer = _sanitize_closed_answer(answer, sources)

    gaps = ["not in retrieved notes"] if _detect_unknown_in_answer(answer) else []

    return {
        "answer": answer,
        "sources": sources,
        "queries": executed_queries,
        "gaps": gaps,
        "model": resolved_model,
        "writer": "local",
        "mode": "closed",
        "question": question,
    }


RESEARCH_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "mode": {"type": "string", "default": "closed"},
        "limit": {"type": "integer"},
    },
    "required": ["question"],
}

register_ai_tool(
    name="research",
    description="Execute closed-corpus research over local facts and memories",
    parameters=RESEARCH_TOOL_PARAMETERS,
    func=run_research,
    policy=ToolPolicy(manual=True, network=False, schedulable=False),
)
