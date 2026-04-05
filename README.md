# Mutiny Bot

Mutiny Bot is a Discord bot built with `discord.py` that uses local Ollama models through `litellm` for chat and automation. It keeps a small SQLite database for chat history and configuration, exposes slash commands for model/personality/status management, and includes AI tools for scheduling and generating a local morning briefing.

## Features

- Chat responses powered by local Ollama models only
- Slash commands for switching models, updating the system prompt, and viewing status
- SQLite-backed chat history and bot configuration
- Scheduled automations via APScheduler
- Local morning briefing tool with no external API calls
- Broadcast queue for pushing messages to a configured Discord channel

## Requirements

- Python 3.10 or newer
- A Discord bot application and token
- A running local Ollama server
- At least one allowed Ollama model pulled locally

## Setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies:

```bash
pip install discord.py aiosqlite python-dotenv apscheduler litellm
```

3. Create a `.env` file in the project root with at least:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
BROADCAST_CHANNEL_ID=0
OLLAMA_API_BASE=http://127.0.0.1:11434
```

`BROADCAST_CHANNEL_ID` is optional, but required if you want the scheduler to post automated results into a Discord channel.

4. Start Ollama locally and make sure one of the allowed models exists:

```bash
ollama pull qwen2.5-coder:7b
```

The bot only accepts these model IDs:

- `ollama/qwen2.5-coder:7b`
- `ollama/phi4-mini`
- `ollama/llama3.1`

## Run

From the repository root, run:

```bash
source .venv/bin/activate
python mutiny_bot.py
```

The bot will create or update `mutiny.db` on first launch, sync slash commands, and connect to Discord with the token from `.env`.

## Commands

### Slash commands

- `/model` - switch the active local model
- `/personality` - update the system prompt
- `/status` - show the current model, prompt snippet, and database size

### AI tools

- `get_morning_briefing` - generate a local morning ops briefing
- `schedule_daily_automation` - schedule a registered tool to run daily
- `list_active_automations` - list active scheduled jobs
- `stop_automation` - remove a scheduled job by ID

## Notes

- The bot is designed for local inference only. It passes `OLLAMA_API_BASE` into `litellm` requests and uses an allowlist of `ollama/` model IDs.
- If `DISCORD_BOT_TOKEN` is missing, the bot exits immediately at startup.
- Conversation history and configuration are stored in `mutiny.db`.