"""MutinyBot launcher."""

import logging
import os
import sys
from pathlib import Path


def _ensure_project_venv() -> None:
    """Re-exec with the local project venv when available.

    This prevents common startup failures where users run `python3 mutiny_bot.py`
    outside `.venv` and miss required dependencies.
    """
    running_in_venv = sys.prefix != sys.base_prefix
    if running_in_venv:
        return

    project_root = Path(__file__).resolve().parent
    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return

    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


_ensure_project_venv()

_KNOWN_RUNTIME_DEPENDENCIES = {
    "discord",
    "aiosqlite",
    "dotenv",
    "apscheduler",
    "litellm",
    "sqlalchemy",
}

try:
    from bot.bot import MutinyBot
    from config import TOKEN, intents, validate_startup_config
    import tools.task_prioritizer  # noqa: F401
except ModuleNotFoundError as dependency_error:
    missing_name = str(getattr(dependency_error, "name", "") or "")
    if missing_name in _KNOWN_RUNTIME_DEPENDENCIES:
        raise SystemExit(
            f"Missing required dependency '{missing_name}'. Install project dependencies with:\n"
            f"  {sys.executable} -m pip install -r requirements.txt"
        ) from dependency_error
    raise



# Basic logging configuration for the entire application.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutiny_bot")


def get_capabilities_response() -> str:
    """Return the canonical capability list for user-facing "what can you do" questions."""
    return (
        "I'm MutinyBot, your direct Discord assistant with real tools:\n"
        "- Morning briefing: local system snapshot and ops checklist.\n"
        "- News monitoring: RSS monitoring with deduplication and scheduled posting.\n"
        "- Eisenhower task prioritization: real urgent/important matrix classification with concrete next steps.\n"
        "- MemPalace memory: persistent memory and knowledge retrieval across conversations.\n"
        "- Local Ollama models: phi4-mini, gemma4:e4b, and qwen2.5-coder for local-first AI responses.\n"
        "- Automation scheduling: create, list, and stop recurring tool jobs."
    )

config_errors, config_warnings = validate_startup_config()
for warning in config_warnings:
    logger.warning(warning)

if config_errors:
    formatted_errors = "\n".join(f"- {error}" for error in config_errors)
    raise ValueError(f"Invalid startup configuration:\n{formatted_errors}")


if __name__ == "__main__":
    # Create and run the bot.
    bot = MutinyBot(command_prefix="!", intents=intents)
    bot.run(str(TOKEN))
