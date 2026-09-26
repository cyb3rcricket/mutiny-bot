"""Closed-mode research pipeline for local facts and memories."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx

import config
from database.db import DatabaseManager
from database.migrations import utc_now
from llm.llm_handler import LLMHandler
from memory.palace import PalaceAdapter
from memory.service import recall
from tools.registry import ToolPolicy, register_ai_tool

logger = logging.getLogger("mutiny_bot.research")

_SEARXNG_HTTPX_URL_PATTERN = re.compile(
    r"(?i)\bhttps?://(?:[^@\s\"']+@)?(?:localhost|127\.0\.0\.1|\[?::1\]?)(?::\d+)?/[^\s\"']+"
)


class _SearxngHttpxLogFilter(logging.Filter):
    """Keep HTTPX request logs from exposing a SearxNG query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        for match in _SEARXNG_HTTPX_URL_PATTERN.finditer(message):
            try:
                parsed = urlparse(match.group(0))
            except ValueError:
                continue
            if not parsed.path.rstrip("/").endswith("/search"):
                continue
            if any(key == "q" for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
                return False
        return True


def _install_searxng_httpx_log_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(item, _SearxngHttpxLogFilter) for item in httpx_logger.filters):
        httpx_logger.addFilter(_SearxngHttpxLogFilter())


class ResearchError(ValueError):
    """Raised when a research run cannot proceed due to config, modes, or backend issues."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


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


_WEB_CITATION_PATTERN = re.compile(r"\[(-?\d+)\]")
_WEB_URL_MARKER = "\ue000mutiny-web-url\ue001"
_WEB_ALLOWED_URL_PREFIX = "\ue000mutiny-web-allowed-"
_WEB_HTTP_START = re.compile(r"(?i)https?://")
_WEB_OTHER_SCHEME_START = re.compile(
    r"(?i)(?<![\w])(?:[a-z][a-z0-9+.-]*:(?://|[^\s<>\[\]\"'`()]+)|//)"
)
_MAX_WEB_EXCERPT_CHARS = 2000


def _cap_web_excerpt(value: Any) -> str:
    return str(value or "").strip()[:_MAX_WEB_EXCERPT_CHARS].rstrip()


def _scan_web_token(text: str, start: int) -> tuple[str, int]:
    """Scan a URL-like token, retaining balanced and escaped parentheses."""
    index = start
    depth = 0
    escaped = False
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if character in " \t\r\n<>[]\"'`":
            break
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                break
            depth -= 1
        index += 1
    return text[start:index], index


def _split_web_trailing_punctuation(token: str) -> tuple[str, str]:
    trailing = ""
    while token and token[-1] in ".,;:!?":
        trailing = token[-1] + trailing
        token = token[:-1]
    return token, trailing


def _sanitize_web_answer(answer: Any, sources: list[dict[str, Any]]) -> str:
    """Remove untrusted URLs and out-of-range source references from web output.

    Keeps URLs that exactly match a source.external_url on THIS run; strips all other URLs.
    Drops [n] citations outside 1..len(sources).
    """
    valid_urls = set()
    for source in sources:
        external_url = str(source.get("external_url") or "").strip()
        try:
            parsed = urlparse(external_url)
        except ValueError:
            continue
        if (
            external_url
            and not any(character.isspace() for character in external_url)
            and parsed.scheme.lower() in {"http", "https"}
            and parsed.netloc
        ):
            valid_urls.add(external_url)

    def sanitize_text(value: Any) -> str:
        text = str(value or "")

        allowed_markers = {
            url: f"{_WEB_ALLOWED_URL_PREFIX}{index}\ue001"
            for index, url in enumerate(sorted(valid_urls))
        }
        marker = re.escape(_WEB_URL_MARKER)
        allowed_prefix = re.escape(_WEB_ALLOWED_URL_PREFIX)

        def replace_http_urls(source_text: str) -> str:
            pieces: list[str] = []
            cursor = 0
            for match in _WEB_HTTP_START.finditer(source_text):
                if match.start() < cursor:
                    continue
                pieces.append(source_text[cursor : match.start()])
                token, end = _scan_web_token(source_text, match.start())
                bare_token, trailing = _split_web_trailing_punctuation(token)
                normalized_token = token.replace(r"\(", "(").replace(r"\)", ")")
                normalized_bare_token = bare_token.replace(r"\(", "(").replace(r"\)", ")")
                if token in allowed_markers:
                    pieces.append(allowed_markers[token])
                elif normalized_token in allowed_markers:
                    pieces.append(allowed_markers[normalized_token])
                elif bare_token in allowed_markers:
                    pieces.append(allowed_markers[bare_token] + trailing)
                elif normalized_bare_token in allowed_markers:
                    pieces.append(allowed_markers[normalized_bare_token] + trailing)
                else:
                    pieces.append(_WEB_URL_MARKER + trailing)
                cursor = end
            pieces.append(source_text[cursor:])
            return "".join(pieces)

        def replace_other_schemes(source_text: str) -> str:
            pieces: list[str] = []
            cursor = 0
            for match in _WEB_OTHER_SCHEME_START.finditer(source_text):
                if match.start() < cursor:
                    continue
                pieces.append(source_text[cursor : match.start()])
                token, end = _scan_web_token(source_text, match.start())
                _bare_token, trailing = _split_web_trailing_punctuation(token)
                pieces.append(_WEB_URL_MARKER + trailing)
                cursor = end
            pieces.append(source_text[cursor:])
            return "".join(pieces)

        text = replace_http_urls(text)
        text = replace_other_schemes(text)
        text = re.sub(
            rf"\[([^\]]*)\]\(\s*[<\"'`]?{marker}([.,;:!?])?[>\"'`]?\s*\)",
            r"\1\2",
            text,
        )
        text = re.sub(
            rf"\[([^\]]*)\]\(\s*(?![<\"'`]?{allowed_prefix})[^)]*\)",
            r"\1",
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
        text = text.replace(_WEB_URL_MARKER, "")
        for url, allowed_marker in allowed_markers.items():
            text = text.replace(allowed_marker, url)

        source_count = len(sources)

        def keep_citation(match: re.Match[str]) -> str:
            try:
                reference = int(match.group(1))
            except (TypeError, ValueError):
                return ""
            return match.group(0) if 1 <= reference <= source_count else ""

        return _WEB_CITATION_PATTERN.sub(keep_citation, text)

    def has_substantive_text(text: str) -> bool:
        without_citations = _WEB_CITATION_PATTERN.sub("", text)
        return any(character.isalnum() for character in without_citations)

    sanitized = sanitize_text(answer).strip()
    if has_substantive_text(sanitized):
        return sanitized

    for source in sources:
        excerpt = sanitize_text(source.get("excerpt", "")).strip()
        if has_substantive_text(excerpt):
            return excerpt[:_MAX_WEB_EXCERPT_CHARS].rstrip()
    return "No usable retrieved excerpt remains."


async def search_web(
    queries: list[str],
    *,
    mode: str = "web",
    limit: int = 8,
    searxng_url: str | None = None,
    executed_queries: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Query local SearxNG on loopback for live web snippets.

    Two locks required: OUTBOUND_ENABLED must be True and caller mode must be 'web'.
    """
    if not getattr(config, "OUTBOUND_ENABLED", False):
        raise ResearchError("outbound_disabled", "Outbound network access is disabled.")
    if mode != "web":
        raise ResearchError("invalid_mode", f"Web search requires mode='web', got '{mode}'.")

    target_url = (searxng_url or getattr(config, "SEARXNG_URL", "http://127.0.0.1:8080")).strip()
    endpoint_error = config.searxng_endpoint_error(target_url)
    if endpoint_error:
        raise ResearchError("invalid_searxng_url", endpoint_error)

    seen_urls: set[str] = set()
    sources: list[dict[str, Any]] = []
    max_sources = max(0, int(limit))
    if max_sources == 0:
        return []

    for q in queries:
        clean_q = q.strip()
        if not clean_q:
            continue
        if executed_queries is not None:
            executed_queries.append(clean_q)
        try:
            _install_searxng_httpx_log_filter()
            async with httpx.AsyncClient(timeout=10.0, trust_env=False, proxy=None) as client:
                response = await client.get(
                    f"{target_url.rstrip('/')}/search",
                    params={"q": clean_q, "format": "json"},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            logger.warning("SearxNG request failed (%s)", type(exc).__name__)
            raise ResearchError("searxng_unavailable", "SearxNG search failed.")

        results = data.get("results", []) if isinstance(data, dict) else []
        now = utc_now()
        for hit in results:
            if not isinstance(hit, dict):
                continue
            hit_url = str(hit.get("url") or "").strip()
            if not hit_url:
                continue
            try:
                parsed = urlparse(hit_url)
            except ValueError:
                continue
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or any(character.isspace() for character in hit_url)
            ):
                continue
            title = str(hit.get("title") or "").strip() or hit_url
            raw_snippet = hit.get("content") or hit.get("snippet") or ""
            snippet = _cap_web_excerpt(raw_snippet)
            if not snippet:
                continue
            if hit_url in seen_urls:
                continue
            seen_urls.add(hit_url)

            sources.append(
                {
                    "kind": "web",
                    "title": title,
                    "excerpt": snippet,
                    "record_id": hit_url,
                    "external_url": hit_url,
                    "retrieved_at": now,
                }
            )
            if len(sources) >= max_sources:
                break
        if len(sources) >= max_sources:
            break

    return sources


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
    searxng_url: str | None = None,
) -> dict[str, Any]:
    """Execute a research run over local facts and memories ('closed') or web snippets ('web')."""
    if mode == "wiki":
        raise ValueError(
            "Research mode 'wiki' is not implemented. Mutiny research currently supports 'closed' and 'web' modes only."
        )
    if mode not in {"closed", "web"}:
        raise ValueError(
            f"Research mode '{mode}' is not implemented. Mutiny research currently supports 'closed' and 'web' modes only."
        )

    resolved_model = (
        model.strip() if isinstance(model, str) and model.strip() else await db.get_current_model()
    )

    clean_question = question.strip() if isinstance(question, str) else ""

    if mode == "web":
        if not getattr(config, "OUTBOUND_ENABLED", False):
            raise ResearchError("outbound_disabled", "Outbound network access is disabled.")

        if not clean_question:
            return {
                "answer": "No relevant web snippets were found matching your question.",
                "sources": [],
                "queries": [],
                "gaps": ["not in retrieved notes"],
                "model": resolved_model,
                "writer": "local",
                "mode": "web",
                "question": question,
            }

        # Query rewrite with local LLM, tools=None
        query_candidates = [clean_question]
        try:
            rewrite_prompt = (
                "Given the user's research question, provide 1 to 3 short web search phrases "
                "suitable for querying the web.\n"
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

        query_candidates = [q.strip() for q in query_candidates if q.strip()]
        executed_queries: list[str] = []
        sources = await search_web(
            query_candidates,
            mode=mode,
            limit=limit,
            searxng_url=searxng_url,
            executed_queries=executed_queries,
        )

        if not sources:
            return {
                "answer": "No relevant web snippets were found matching your question.",
                "sources": [],
                "queries": executed_queries,
                "gaps": ["not in retrieved notes"],
                "model": resolved_model,
                "writer": "local",
                "mode": "web",
                "question": question,
            }

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
        answer = _sanitize_web_answer(answer, sources)

        gaps = ["not in retrieved notes"] if _detect_unknown_in_answer(answer) else []

        return {
            "answer": answer,
            "sources": sources,
            "queries": executed_queries,
            "gaps": gaps,
            "model": resolved_model,
            "writer": "local",
            "mode": "web",
            "question": question,
        }

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
