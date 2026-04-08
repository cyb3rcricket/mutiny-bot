"""Central model registry and discovery helpers for local Ollama models."""

from __future__ import annotations

import subprocess
import time
from typing import List

# Canonical allowlist for this bot. Keep this as the single source of truth.
CANONICAL_MODELS: List[str] = [
    "gemma4:e4b",
    "phi4-mini:latest",
    "qwen2.5-coder:7b",
]
DEFAULT_CANONICAL_MODEL = "qwen2.5-coder:7b"

_CACHE_TTL_SECONDS = 60
_cached_at: float = 0.0
_cached_models: List[str] = CANONICAL_MODELS.copy()


def _parse_ollama_list_output(raw_output: str) -> set[str]:
    """Parse `ollama list` output into plain model names."""
    names: set[str] = set()
    for line in raw_output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("name"):
            continue
        # First token is model name in `ollama list` output.
        model_name = line.split()[0]
        if model_name:
            names.add(model_name)
    return names


def get_installed_models(force_refresh: bool = False) -> List[str]:
    """Return supported model names, filtered by currently installed Ollama models.

    Returns only models from CANONICAL_MODELS. Uses a short cache and falls back
    to the last known values if Ollama is unavailable.
    """
    global _cached_at, _cached_models

    now = time.time()
    if not force_refresh and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_models.copy()

    try:
        raw_output = subprocess.check_output(
            ["ollama", "list"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
        )
        installed = _parse_ollama_list_output(raw_output)
        filtered = [name for name in CANONICAL_MODELS if name in installed]
        # Keep bot usable even if ollama is up but list is temporarily empty.
        _cached_models = filtered or CANONICAL_MODELS.copy()
        _cached_at = now
        return _cached_models.copy()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Ollama missing/down: return cached set or canonical fallback.
        if _cached_models:
            return _cached_models.copy()
        return CANONICAL_MODELS.copy()


def get_litellm_model_ids(force_refresh: bool = False) -> List[str]:
    """Return supported models as litellm Ollama IDs (`ollama/<name>`)."""
    return [f"ollama/{name}" for name in get_installed_models(force_refresh=force_refresh)]


def get_default_litellm_model(force_refresh: bool = False) -> str:
    """Return the preferred default litellm model ID."""
    installed = get_installed_models(force_refresh=force_refresh)
    preferred = DEFAULT_CANONICAL_MODEL if DEFAULT_CANONICAL_MODEL in installed else installed[0]
    return f"ollama/{preferred}"
