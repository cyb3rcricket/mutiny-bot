"""Shared configuration for MutinyBot."""

import os

from dotenv import load_dotenv

# Load environment variables from a local .env file before reading settings.
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BROADCAST_CHANNEL_ID = int(os.getenv("BROADCAST_CHANNEL_ID", "0"))
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
DEFAULT_MODEL = "ollama/qwen2.5-coder:7b"
DEFAULT_SYSTEM_PROMPT = (
    "You are MutinyBot, a practical IT admin assistant. "
    "Be concise, technical, and action-oriented."
)
DB_PATH = "mutiny.db"
ALLOWED_MODELS = {
    "ollama/qwen2.5-coder:7b",
    "ollama/phi4-mini",
    "ollama/llama3.1",
}