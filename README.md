# **MUTINY BOT**

You are not here to rent intelligence from somebody else's server farm.
You are here to take it back.

Mutiny Bot is a Discord operations bot for people who want control, speed, and privacy without compromise. It runs on local Ollama models through `litellm`, drives real automation with slash commands, and keeps your memory and configuration in your own stack.

No cloud dependency for inference. No mystery pipeline. No black box decisions about your data. You run it. You own it. You ship it.

## Key Features

- **Local Ollama models only** for AI inference. No remote LLM provider required.
- **Dynamic model detection** via installed Ollama models, with canonical support for `gemma4:e4b`, `phi4-mini:latest`, and `qwen2.5-coder:7b`.
- **Privacy-first architecture**: model inference and memory processing happen on your machine.
- **SQLite-backed state** for chat history, bot configuration, and operational persistence.
- **MemPalace-powered long-term memory** for deduplication and semantic recall (vector memory backed by ChromaDB under the hood).
- **APScheduler + SQLAlchemy job persistence** for durable, recurring automations.
- **RSS news monitoring pipeline** with AI summarization and memory-based deduplication.
- **Broadcast queue system** to safely push scheduled outputs into Discord channels.
- **System operations command surface** for logs, Docker visibility, host checks, and health insight.
- **AI utility workflows** for script generation, error explanation, brainstorming, and structured tooling.
- **Fully local runtime footprint** for model work and memory storage, designed for teams that do not want to hand their internal context to the cloud.

## Quick Start / Installation

### Requirements

- Python 3.9+
- A Discord bot token
- A running local Ollama daemon
- Recommended local models:
  - `gemma4:e4b`
  - `phi4-mini:latest`
  - `qwen2.5-coder:7b`

### Install

1. Create and activate a virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create `.env` in the project root.

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
BROADCAST_CHANNEL_ID=0
OLLAMA_API_BASE=http://127.0.0.1:11434
SCHEDULER_DB_PATH=mutiny_scheduler.db
```

`BROADCAST_CHANNEL_ID` is optional unless you want scheduled outputs posted to a Discord channel.
`SCHEDULER_DB_PATH` is optional and defaults to `mutiny_scheduler.db`.

4. Pull the current canonical models.

```bash
ollama pull gemma4:e4b
ollama pull phi4-mini:latest
ollama pull qwen2.5-coder:7b
```

5. Run the bot.

```bash
source .venv/bin/activate
python mutiny_bot.py
```

On first launch, Mutiny Bot initializes local databases, loads tools, syncs slash commands, and starts scheduler services.

### Run Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

## How To Use The Main Commands

All slash commands require **Manage Server** permission unless noted otherwise. A global cooldown of 3 seconds is enforced.

### Core Automation

| Command | What it does | Example |
|---|---|---|
| `/schedule <task> <time>` | Schedule a recurring task | `/schedule "daily backup" "daily at 02:00"` |
| `/jobs` | List active scheduled jobs | `/jobs` |
| `/snooze-job <job-id> <hours>` | Pause a scheduled job temporarily | `/snooze-job 123 24` |
| `/quick-run <tool-name>` | Execute a tool immediately (owner only) | `/quick-run get_morning_briefing` |

### News Monitoring

| Command | What it does | Example |
|---|---|---|
| `/add_news_monitor <channel> <name> <search_query> [frequency] [time]` | Create a scheduled RSS news monitor | `/add_news_monitor #news ai-news "artificial intelligence" daily 08:00` |
| `/list_news_monitors` | List active news monitors | `/list_news_monitors` |
| `/remove_news_monitor <name>` | Remove a news monitor | `/remove_news_monitor ai-news` |
| `/run_news_monitor <name>` | Trigger a news monitor immediately | `/run_news_monitor ai-news` |

### System + Memory + AI Operations

| Command | What it does | Example |
|---|---|---|
| `/system` | Show host system health | `/system` |
| `/docker` | List running Docker containers | `/docker` |
| `/logs <service>` | Show recent service logs | `/logs syslog` |
| `/ping <host>` | Run connectivity/latency check | `/ping google.com` |
| `/remember <fact>` | Persist a fact to memory | `/remember "Server backup runs at 2 AM daily"` |
| `/recall` | Recall saved facts | `/recall` |
| `/ask-notes <question>` | Ask AI over memory + chat context | `/ask-notes "What are our backup schedules?"` |
| `/generate-script <task>` | Generate a bash script | `/generate-script "backup database to S3"` |
| `/explain-error <error_message>` | Explain and debug an error | `/explain-error "ModuleNotFoundError: No module named requests"` |
| `/brainstorm <idea>` | Generate practical ideas | `/brainstorm "new Discord bot features"` |
| `/daily-insight` | Get a daily AI ops insight | `/daily-insight` |
| `/clear-history` | Clear your chat history | `/clear-history` |
| `/reset` | Reset your chat context | `/reset` |

### Model + Bot Control

| Command | What it does | Example |
|---|---|---|
| `/model <model_name>` | Set active model | `/model phi4-mini:latest` |
| `/switch-model <model_name>` | Switch active model (autocomplete enabled) | `/switch-model phi4-mini:latest` |
| `/personality <prompt_text>` | Set the system prompt | `/personality You are a practical IT assistant...` |
| `/status` | Show model, installed models, personality snippet, DB size | `/status` |
| `/botstatus` | Show uptime, active jobs, and history counts | `/botstatus` |
| `/sync-commands` | Resync app commands (owner only) | `/sync-commands` |
| `/post-commands [channel]` | Publish command reference (owner only) | `/post-commands #general` |
| `/restart-bot` | Restart bot with confirmation (owner only) | `/restart-bot` |
| `/list-tools` | List registered AI tools (owner only) | `/list-tools` |
| `/help` | Show command help | `/help` |

### AI Tools (Function Calling)

These tools can be invoked by the model or scheduled:

- `get_morning_briefing` - Generate a local morning operations briefing
- `schedule_daily_automation` - Schedule a tool at a daily time
- `list_active_automations` - List active scheduled automations
- `stop_automation` - Stop a scheduled automation by job ID
- `execute_news_monitor` - Fetch, summarize, and broadcast monitored news

## Current Status

Mutiny Bot is live with dynamic local model detection and currently running these installed Ollama models:

- `gemma4:e4b`
- `phi4-mini:latest`
- `qwen2.5-coder:7b`

Startup log confirms:

`Loaded 3 Ollama models: gemma4:e4b, phi4-mini:latest, qwen2.5-coder:7b`

### Operational Notes

- If `DISCORD_BOT_TOKEN` is missing or empty, startup validation will fail and the bot will not launch.
- Conversation history and configuration are persisted locally in SQLite.
- Inference is local-only through Ollama via `litellm` using your configured `OLLAMA_API_BASE`.

## Closing Statement

Build tools that answer to you.
Run models you can inspect.
Keep your memory local.
Automate the work that burns your time.

This is not a demo of what might be possible someday.
This is already yours.
Take it further.