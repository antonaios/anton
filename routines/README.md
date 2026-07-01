# routines — ANTON Python bridge + business logic

> FastAPI bridge (49 endpoints, 19 router files) + ~24 routine modules + per-call audit. Runs as a Windows process; serves the React dashboard at `http://127.0.0.1:8765/` in production mode.

**Canonical reference:** see `<repo>\ANTON-CATALOG.md` for the full inventory — every route, every routine, every sensitivity tier, every scheduled task. This README is a thin entry point.

## What's here

| Layer | Module(s) | Purpose | CATALOG ref |
|---|---|---|---|
| **Bridge** | `api/` | FastAPI app + 19 routers + 49 endpoints + lifespan + central guards | §5 |
| **Sessions** | `sessions/` | SQLite-backed chat sessions + LLM router | §7 |
| **Hooks** | `hooks/` | Decorators + event bus + central guards (sensitivity, audit, cost-cap) | §3 |
| **Scheduler** | `scheduler/` | APScheduler in-process + 5 cron jobs + CRUD endpoints | §8 |
| **Credentials** | `credentials/` | Encrypted-at-rest (Fernet + Windows DPAPI) + loopback-only API | §13 |
| **Routines** | `hinotes/`, `recall/`, `sectornews/`, `sectors/`, `morning_brief/`, `daily_digest/`, `vault_health/`, `intake/`, `promotion/`, `markets/`, `dealtracker/`, `earnings/`, `bd/`, `learning/`, `lessons/`, `projects/` | Per-domain business logic; each writes its own audit JSONL | §7 |
| **Telemetry** | `telemetry/` | Per-call LLM cost table + writer + roll-up endpoint | §5 + §13 |
| **Shared** | `shared/` | Audit writer, routing, ollama_client, filename sanitisation, profile loader | §7 |

## Run the bridge

```powershell
# From <repo>\routines\
$env:AGENTIC_VAULT = "<vault>"
.venv\Scripts\agentic-api.exe
# Bridge alive on http://127.0.0.1:8765/
# Dashboard served from dist/ when AGENTIC_DASHBOARD_MODE=production
```

For dev mode (Vite HMR at :5173) + full mode-flip recipe: see `HANDOFF-2026-05-26-PM.md` §10 or ANTON-CATALOG §22.5.

## Test

```powershell
cd "<repo>\routines"
.venv\Scripts\python.exe -m pytest -q
# Expected: 846 passed (post-M-COMMIT 9b0390e). 61 test files across api/, sessions/, hooks/, scheduler/, credentials/, telemetry/, workspaces/, proposals/, projects/, shared/, markets/, recall/, sectornews/.
```

## Sensitivity posture (central-hook enforced)

Per `_claude/CLAUDE.md` §4 + ANTON-CATALOG §12. Routing flag: `AGENTIC_PLAN_TIER=bridge | enterprise`.

- **public** → any cloud lane
- **internal** → Claude (Max/Enterprise); MiniMax only if no party named
- **confidential** → **local Ollama during bridge**; Claude Enterprise+ZDR post-procurement
- **MNPI** → **local Ollama only** — sole exception: the default-OFF enterprise-MNPI attestation gate (`CLAUDE.md` §5.2; chat-path only, explicit + attested + escalated); raw `_mnpi/` source stays local regardless of tier

Enforcement is centralised at `hooks/central_guards.py:enforce_sensitivity_lane` (`@before_llm_call`). No skill/composite/crew can bypass.

## Repo state

- **Standalone git repo** (this `routines/` is not part of the umbrella `<repo>\` repo — it has its own `.git`)
- HEAD: `9b0390e` (M-COMMIT — #25 + #25b credentials)
- 846 tests passing
- No remote configured (local-only by design during bridge phase per OUTSTANDING #50)

## Where to read next

1. **`<repo>\ANTON-CATALOG.md`** — full structural reference for every endpoint, routine, sensitivity rule (§§1-16); roadmap for what's planned (§17); platform context (§§18-22).
2. **`<repo>\OUTSTANDING.md`** — live backlog. `Last touched` line is the most-recent edit.
3. **`<repo>\HANDOFF.md`** — umbrella handoff for cold-start sessions.
4. **`<vault>\_claude\CLAUDE.md`** — operating rules (§3 atomic notes; §4 sensitivity; §5 never list; §12 lane taxonomy).
5. **`<vault>\Topics\Architecture\workspace-write-policy.md`** — where every routine's output lands.

---

*Refreshed 2026-05-27. Refresh cadence per ANTON-CATALOG §16: when a routine ships or a contract changes. Otherwise this README stays a thin pointer doc — ANTON-CATALOG carries the depth.*
