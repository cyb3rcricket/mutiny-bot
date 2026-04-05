"""MutinyBot launcher."""

import logging
import discord

from bot.bot import MutinyBot
from config import TOKEN, intents, validate_startup_config


# Basic logging configuration for the entire application.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutiny_bot")

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
