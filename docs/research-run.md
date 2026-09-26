# Research Run Contract

This document defines the frozen on-disk contract for Research runs in Mutiny.

Phase 1 decision is frozen: reuse existing SQLite tables (`runs` and `sources` in `database/migrations.py`). No new columns. No new tables. No migration.

## A. Research Run Record

A Research turn is a `runs` row with `tool_name = "research"`.

Existing schema columns: `id`, `job_id`, `tool_name`, `arguments_json`, `status`, `started_at`, `finished_at`, `output`, `error_code`, `request_id`.

- `tool_name`: strictly `"research"`.
- `status`: `running` → `complete` | `failed`.
- `request_id`: existing idempotency key.
- `output`: the write-up only (plain text). Never store the source list only in `output`.
- Do not put the essay in `arguments_json`.

## B. `arguments_json` Shape

The input parameters and run metadata are stored in `runs.arguments_json`.

```json
{
  "question": "What is Mutiny allowed to do in default chat?",
  "mode": "closed",
  "queries": ["mutiny default chat tools", "mutiny loopback ollama"],
  "gaps": [],
  "model": "gemma4:e4b",
  "writer": "local"
}
```

Rules:
- `mode` is `"closed"` | `"wiki"` | `"web"`.
- Phase 7 implements `"web"` behind two locks: `MUTINY_OUTBOUND_ENABLED=1` AND `mode=="web"`.
- `"wiki"` is not implemented and must error.
- `queries` = phrases actually used (may be `[]`).
- `gaps` = strings naming what was not found.
- `writer` is `"local"` (Ollama inference with `tools=None`).
- Do not put the essay in `arguments_json`.

## C. Citeable Snippets

Each citeable snippet is a `sources` row:

Existing schema columns: `id`, `message_id`, `run_id`, `kind`, `title`, `excerpt`, `record_id`, `retrieved_at`, `external_url`.

- `run_id` always set.
- `message_id` also set if the assistant bubble should show the same cards.
- `kind` in closed mode: `"fact"` | `"memory"` | `"document"`.
- `kind` in web mode: `"web"`.
- `title` and `excerpt` required.
- `record_id`: identifies the local fact/document section in closed mode; set to the URL in web mode.
- `external_url`: must be null in closed mode; set to the snippet URL in web mode.
- `retrieved_at`: ISO timestamp when the snippet was attached.

## D. Refuse

The system must refuse:
- Citing a URL / title that is not a `sources` row on this run.
- Inventing footnotes.
- Default chat creating research runs.
- Treating wiki mode as implemented.
- Running web mode when `MUTINY_OUTBOUND_ENABLED` is 0.

## E. Closed Mode

Closed mode looks only at things already on this machine: saved facts, memories, and local files under `MUTINY_DOCS_PATH` (default `./research_docs`). `.md` files are indexed directly; `.pdf` text is indexed when `pypdf` or `fitz` can be imported by the running Python environment. Local document sources have `external_url = null`. Closed mode strips all `http(s)` URLs from the final answer.

## F. Web Mode (Phase 7)

Web mode retrieves live-web snippets through a local SearxNG instance on loopback (`MUTINY_SEARXNG_URL`, default `http://127.0.0.1:8080`).

- **Two locks**: `MUTINY_OUTBOUND_ENABLED=1` in the environment AND `mode=="web"` on the research turn. If outbound is disabled, web runs fail immediately with zero network egress.
- **Snippets only**: title, url, snippet/content. No full-page fetch, no JS rendering, no images. Mutiny does not query Google/Bing directly.
- Only HTTP(S) result URLs with nonempty snippets become web sources; whitespace-only or invalid results are discarded, and stored snippets are capped at 2,000 characters.
- **Pure web retrieval**: mode=web retrieves web snippets only and does not mix local facts/docs into the run.
- **Local writer**: Writer stays local Ollama with `tools=None`. Zero hits skips the writer and reports gaps without inventing citations.
- **Citation sanitizer**: Web mode keeps URLs that exactly match a `source.external_url` on this run; all other URLs are stripped. Citations `[n]` outside `1..len(sources)` are dropped.
