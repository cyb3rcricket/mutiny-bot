"""MutinyBot launcher."""

import logging
import discord

from bot.bot import MutinyBot
from config import TOKEN, intents


# Basic logging configuration for the entire application.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutiny_bot")

# Ensure a token is present before starting the bot.
if not TOKEN:
    raise ValueError(
        "DISCORD_BOT_TOKEN is missing. Add it to your .env file before starting the bot."
    )


if __name__ == "__main__":
    # Create and run the bot.
    bot = MutinyBot(command_prefix="!", intents=intents)
    bot.run(str(TOKEN))
