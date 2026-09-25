"""Local Ollama model discovery.

Discovery runs only when a caller asks. Importing this module does not spawn
a process or choose a model.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List
from urllib.parse import urlparse

from config import OLLAMA_API_BASE, is_loopback_host

CANONICAL_MODELS: List[str] = [
    "gemma4:e4b",
    "phi4-mini:latest",
    "qwen2.5-coder:7b",
]
DEFAULT_CANONICAL_MODEL = CANONICAL_MODELS[0]

_CACHE_TTL_SECONDS = 60
_cache_lock = threading.Lock()
_cached_at: float = 0.0
_cached_host: str = ""
_cached_models: List[str] = []
_cached_status: str = "unchecked"


def _parse_ollama_list_output(raw_output: str) -> set[str]:
    """Parse `ollama list` output into plain model names."""
    names: set[str] = set()
    for line in raw_output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("name"):
            continue
        model_name = line.split()[0]
        if model_name:
            names.add(model_name)
    return names


def ollama_host_from_api_base(api_base: str) -> str:
    """Convert an API base URL into the OLLAMA_HOST value subprocesses should inherit."""
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OLLAMA_API_BASE must be an http or https URL.")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 11434)
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def is_eligible_local_model(name: str) -> bool:
    """Reject provider-qualified and known cloud-backed Ollama entries."""
    cleaned = (name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned:
        return False
    tag = cleaned.split(":")[-1].lower() if ":" in cleaned else ""
    if tag == "cloud" or tag.endswith("-cloud"):
        return False
    return True


def select_default_model(installed: list[str]) -> str:
    """First installed canonical model, otherwise the first eligible local model."""
    eligible = [name for name in installed if is_eligible_local_model(name)]
    for name in CANONICAL_MODELS:
        if name in eligible:
            return name
    return eligible[0] if eligible else ""


def _order_installed(names: set[str]) -> list[str]:
    eligible = sorted(name for name in names if is_eligible_local_model(name))
    canonical = [name for name in CANONICAL_MODELS if name in eligible]
    rest = [name for name in eligible if name not in canonical]
    return canonical + rest


@dataclass(frozen=True)
class DiscoveryResult:
    models: list[str]
    status: str
    checked_at: str
    host: str


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_models(force_refresh: bool = False, *, api_base: str | None = None) -> DiscoveryResult:
    """Return installed eligible local models. Failures yield an empty list and status 'failed'."""
    global _cached_at, _cached_host, _cached_models, _cached_status

    base = api_base or os.getenv("OLLAMA_API_BASE", OLLAMA_API_BASE)
    try:
        host = ollama_host_from_api_base(base)
    except ValueError:
        return DiscoveryResult(models=[], status="failed", checked_at=_stamp(), host="")

    hostname = urlparse(base).hostname or ""
    if not is_loopback_host(hostname):
        return DiscoveryResult(models=[], status="failed", checked_at=_stamp(), host=host)

    now = time.time()
    with _cache_lock:
        cache_fresh = (
            not force_refresh
            and _cached_host == host
            and _cached_status in {"ok", "empty"}
            and (now - _cached_at) < _CACHE_TTL_SECONDS
        )
        if cache_fresh:
            return DiscoveryResult(
                models=_cached_models.copy(),
                status=_cached_status,
                checked_at=_stamp(),
                host=host,
            )

    env = os.environ.copy()
    env["OLLAMA_HOST"] = host
    try:
        raw_output = subprocess.check_output(
            ["ollama", "list"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
            env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return DiscoveryResult(models=[], status="failed", checked_at=_stamp(), host=host)

    models = _order_installed(_parse_ollama_list_output(raw_output))
    status = "ok" if models else "empty"
    with _cache_lock:
        _cached_models = models
        _cached_at = time.time()
        _cached_host = host
        _cached_status = status
    return DiscoveryResult(models=models.copy(), status=status, checked_at=_stamp(), host=host)


def get_installed_models(force_refresh: bool = False, *, api_base: str | None = None) -> List[str]:
    """Return eligible installed model names. Discovery failure returns an empty list."""
    return discover_models(force_refresh=force_refresh, api_base=api_base).models


def get_litellm_model_ids(force_refresh: bool = False) -> List[str]:
    """Return installed models as litellm Ollama IDs (`ollama/<name>`)."""
    return [f"ollama/{name}" for name in get_installed_models(force_refresh=force_refresh)]


def get_default_litellm_model(force_refresh: bool = False) -> str:
    """Return the preferred default litellm model ID, or an empty string when none are installed."""
    selected = select_default_model(get_installed_models(force_refresh=force_refresh))
    if not selected:
        return ""
    return f"ollama/{selected}"


def selected_model_is_available(selected: str, discovery: DiscoveryResult) -> bool:
    """A failed discovery does not prove the saved model is gone."""
    if discovery.status == "failed":
        return False
    return bool(selected) and selected in discovery.models
