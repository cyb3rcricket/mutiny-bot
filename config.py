"""Shared configuration for MutinyBot."""

import os
from urllib.parse import urlparse

import discord
from dotenv import load_dotenv

# Load environment variables from a local .env file before reading settings.
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Parse BROADCAST_CHANNEL_ID safely at import time. If the env var is missing,
# empty, or non-numeric (e.g. "none"), fall back to 0 instead of raising.
_raw_broadcast = os.getenv("BROADCAST_CHANNEL_ID", "")
try:
    if isinstance(_raw_broadcast, str) and _raw_broadcast.strip().lower() in ("", "none", "null"):
        BROADCAST_CHANNEL_ID = 0
    else:
        BROADCAST_CHANNEL_ID = int(_raw_broadcast)
except (TypeError, ValueError):
    BROADCAST_CHANNEL_ID = 0
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")

# Timezone used by scheduled automation tools (IANA tz name, e.g. "America/Chicago", "UTC").
AUTOMATION_TIMEZONE = os.getenv("AUTOMATION_TIMEZONE", "America/Chicago").strip() or "America/Chicago"

DEFAULT_MODEL = "ollama/qwen2.5-coder:7b"
DEFAULT_SYSTEM_PROMPT = (
    "You are MutinyBot, a practical IT admin assistant here to help the user. "
    "Be concise, technical, and action-oriented. "
    "Always respond in natural, conversational language - never output JSON or raw data structures unless explicitly asked. "
    "When users greet you or ask casual questions, respond with friendly, natural text. "
    "Format your responses for Discord: use **bold** for emphasis, code blocks for technical content, and clear paragraphs. "
    "When ending casual conversations, ask how YOU can help the user, not how the user can help you. "
    "Only use tools when the user explicitly requests automation tasks like scheduling or listing jobs."
)
DB_PATH = "mutiny.db"
SCHEDULER_DB_PATH = os.getenv("SCHEDULER_DB_PATH", "mutiny_scheduler.db")
ALLOWED_MODELS = {
    "ollama/qwen2.5-coder:7b",
    "ollama/phi4-mini",
    "ollama/llama3.1",
}

# Bot owner Discord user ID for privileged commands
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))  # Replace with your actual Discord user ID

intents = discord.Intents.default()
intents.message_content = True

# Maximum number of messages to keep in full context before summarizing
MAX_HISTORY_MESSAGES = 12

# ID of the Discord channel where you want /jobs, /history, and /status to work
# Right-click your monitoring channel → Copy ID, paste the number here.
# Leave as None for now so the commands work in every channel.
MONITORING_CHANNEL_ID = None

# Log file paths for /logs command
LOG_PATHS = {
    "syslog": "/var/log/syslog",
    "auth": "/var/log/auth.log",
    "kern": "/var/log/kern.log",
    "docker": "/var/log/docker.log",
    "nginx": "/var/log/nginx/access.log",
    "apache": "/var/log/apache2/access.log",
    "mysql": "/var/log/mysql/error.log",
    "postgresql": "/var/log/postgresql/postgresql.log",
}


def validate_startup_config() -> tuple[list[str], list[str]]:
    """Validate startup configuration and return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    if not TOKEN or not str(TOKEN).strip():
        errors.append("DISCORD_BOT_TOKEN is missing or empty.")

    raw_broadcast = str(_raw_broadcast or "").strip()
    if raw_broadcast and raw_broadcast.lower() not in {"none", "null"}:
        try:
            int(raw_broadcast)
        except (TypeError, ValueError):
            warnings.append(
                "BROADCAST_CHANNEL_ID is not numeric; falling back to 0 (broadcasts disabled)."
            )

    parsed_ollama_url = urlparse(OLLAMA_API_BASE)
    if parsed_ollama_url.scheme not in {"http", "https"} or not parsed_ollama_url.netloc:
        warnings.append(
            "OLLAMA_API_BASE does not look like a valid http/https URL; API calls may fail."
        )

    return errors, warnings