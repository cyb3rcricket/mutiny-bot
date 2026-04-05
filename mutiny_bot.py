"""MutinyBot launcher."""

from bot.bot import MutinyBot, intents
from config import TOKEN


if __name__ == "__main__":
    # Create and run the bot.
    bot = MutinyBot(command_prefix="!", intents=intents)
    bot.run(str(TOKEN))
