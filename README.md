# Mutiny

Mutiny is a single-user console for local Ollama chat, saved notes, and scheduled local tools. It listens on `127.0.0.1` only. Discord is not part of this program.

## Run

Python 3.11 or newer, plus a local Ollama daemon.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python mutiny_bot.py
```

Open `http://127.0.0.1:8765`. The page keeps its own conversation history in SQLite. A refresh shows the same threads.

Optional memory indexing:

```bash
python -m pip install -r requirements-memory.txt
```

MemPalace is used only when its embedding files are already on disk. Otherwise recall uses the SQLite facts. The console does not download those files.

Copy `.env.example` to `.env` to change the port, database path, timezone, or Ollama URL. The Ollama URL must be a loopback address. `MUTINY_BIND_HOST` must stay on loopback; there is no LAN bind.

## Privacy

- The process binds to `127.0.0.1` (or another loopback address you set).
- Inference goes to the configured loopback Ollama endpoint. Cloud model names and non-loopback endpoints are rejected.
- Set `OLLAMA_NO_CLOUD=1` on the Ollama daemon before starting it. Setting that variable in the console process does not reconfigure a daemon that is already running.
- Ordinary chat does not call tools and does not browse the web.
- Outbound news is off. `requirements-news.txt` is not installed by the steps above, and the console does not schedule the news monitor.
- The browser session is an HttpOnly cookie for this process. It is not an account.
- Mutations require the page's own Origin and the `X-Mutiny-Request` header. API docs are disabled so the page does not load a documentation CDN.

## What you can do

- Chat in separate threads. History, the selected model, and the personality survive a restart.
- Remember a fact, recall it, or ask a question over saved notes.
- Run the local morning briefing, or schedule it daily. Pause, resume, and stop are in the Jobs drawer. Results stay in SQLite.
- Clear a thread's messages without deleting saved facts, or reset context while leaving the transcript visible.

If Ollama has no installed models, the model picker says so. It does not invent one.

## Data already on disk

Starting the console migrates an existing `mutiny.db`. A backup is written beside it with a `.migration-bak` suffix before the schema change. Legacy chat rows become one imported thread per old user id. Facts keep their ids. Pending broadcast rows are copied into run history and are not delivered anywhere.

The old scheduler file (`mutiny_scheduler.db` by default) is not started. Jobs from that file can be inspected and, when they are the morning briefing, recreated in a paused state:

```bash
python scripts/migrate_legacy_jobs.py mutiny_scheduler.db mutiny_console_scheduler.db
```

Do not point that script at a file you did not create locally. News jobs are recorded as disabled. Other old payloads are reported and not recreated.

Copying old rows into MemPalace is a separate explicit command. It is not part of startup. A second run skips rows already recorded in `memory_imports`:

```bash
python -m scripts.migrate_old_memory_to_palace --db-path mutiny.db
```

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

The suite is meant to run without Discord and without reaching the public internet. Live Ollama and a provisioned MemPalace install are optional; this environment did not have a running Ollama daemon, so that smoke was not run.

## Not in this version

- Discord, Telegram, or any other messenger.
- Streaming replies. The console waits for the full answer, then stores it.
- Outbound news. The old monitor module is still in the tree and is not scheduled or shown in the UI.
- Shell, Docker, log browsing, ping, or process restart.
- LAN or public binding.
