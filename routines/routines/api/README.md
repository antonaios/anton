# routines.api — FastAPI bridge

Loopback HTTP transport for the routines CLI surface. Consumed by the
React dashboard at `<repo>/dashboard/`.

## Quick start

```bash
# From the routines repo root.
pip install fastapi uvicorn
python -m routines.api.app
# Listens on http://127.0.0.1:8765
# OpenAPI docs at http://127.0.0.1:8765/api/docs
```

Override host/port via env: `AGENTIC_API_HOST`, `AGENTIC_API_PORT`. A
non-loopback host **refuses to start** unless `AGENTIC_ALLOW_PUBLIC_BIND=1`
is also set (#ops-bind-guard) — and then it logs a loud warning. Don't
set the override without a security review — sensitivity gating in the
routines layer assumes the bridge is loopback only.

The vault path follows the same `AGENTIC_VAULT` env override that the
Streamlit dashboard uses; default falls back to `<vault>` on
Windows, `/mnt/x/OS AI Vault` elsewhere.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/health`              | bridge liveness |
| POST | `/api/recall`              | in-process recall query, structured JSON response |
| POST | `/api/recall/index`        | fire-and-forget reindex subprocess, returns PID |
| POST | `/api/workflows/sector-news` | fire `sector-news run <sector>` or `run-all` if no sector (canonical post-#60 surface) |
| POST | `/api/sector-news/run`     | **deprecated alias** for `/api/workflows/sector-news` — kept one cycle for the dashboard's `sectorNewsRun()` helper |
| POST | `/api/memory-promote/run-all` | fire `memory-promote run-all` |
| GET  | `/api/projects`            | active vault projects |
| GET  | `/api/audit-runs?routine=&limit=` | tail one of the audit JSONL files |
| GET  | `/api/vault-pulse?hours=&limit=` | recently-touched vault notes |

## Why some endpoints fire subprocesses and others don't

`/api/recall` is in-process: queries return fast and the structured result
is what the UI cares about. Reindex / sector-news / memory-promote are
multi-minute jobs — firing them as subprocesses lets the HTTP request
return immediately and the job logs to `routines/runs/<routine>.jsonl`
where the audit-runs endpoint can surface progress.

## Adding a route

1. New file under `routes/` with an `APIRouter`.
2. Register it in `app.py`'s `create_app`.
3. Pydantic models for request + response — the dashboard's TypeScript
   client (`dashboard/src/lib/api.ts`) is hand-written today, so update
   both sides.
