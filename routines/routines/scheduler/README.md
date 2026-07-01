# routines/scheduler — bridge-embedded APScheduler

Implements [[OUTSTANDING]] #23. Pattern lifted from AutoGPT
(`backend/executor/scheduler.py`); see
[[AUTOGPT-EVALUATION]] §2.2 for the original.

## What this is

A `BackgroundScheduler` instance owned by the FastAPI bridge:

- **`default`** jobstore → `SQLAlchemyJobStore` at
  `<routines>/state/schedules.db`. Cron jobs persist across bridge restarts.
- **`ephemeral`** jobstore → `MemoryJobStore`. One-off jobs and notifications
  that don't need to survive a restart.

Both share one scheduler → one worker thread pool → no race between persistent
and ephemeral.

## What this is NOT (yet)

- The framework only. **No specific job is wired** — morning-brief and the
  maintenance jobs come in a follow-on #23 session.
- No CRUD API. `GET /api/scheduler/jobs` is read-only; POST/DELETE land
  alongside the job-registration follow-on.

## Lifecycle

The scheduler is started by the FastAPI `lifespan` in
[[../api/app]] and stopped on shutdown. Multiple uvicorn workers MUST NOT
share the same SQLite jobstore — run with `--workers 1`. The bridge ships
with one worker anyway; this is a constraint to remember if scaling.

## Production autostart — Windows Service via nssm

Out-of-the-box, the bridge runs only while a terminal is open. To turn it
into a system service that starts at boot and restarts on crash:

1. **Install nssm** (Non-Sucking Service Manager):
   ```
   choco install nssm
   ```
   (or download from <https://nssm.cc> and unzip to `C:\Tools\nssm`).

2. **Install the service:**
   ```
   nssm install AgenticBridge "C:\Python314\python.exe" "-m" "routines.api.app"
   nssm set AgenticBridge AppDirectory "<repo>\routines"
   nssm set AgenticBridge AppEnvironmentExtra ^
     "AGENTIC_DASHBOARD_MODE=production" ^
     "AGENTIC_API_HOST=127.0.0.1" ^
     "AGENTIC_API_PORT=8765"
   nssm set AgenticBridge Start SERVICE_AUTO_START
   ```

3. **Logging** — point stdout/stderr at the routines log dir:
   ```
   nssm set AgenticBridge AppStdout "<repo>\routines\runs\bridge.stdout.log"
   nssm set AgenticBridge AppStderr "<repo>\routines\runs\bridge.stderr.log"
   nssm set AgenticBridge AppRotateFiles 1
   nssm set AgenticBridge AppRotateBytes 10485760
   ```

4. **Crash-restart policy** (default is exponential backoff; reasonable):
   ```
   nssm set AgenticBridge AppExit Default Restart
   nssm set AgenticBridge AppRestartDelay 5000
   ```

5. **Start it:**
   ```
   nssm start AgenticBridge
   ```

6. **Verify:** `Get-Service AgenticBridge` should show `Status: Running`.
   Tail `runs/bridge.stdout.log` for the lifespan banner — `BridgeScheduler
   started (db=…)` confirms the embedded scheduler came up.

## Uninstall

```
nssm stop AgenticBridge
nssm remove AgenticBridge confirm
```

## Operator review checklist (when picking up #23 follow-on)

- [ ] Decide which jobs are mandatory at startup vs. user-managed
- [ ] Add POST `/api/scheduler/jobs` (CRUD)
- [ ] Add the morning-brief cron (`CronTrigger.from_crontab("30 6 * * 1-5")` per
      eval doc) — but only after credentials-manager (#25) lands so the
      brief can reach the cloud lane safely
- [ ] Decide jobstore name for system-maintenance jobs vs. user-scheduled
      ones (probably `default` for both with a `name` prefix convention)
- [ ] Service install — choose between nssm (above) and
      `python-windows-service` directly
