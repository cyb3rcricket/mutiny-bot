"""Side-effect-light local configuration.

Importing this module reads environment variables and nothing else. It does
not import Discord, discover models, or open a network connection.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Exact historical default. Migration replaces a stored prompt only when it
# matches this string, so the text must not drift.
LEGACY_DEFAULT_SYSTEM_PROMPT = (
    "You are MutinyBot, a friendly and conversational Discord assistant. Respond naturally and helpfully to the user. "
    "\n\nYou are MutinyBot, a practical IT admin assistant here to help the user. "
    "Be friendly, conversational, and action-oriented. "
    "Always respond in natural, conversational language - never output JSON or raw data structures unless explicitly asked. "
    "When users greet you or ask casual questions, respond with friendly, natural text. "
    "Format your responses for Discord: use **bold** for emphasis, code blocks for technical content, and clear paragraphs. "
    "When ending casual conversations, ask how YOU can help the user, not how the user can help you. "
    "Only use tools when the user explicitly requests automation tasks like scheduling or listing jobs."
)

DEFAULT_SYSTEM_PROMPT = (
    "You are Mutiny, a local assistant running on this machine. "
    "Open with the answer in one or two direct sentences, then add only the detail that was asked for. "
    "When the user asks for JSON, code, or another exact format, return that format unchanged. "
    "Do not invent citations, tool results, or facts you were not given. "
    "You cannot browse the web or leave this machine."
)

# Preference label only. Availability comes from runtime discovery.
DEFAULT_MODEL = "gemma4:e4b"

DB_PATH = os.getenv("MUTINY_DB_PATH", "mutiny.db")
SCHEDULER_DB_PATH = os.getenv("SCHEDULER_DB_PATH", "mutiny_console_scheduler.db")
LEGACY_SCHEDULER_DB_PATH = os.getenv("LEGACY_SCHEDULER_DB_PATH", "mutiny_scheduler.db")
PALACE_PATH = os.path.expanduser(os.getenv("MUTINY_PALACE_PATH", "~/.mutiny/palace"))
BIND_HOST = os.getenv("MUTINY_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.getenv("MUTINY_PORT", "8765"))
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434").strip()
AUTOMATION_TIMEZONE = os.getenv("AUTOMATION_TIMEZONE", "America/Chicago").strip() or "America/Chicago"
OUTBOUND_ENABLED = os.getenv("MUTINY_OUTBOUND_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MAX_HISTORY_MESSAGES = 12
MAX_INPUT_CHARS = 32_000

# Read by the dormant news helper only. The console never starts that helper.
_raw_broadcast = os.getenv("BROADCAST_CHANNEL_ID", "")
try:
    if isinstance(_raw_broadcast, str) and _raw_broadcast.strip().lower() in ("", "none", "null"):
        BROADCAST_CHANNEL_ID = 0
    else:
        BROADCAST_CHANNEL_ID = int(_raw_broadcast)
except (TypeError, ValueError):
    BROADCAST_CHANNEL_ID = 0

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str | None) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def ollama_endpoint_error(api_base: str = OLLAMA_API_BASE) -> str | None:
    """Return an error when the Ollama URL is not a loopback HTTP endpoint."""
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "OLLAMA_API_BASE must be an http or https URL."
    if not is_loopback_host(parsed.hostname):
        return "OLLAMA_API_BASE must point at a loopback address."
    return None


def bind_host_error(host: str = BIND_HOST) -> str | None:
    if not is_loopback_host(host):
        return "MUTINY_BIND_HOST must be a loopback address. LAN bind is not available."
    return None


def validate_startup_config() -> tuple[list[str], list[str]]:
    """Return loopback and Ollama problems. There is no messenger credential."""
    errors: list[str] = []
    warnings: list[str] = []
    host_error = bind_host_error()
    if host_error:
        errors.append(host_error)
    ollama_error = ollama_endpoint_error()
    if ollama_error:
        errors.append(ollama_error)
    return errors, warnings
