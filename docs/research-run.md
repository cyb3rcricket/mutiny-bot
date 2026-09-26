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
- v1 implementation may accept only `"closed"`; `"wiki"` and `"web"` must error.
- `queries` = phrases actually used (may be `[]`).
- `gaps` = strings naming what was not found.
- `writer` is `"local"` for now.
- Do not put the essay in `arguments_json`.

## C. Citeable Snippets

Each citeable snippet is a `sources` row:

Existing schema columns: `id`, `message_id`, `run_id`, `kind`, `title`, `excerpt`, `record_id`, `retrieved_at`, `external_url`.

- `run_id` always set.
- `message_id` also set if the assistant bubble should show the same cards.
- `kind` in v1: `"fact"` | `"memory"`.
- Reserved later: `"document"` | `"wikipedia"` | `"web"`.
- `title` and `excerpt` required.
- `record_id` for local facts.
- `external_url` must be null in closed mode.
- `retrieved_at` = when the snippet was attached.

## D. Refuse

The system must refuse:
- Citing a URL / title that is not a `sources` row on this run.
- Inventing footnotes.
- Default chat creating research runs.
- Treating wiki/web modes as implemented.

## E. Closed Mode

Closed mode looks only at things already on this machine (saved facts; later local files). Web and wiki are field values only so we do not migrate later.
