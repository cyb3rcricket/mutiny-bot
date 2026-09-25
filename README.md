# Mutiny

You are not here to rent intelligence from somebody else's server farm.
Mutiny is a personal console that keeps inference on your machine and conversation history on your disk.
No cloud accounts. No telemetry. No remote black box deciding what happens to your prompts.
Build tools that answer to you.
Run models you can inspect.
Keep your memory local.
You run it. You own it.

## What this is

Mutiny is a single-user localhost console for local Ollama chat, SQLite-backed threads, saved facts, and scheduled local tools. Running `python mutiny_bot.py` serves a FastAPI backend and a static single-page interface strictly on `127.0.0.1:8765`. It gives you a private local workstation to prompt local models, persist system prompts and thread state, query personal notes, and run local automations without an external account or remote gateway.

## Look here first

- [web/app.py](web/app.py): Application factory that sets privacy guards, wires database and scheduler lifespans, mounts the static UI, and disables external API documentation CDNs.
- [web/security.py](web/security.py): Loopback host enforcement, process-bound HttpOnly session tokens, Origin and `X-Mutiny-Request` mutation guards, and Content Security Policy headers.
- [llm/llm_handler.py](llm/llm_handler.py): Local inference through `litellm` and Ollama, enforcing loopback-only endpoints, rejecting cloud model identifiers, and sanitizing tool calls.
- [database/migrations.py](database/migrations.py): Versioned SQLite schema migrations with pre-upgrade atomic database backups and legacy record transformations.

## Run

Python 3.11 or newer and a running local Ollama daemon are required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python mutiny_bot.py
```

Open `http://127.0.0.1:8765`. Conversation history, active models, and saved notes persist across page refreshes and server restarts.

Optional vector memory indexing:

```bash
pip install -r requirements-memory.txt
```

MemPalace is used only when its embedding model files already exist on disk. Otherwise, note recall falls back to SQLite facts. The console never downloads model files.

Copy `.env.example` to `.env` to override the port, database path, automation timezone, or Ollama URL. The Ollama URL must resolve to a loopback address. `MUTINY_BIND_HOST` must remain a loopback address; there is no LAN binding.

## Privacy

- The server binds strictly to `127.0.0.1` (or another loopback address you configure). Non-loopback bindings are rejected at startup.
- Inference requests route solely to the configured loopback Ollama endpoint. Cloud model identifiers and remote endpoints are rejected.
- Set `OLLAMA_NO_CLOUD=1` in the Ollama daemon's own service environment before launching it. Setting the variable inside this console process does not reconfigure an already running daemon.
- Ordinary chat interactions do not invoke tools and cannot browse the public web.
- Outbound news monitoring is disabled. `requirements-news.txt` is not installed by default, and the dormant news monitor module in the codebase is neither scheduled nor exposed in the UI.
- The web session relies on an HttpOnly cookie generated per process run. It is not an account system.
- State-modifying requests require matching page Origin headers and the `X-Mutiny-Request` header. OpenAPI and documentation routes are disabled to prevent loading third-party CDNs.

## What you can do

- **Threaded chat**: Manage distinct conversation threads. Conversation history, the selected model, and the configured system personality survive process restarts.
- **Persistent memory**: Save facts with `/remember`, review them with `/recall`, or query your saved knowledge with `/ask-notes`.
- **Local automation**: Execute the local morning briefing on demand or schedule it daily. Manage recurring runs with pause, resume, and stop controls in the Jobs drawer. All run histories and outputs stay in SQLite.
- **Context management**: Clear a thread's stored messages without affecting saved facts, or reset conversation context for the model while keeping the transcript visible on screen.
- **Accurate status**: If Ollama has no installed local models, the model selector reports that state honestly instead of falling back to remote defaults or fictitious models.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q tests
```

The test suite runs entirely offline without Discord credentials or access to the public internet. A live Ollama daemon and provisioned MemPalace environment are optional; tests mock and isolate external boundaries by default.

## Not in this version

- Discord, Telegram, or any other external messenger integrations.
- Streaming completions. The console waits for the full response from the local model before persisting and displaying it.
- Outbound news feeds or news UI controls.
- Host shell execution, Docker inspection, system log viewers, ping utilities, or remote restart controls.
- LAN binding or public network deployment.

## Legacy data

If you are upgrading from an older installation with historical data on disk, all migration tooling is isolated and guarded:

### SQLite database migration

Starting the console automatically migrates an existing `mutiny.db` to the latest schema:
- A complete pre-migration backup is written to `mutiny.db.migration-bak` using SQLite's backup API before any changes are committed.
- Legacy chat history entries are partitioned and converted into threads organized by previous user ID.
- Stored facts preserve their existing IDs.
- Pending broadcast queue entries are converted to run records and marked completed without sending messages externally.
- If the stored system personality matches the old Discord default ("You are MutinyBot, a friendly and conversational Discord assistant..."), it is reset to the local console default. Custom prompts are preserved.

### Legacy scheduler migration

The legacy scheduler database (`mutiny_scheduler.db`) is not loaded by the console runtime. Safe jobs can be inspected and converted to paused jobs in the new scheduler database using:

```bash
python scripts/migrate_legacy_jobs.py mutiny_scheduler.db mutiny_console_scheduler.db
```

Only run this script on databases created locally. Morning briefing jobs are recreated in a paused state for review in the Jobs drawer. News monitor jobs are flagged as disabled, and unsupported legacy payloads are logged and skipped.

### MemPalace memory import

Importing SQLite chat history and notes into MemPalace drawers is an explicit offline script, never executed automatically at startup:

```bash
python -m scripts.migrate_old_memory_to_palace --db-path mutiny.db
```

The migration checkpoints each record in the `memory_imports` table. Subsequent runs safely skip rows that have already been imported.
