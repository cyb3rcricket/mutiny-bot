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

- Python 3.9 or newer
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
pip install -r requirements.txt
```

3. Create a `.env` file in the project root with at least:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
BROADCAST_CHANNEL_ID=0
OLLAMA_API_BASE=http://127.0.0.1:11434
SCHEDULER_DB_PATH=mutiny_scheduler.db
```

`BROADCAST_CHANNEL_ID` is optional, but required if you want the scheduler to post automated results into a Discord channel.
`SCHEDULER_DB_PATH` is optional and defaults to `mutiny_scheduler.db`.

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

The bot will create or update its local SQLite database on first launch, sync slash commands, and connect to Discord with the token from `.env`.

## Tests

Run the built-in unit tests from the repository root:

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

## Available Slash Commands

All slash commands require Manage Server permission and must be used in the designated monitoring channel. Commands have a 3-second cooldown to prevent spam.

### 🤖 Automation & Scheduling

| Command | Description | Example |
|---------|-------------|---------|
| `/schedule <task> <time>` | Schedule a recurring task to run automatically | `/schedule "daily backup" "daily at 02:00"` |
| `/jobs` | List all active scheduled jobs with their status | `/jobs` |
| `/quick-run <tool-name>` | Execute an AI tool immediately | `/quick-run get_morning_briefing` |
| `/snooze-job <job-id> <hours>` | Temporarily pause a scheduled job | `/snooze-job 123 24` |

### 🖥️ System & Infrastructure Monitoring

| Command | Description | Example |
|---------|-------------|---------|
| `/system` | Display comprehensive system information (CPU, RAM, disk usage) | `/system` |
| `/docker` | List all running Docker containers with resource usage | `/docker` |
| `/ping <host>` | Test network connectivity and measure latency | `/ping google.com` |
| `/logs <service>` | View recent log entries for system services | `/logs syslog` |

### 📚 Memory & Knowledge Tools

| Command | Description | Example |
|---------|-------------|---------|
| `/remember <fact>` | Save important information for future reference | `/remember "Server backup runs at 2 AM daily"` |
| `/recall` | Display all saved facts and knowledge | `/recall` |
| `/clear-history` | Clear chat history for the current user | `/clear-history` |
| `/reset` | Reset chat history for the current user | `/reset` |
| `/ask-notes <question>` | Query saved facts and chat history using AI | `/ask-notes "What are our backup schedules?"` |

### 🎨 Creative & Productivity

| Command | Description | Example |
|---------|-------------|---------|
| `/generate-script <task>` | Generate bash scripts using AI assistance | `/generate-script "backup database to S3"` |
| `/explain-error <error_message>` | Get AI-powered explanations for error messages | `/explain-error "ModuleNotFoundError: No module named 'requests'"` |
| `/brainstorm <idea>` | Generate creative ideas and solutions using AI | `/brainstorm "new Discord bot features"` |
| `/daily-insight` | Get a fun, AI-generated system health insight | `/daily-insight` |

### ⚙️ Bot Management

| Command | Description | Example |
|---------|-------------|---------|
| `/botstatus` | Show bot status, uptime, and configuration | `/botstatus` |
| `/switch-model <model_name>` | Change the active AI model | `/switch-model ollama/phi4-mini` |
| `/restart-bot` | Restart the bot with confirmation (owner only) | `/restart-bot` |
| `/sync-commands` | Sync slash commands with Discord (owner only) | `/sync-commands` |

### 🛠️ Utilities

| Command | Description | Example |
|---------|-------------|---------|
| `/list-tools` | Display all available AI tools with descriptions | `/list-tools` |
| `/help` | Show comprehensive help for all commands | `/help` |
| `/post-commands [channel]` | Post full command reference to a channel (owner only) | `/post-commands #general` |

### 🔧 AI Tools (Function Calling)

These tools can be called programmatically or scheduled:

- `get_morning_briefing` - Generate a local morning operations briefing
- `schedule_daily_automation` - Schedule a registered tool to run daily at a specific time
- `list_active_automations` - List all currently active scheduled automations
- `stop_automation` - Stop and remove a scheduled automation by job ID

## Notes

- The bot is designed for local inference only. It passes `OLLAMA_API_BASE` into `litellm` requests and uses an allowlist of `ollama/` model IDs.
- If `DISCORD_BOT_TOKEN` is missing, the bot exits immediately at startup.
- Conversation history and configuration are stored in a local SQLite database.