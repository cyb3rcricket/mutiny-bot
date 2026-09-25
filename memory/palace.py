"""Optional MemPalace boundary.

SQLite remains authoritative. This adapter never turns an arbitrary dictionary
into a source, and it refuses to initialize when the local embedding assets
Chroma would download are not already cached.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_EMBEDDING_FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
)
_WING = "local"
_ROOM = "facts"


def embedding_assets_cached() -> bool:
    folder = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "onnx"
    return all((folder / name).is_file() for name in _EMBEDDING_FILES)


@dataclass(frozen=True)
class MemoryHit:
    text: str
    record_id: str | None = None
    title: str | None = None


def normalize_search_results(payload: Any) -> list[MemoryHit]:
    """Accept a bare list or `{"results": [...]}`. Ignore non-text records."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        items = payload.get("results", [])
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    if not isinstance(items, list):
        return []

    hits: list[MemoryHit] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            record_id = None
            title = None
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content") or item.get("memory")
            if not isinstance(value, str):
                continue
            text = value.strip()
            raw_id = item.get("id") or item.get("record_id") or item.get("drawer_id")
            record_id = str(raw_id) if raw_id else None
            raw_title = item.get("title")
            title = str(raw_title) if isinstance(raw_title, str) and raw_title.strip() else None
        else:
            continue
        if text:
            hits.append(MemoryHit(text=text, record_id=record_id, title=title))
    return hits


class PalaceAdapter:
    """Explicit read/write access to an existing local palace."""

    def __init__(
        self,
        palace_path: str,
        *,
        search_fn: Callable[..., Any] | None = None,
        add_fn: Callable[..., Any] | None = None,
        assets_ready: bool | None = None,
        importer: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        self.palace_path = os.path.expanduser(palace_path)
        self._search_fn = search_fn
        self._add_fn = add_fn
        self._assets_ready = embedding_assets_cached() if assets_ready is None else assets_ready
        self._importer = importer or _import_mempalace
        self._write_lock = threading.Lock()
        self._load_error: str | None = None
        self._loaded = search_fn is not None or add_fn is not None
        if self._loaded:
            self._load_error = None

    @property
    def degraded_reason(self) -> str | None:
        if not self._assets_ready and self._search_fn is None:
            return "embedding assets are not cached locally"
        self._ensure_loaded()
        return self._load_error

    @property
    def available(self) -> bool:
        return self.degraded_reason is None

    def search(self, query: str, *, limit: int = 5, wing: str | None = None, room: str | None = None) -> list[MemoryHit]:
        if self.degraded_reason is not None or self._search_fn is None:
            return []
        try:
            payload = self._search_fn(
                query,
                palace_path=self.palace_path,
                wing=wing,
                room=room,
                n_results=limit,
            )
        except TypeError:
            payload = self._search_fn(query, self.palace_path)
        except Exception:
            return []
        if isinstance(payload, dict) and payload.get("error") and "results" not in payload:
            return []
        return normalize_search_results(payload)[:limit]

    def index_fact(self, fact_id: str, content: str) -> str:
        """Return indexed, unavailable, or failed. Does not raise for palace problems."""
        if not self._assets_ready and self._add_fn is None:
            return "unavailable"
        self._ensure_loaded()
        if self._add_fn is None:
            return "unavailable"
        with self._write_lock:
            previous = os.environ.get("MEMPALACE_PALACE_PATH")
            os.environ["MEMPALACE_PALACE_PATH"] = self.palace_path
            try:
                result = self._add_fn(
                    wing=_WING,
                    room=_ROOM,
                    content=content,
                    source_file=f"fact:{fact_id}",
                    added_by="mutiny",
                )
            except Exception:
                return "failed"
            finally:
                if previous is None:
                    os.environ.pop("MEMPALACE_PALACE_PATH", None)
                else:
                    os.environ["MEMPALACE_PALACE_PATH"] = previous
        if isinstance(result, dict) and result.get("success") is False:
            if result.get("reason") == "duplicate":
                return "indexed"
            return "failed"
        return "indexed"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._assets_ready:
            self._load_error = "embedding assets are not cached locally"
            return
        try:
            search_fn, add_fn = self._importer()
        except Exception as exc:
            self._load_error = f"import failed: {exc.__class__.__name__}"
            return
        if search_fn is None or add_fn is None:
            self._load_error = "import failed: missing mempalace callables"
            return
        self._search_fn = search_fn
        self._add_fn = add_fn
        self._load_error = None


def _import_mempalace() -> tuple[Any, Any]:
    from mempalace.mcp_server import tool_add_drawer
    from mempalace.searcher import search_memories

    return search_memories, tool_add_drawer
