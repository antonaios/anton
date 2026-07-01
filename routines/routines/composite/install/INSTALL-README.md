# install_synapse — operator-facing install guide

Scripts the 8-step install workaround the 2026-05-26 overnight spike
validated empirically. Encodes the cp1252 fix, the Node-prereq bypass,
and the host-agent + custom-tool pre-seeding so a cold Windows machine
can stand up a working Synapse backend in one command.

**Status:** staged at `proposed-2026-05-26-phase6/26a-installer/`; not
yet promoted. Promotion target: `routines/composite/install/`.

## 1. Prerequisites

- Windows 10/11 (the spike was on Windows 11 Home 26200).
- Python 3.11+ on PATH (spike used 3.13.0).
- Ollama installed + reachable at `http://127.0.0.1:11434` with
  `qwen3:14b` pulled.
- ~6 minutes of disk I/O for `pip install synapse-orch-ai==1.6.4` (~190
  transitive deps; ~600 MB on disk).
- Free TCP port `9100` (the bridge owns 8765 — Synapse cannot share).
- **NOT required:** Docker (TRANSFORM is replaced by the bridge
  `_compose` proxy — see #26b).
- **NOT required:** Node.js 20.9+ (CLI requires it, but the installer
  invokes `python backend/main.py` directly; backend works on Node 18+
  with non-fatal `EBADENGINE` warnings).

## 2. Install

```powershell
# From the staging dir (post-promotion: from routines/composite/install/)
cd "<repo>\proposed-2026-05-26-phase6\26a-installer"

# Full install + autostart task
python install_synapse.py install --register-task

# Or: full install without autostart
python install_synapse.py install

# Or: scaffold only, do not launch the backend
python install_synapse.py install --no-start
```

The 8 steps execute in order. Every step prints `[run]` (did work),
`[skip]` (already in target state), `[ok]` (verified), or `[warn]`
(continuing despite anomaly). All steps are idempotent — re-running is
safe and only does work where state diverges from the target.

Final output should look like:

```
install_synapse v0 — target: <repo>\synapse
──────────────────────────────────────────────────────────────────────
  [skip] venv already exists at <repo>\synapse\.venv
  [skip] synapse-orch-ai==1.6.4 already installed
  [skip] data dir exists at <repo>\synapse\data
  [skip] settings.json exists at <repo>\synapse\data\settings.json (operator-editable; not overwriting)
  [run]  writing 2 ANTON tool(s) to custom_tools.json (3 total)
  [run]  adding 1 composite host agent(s) to user_agents.json (11 total)
  [ok]   backend up on :9100 (PID 12345)
  [run]  registering Scheduled Task 'ANTON-Synapse-Backend'
  [ok]   task 'ANTON-Synapse-Backend' registered for logon
──────────────────────────────────────────────────────────────────────
install complete
  backend       : http://127.0.0.1:9100
  data dir      : <repo>\synapse\data
  log           : <repo>\synapse\data\backend.log
  next          : run smoke tests in INSTALL-README.md §3
```

## 3. Smoke tests

After install, verify the backend answers and the ANTON tools / host
agents are registered:

```powershell
# A. Health probe (should return 200)
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9100/api/health

# B. Tools listing — should include anton_recall + compose_pitch_payload
$tools = Invoke-RestMethod http://127.0.0.1:9100/api/tools/custom
$tools | Where-Object { $_.name -in @('anton_recall', 'compose_pitch_payload') } | Select-Object name, url

# C. Agents listing — should include agent_pitch_host
$agents = Invoke-RestMethod http://127.0.0.1:9100/api/agents
$agents | Where-Object { $_.id -eq 'agent_pitch_host' } | Select-Object id, tools

# D. Spike-repro: recall roundtrip (proves end-to-end HTTP-tool path)
#    (Requires the ANTON bridge running on :8765 with anton_recall live.)
$body = @{ name = 'anton_recall'; args = @{ query = 'pension'; limit = 3 } } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:9100/api/tools/run -Method POST -Body $body -ContentType 'application/json'
```

All four should return 200 / structured JSON. Failure on (D) usually
means the bridge isn't running — start it via `python -m routines.api.app`.

## 4. Subcommands (operations after install)

```powershell
# Start backend (no-op if already running)
python install_synapse.py start

# Stop backend (kills the PID listening on the configured port)
python install_synapse.py stop

# Read-only status report
python install_synapse.py status

# Just (re-)register the Scheduled Task
python install_synapse.py register-task

# Custom install location + port
python install_synapse.py --install-root "D:\synapse" --port 9200 install
```

## 5. Failure modes (what to do when it breaks)

| Symptom | Cause | Fix |
|---|---|---|
| `venv creation failed` | Python on PATH is broken / wrong version | Verify `python --version` returns 3.11+ |
| `pip install failed (exit N)` | Network / disk / pin mismatch | Re-run; if persistent, check pip's last 20 lines of output |
| `backend did not bind :9100 within 30s` | Port already taken; Ollama not running; settings.json invalid | Tail `data/backend.log` for the actual exception |
| `UnicodeEncodeError: 'charmap'` in log | `PYTHONIOENCODING` not propagated (rare; means script env composition broke) | File a bug — installer should set this automatically |
| `Scheduled Task registration failed` | Not running as admin (LIMITED tier needs current-user perms only — should not happen, but) | Run PowerShell as admin and re-run `register-task` |
| `custom_tools.json exists but is invalid JSON` | Operator hand-edited and broke the file | Restore from backup or delete the file then re-run install |

For anything not above: tail `data/backend.log` (Synapse's own stdout)
and check the spike artefact `<repo>/SYNAPSE-SPIKE-RESULTS-2026-05-26.md`
§"Findings per item" for known v1.6.4 quirks.

## 6. Rollback

Tear down the entire install (no git effects — every byte is under the
install root):

```powershell
# 1. Stop backend
python install_synapse.py stop

# 2. Delete Scheduled Task
schtasks /Delete /TN ANTON-Synapse-Backend /F

# 3. Remove install root (everything: venv + data + checkpoints)
Remove-Item -Recurse -Force "<repo>\synapse\"
```

Re-running `install` from a fresh state is supported — see §2.

## 7. Operator review checklist (pre-promotion)

Before promoting `install_synapse.py` into `routines/composite/install/`:

- [ ] Verify `--install-root` defaults match where the operator wants
      Synapse to live long-term (currently `<repo>/synapse/`).
- [ ] Confirm `PLACEHOLDER_CUSTOM_TOOLS` covers the tools `/pitch` will
      need (currently: `anton_recall` + `compose_pitch_payload`
      placeholder). The full list arrives once each tool's bridge route
      lands.
- [ ] Confirm `PLACEHOLDER_HOST_AGENTS` shape is what the operator wants
      per composite (currently 1 host agent for `/pitch`; `/teaser` and
      `/ic-memo` get added when those composites scope).
- [ ] Decide whether the Scheduled Task should default to `--register-task`
      ON (current default: OFF; operator opts in explicitly).
- [ ] Smoke-test on a clean machine if possible (rollback step 6 first
      on the spike box) to validate cold-run behaviour.

## 8. What this does NOT do (out of scope)

- Does **not** install Ollama or pull `qwen3:14b` — operator does this
  out-of-band.
- Does **not** upgrade Node.js — the installer works on Node 18+; the
  operator can `winget install OpenJS.NodeJS.LTS` later if/when they
  want the Synapse web UI.
- Does **not** modify the ANTON bridge (`routines/api/app.py`) — staged
  bridge changes live in `26b-compose-proxy/` and `26c-audit-mirror/`,
  promoted separately.
- Does **not** create any composite orchestration JSONs — those land
  with #27 (`/pitch`), #28 (`/teaser`), #29 (`/ic-memo`).
