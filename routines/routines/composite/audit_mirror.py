"""Composite audit SSE mirror (#26c) — tees Synapse events into per-run JSONL.

Promoted 2026-06-09 from ``proposed-2026-05-26-phase6/26c-audit-mirror/``
per STAGING-README §3 (``DEFAULT_RUNS_DIR`` now resolves through
``routines.api.deps.RUNS_DIR``; behaviour otherwise unchanged).

## Why this module is the canonical audit (not Synapse's checkpoint)

The 2026-05-26 overnight spike found four gaps in Synapse's on-disk
checkpoint that make it unsafe as a single source of truth (see
``SYNAPSE-SPIKE-REVIEW-2026-05-26.md`` §Item 5 — "Audit observability"):

1. **Per-step errors NOT in step_history.** Failed steps appear as
   ``status: completed``; the actual error only goes through the SSE
   stream. Polling the checkpoint at the end loses every error event.
2. **Non-LLM steps don't populate ``_exec_memory``.** PRINT,
   EXTRACT_JSON, HUMAN, and TRANSFORM produce no per-step trace.
3. **Token/cost stays 0 for Ollama.** Only cloud-API calls land in the
   cost fields. Local-LLM cost has to be instrumented before/after.
4. **Checkpoints live in the install dir, not ``SYNAPSE_DATA_DIR``** —
   so ``pip install --upgrade synapse-orch-ai`` wipes the entire audit
   history.

Plus a fifth gap from Item 6: **a run cancelled BEFORE its first
checkpoint write leaves no trace on disk.** Subsequent GET returns 404.
Cost was incurred, action happened, nothing recorded.

This module fixes all five by **mirroring on the write-side via SSE**
rather than polling the checkpoint. Every composite run is captured
end-to-end in ``routines/runs/composite.<key>.jsonl`` — one JSONL line
per significant event:

  * ``run_started``  — written synchronously on ``/composite/run``
    accept (BEFORE Synapse is even hit, so even a network failure to
    Synapse leaves a trace).
  * ``step_started`` / ``step_completed`` — from Synapse SSE.
  * ``step_error``   — from Synapse SSE (the gap #1 case).
  * ``human_input_required`` — from Synapse SSE (the gap from Item 3).
  * ``run_cancelled`` — written synchronously on cancel proxy (BEFORE
    Synapse is hit, so the mid-flight-before-first-checkpoint case is
    covered).
  * ``done`` — terminal record; either ``ok`` (Synapse emitted ``done``)
    or ``error`` / ``cancelled`` (synthesised by mirror on terminal
    SSE conditions or HTTP failure).

This module is a LIBRARY: the ``/composite/run`` + ``/composite/runs/
{id}/cancel`` proxy routes (land with #27 ``/pitch``, see OUTSTANDING
§"composite invocation") import ``record_run_started`` /
``record_run_cancelled`` / ``start_mirror_task`` from here. Until #27
lands the module is dormant — no ``app.py`` registration required.

## Idempotency

``mirror_synapse_run(run_id, key)`` is safe to call multiple times for
the same ``run_id``. The implementation uses an in-process registry
(``_ACTIVE_MIRRORS``) keyed by ``run_id`` so a second call returns the
same task instead of opening a second SSE subscription (which would
double-write every event). This is the operator-iteration-case fix:
hitting Cancel + Resume in the dashboard shouldn't double-log the
re-subscribed events. The registry is process-local — it survives one
bridge process, NOT a bridge restart (cross-restart resumability rides
on Synapse's checkpoint, per the STAGING-README §4 operator decision).

## File layout

```
routines/runs/
├── composite.pitch.jsonl       # all runs of /pitch (append-only)
├── composite.teaser.jsonl
└── composite.ic_memo.jsonl
```

One file per composite key, not per run. Per-run filtering is via the
``run_id`` field on every record. Rationale: matches the existing
``runs/tool.*.jsonl`` convention (see ``routines/shared/audit.py``),
keeps `git ls-files` shorter, makes "show me every run of /pitch in the
last week" a single-file scan.

NOTE (promotion flag, 2026-06-09): mirror records intentionally carry NO
top-level ``status`` field, so the ``week_in_review`` collector skips
them; the ``daily_digest`` + ``telemetry.burn`` ``runs/*.jsonl``
glob-walkers will count mirror event rows as activity records (status
defaults to "ok"). Observability noise only — flagged as an operator
decision in the session brief.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from routines.api.deps import RUNS_DIR

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Defaults
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_SYNAPSE_BASE = "http://127.0.0.1:9100"

# Promoted: the bridge's canonical runs/ dir (routines/api/deps.py).
# Tests pass an explicit ``runs_dir=`` so nothing touches the real log.
DEFAULT_RUNS_DIR = RUNS_DIR

# Significant events the mirror always captures explicitly. Other events
# (informational chatter, step_started) are recorded under their own
# event types but don't trigger special handling.
_EXPLICIT_EVENTS = {
    "step_error",
    "human_input_required",
    "done",
    "orchestration_error",
}

# In-process registry of active mirror tasks keyed by Synapse run_id.
# Prevents double-subscription on retry / re-entry.
_ACTIVE_MIRRORS: dict[str, asyncio.Task[None]] = {}

# Strict allowlist for composite keys interpolated into audit file paths.
# (codex fix round 2026-06-10, BLOCKER — see ``_audit_path``.)
_KEY_ALLOWLIST_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ────────────────────────────────────────────────────────────────────────────
# Synchronous record helpers (called from the bridge route handlers
# BEFORE Synapse is hit — so they cover the mid-flight-cancel gap)
# ────────────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit_path(key: str, runs_dir: Path = DEFAULT_RUNS_DIR) -> Path:
    """Resolve the per-composite-key audit JSONL path.

    SECURITY (codex fix round 2026-06-10, BLOCKER): ``key`` reaches this
    function from HTTP-facing callers (the composite proxy routes), and is
    interpolated into a filesystem path — an embedded separator or ``..``
    component could escape the append-only ``runs/`` boundary. Two layers
    of defence, both required:

      1. **Allowlist** — ``key`` must match ``^[A-Za-z0-9_-]+$`` (no dots,
         no separators, no empty string).
      2. **Containment** — the final path is resolved and must sit
         directly inside the resolved ``runs_dir``.

    Raises :class:`ValueError` on violation; callers must treat that as
    abuse/misconfiguration, never as an I/O blip to retry.
    """
    if not _KEY_ALLOWLIST_RE.fullmatch(key):
        raise ValueError(
            f"audit_mirror: invalid composite key {key!r} — "
            "must match ^[A-Za-z0-9_-]+$"
        )
    runs_dir.mkdir(parents=True, exist_ok=True)
    base = runs_dir.resolve()
    path = (base / f"composite.{key}.jsonl").resolve()
    if path.parent != base:
        raise ValueError(
            f"audit_mirror: audit path for key {key!r} resolved outside "
            f"the runs dir ({path} not under {base})"
        )
    return path


def _append_record(
    key: str, record: dict[str, Any], runs_dir: Path = DEFAULT_RUNS_DIR
) -> None:
    """Append one JSONL record to ``composite.<key>.jsonl``.

    Synchronous + atomic-enough for append-only files. The route handler
    can call this before / after async SSE subscription without races —
    a single ``write()`` of a single line is atomic on POSIX + NTFS for
    payloads under PIPE_BUF (~4 KB), and our records are well under that.
    """
    path = _audit_path(key, runs_dir)
    line = json.dumps(record, default=str) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    logger.debug(
        "audit_mirror: appended %s event for run_id=%s key=%s",
        record.get("event"),
        record.get("run_id"),
        key,
    )


def record_run_started(
    run_id: str,
    key: str,
    *,
    inputs: dict[str, Any] | None = None,
    workspace: dict[str, str] | None = None,
    sensitivity: str = "confidential",
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> None:
    """Write a ``run_started`` record. Call BEFORE hitting Synapse.

    Covers the mid-flight-cancel-before-first-checkpoint gap from
    spike Item 6 — even if the cancel beats Synapse's first checkpoint
    write, this record proves the run was accepted and consumed budget.
    """
    _append_record(
        key,
        {
            "ts": _now_iso(),
            "event": "run_started",
            "run_id": run_id,
            "composite_key": key,
            "inputs": inputs or {},
            "workspace": workspace or {},
            "sensitivity": sensitivity,
        },
        runs_dir=runs_dir,
    )


def record_run_cancelled(
    run_id: str,
    key: str,
    *,
    reason: str | None = None,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> None:
    """Write a ``run_cancelled`` record. Call BEFORE hitting Synapse's
    cancel endpoint.

    Same rationale: covers the case where Synapse's cancel call itself
    fails (network blip, backend already crashed) — the ANTON-side
    audit still records the operator's intent to cancel.
    """
    _append_record(
        key,
        {
            "ts": _now_iso(),
            "event": "run_cancelled",
            "run_id": run_id,
            "composite_key": key,
            "reason": reason or "operator_cancel",
        },
        runs_dir=runs_dir,
    )


def record_run_error(
    run_id: str,
    key: str,
    *,
    error_class: str,
    error_message: str,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> None:
    """Write a terminal ``run_error`` record. Used when the bridge can't
    even subscribe to the SSE stream (e.g. Synapse is down)."""
    _append_record(
        key,
        {
            "ts": _now_iso(),
            "event": "run_error",
            "run_id": run_id,
            "composite_key": key,
            "error_class": error_class,
            "error_message": error_message,
        },
        runs_dir=runs_dir,
    )


# ────────────────────────────────────────────────────────────────────────────
# SSE subscription (async — runs in the bridge's event loop)
# ────────────────────────────────────────────────────────────────────────────


async def _iter_sse_events(
    client: httpx.AsyncClient, url: str
) -> AsyncIterator[dict[str, Any]]:
    """Iterate the SSE stream at ``url``, yielding parsed event dicts.

    Synapse's SSE stream uses standard ``data: <json>\\n\\n`` framing
    (see SYNAPSE-SPIKE-RESULTS Item 3 sample event shape). Each ``data:``
    line carries one JSON object; we yield those.
    """

    def _parse(raw: str) -> dict[str, Any] | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "audit_mirror: dropping malformed SSE event: %r",
                raw[:200],
            )
            return None

    async with client.stream("GET", url) as response:
        response.raise_for_status()
        buffer: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
            elif line == "":
                # Event boundary.
                if buffer:
                    raw = "\n".join(buffer)
                    buffer = []
                    parsed = _parse(raw)
                    if parsed is not None:
                        yield parsed
            # Other line prefixes (event:, id:, retry:) — ignore for now;
            # Synapse doesn't use them per the spike SSE samples.
        # CODEX FIX (2026-06-10 CONCERN 3): if the stream closes WITHOUT a
        # trailing blank line (server crash / disconnect mid-frame), the
        # last buffered event used to be silently dropped — and on
        # disconnects that buffered event is precisely the terminal/error
        # event we most need in the audit. Flush it.
        if buffer:
            parsed = _parse("\n".join(buffer))
            if parsed is not None:
                yield parsed


async def mirror_synapse_run(
    run_id: str,
    key: str,
    *,
    synapse_base: str = DEFAULT_SYNAPSE_BASE,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Subscribe to the Synapse SSE stream for ``run_id`` and mirror every
    event into ``composite.<key>.jsonl``.

    Call this as ``asyncio.create_task(mirror_synapse_run(...))`` from
    the bridge route handler that proxies ``/composite/run`` (or use
    :func:`start_mirror_task`, which also registers the task). The task
    runs for the lifetime of the SSE stream (typically until the
    ``done`` event arrives) — usually 10s to 15 min per composite.

    Idempotent on ``run_id``: a second call for the same run returns
    immediately if a mirror task is already active (so dashboard
    reconnects don't double-log).

    PROMOTION FIX (2026-06-09): the staged guard compared against
    ``_ACTIVE_MIRRORS[run_id]`` without exempting the CURRENT task — but
    ``start_mirror_task`` registers the task BEFORE it first runs, so the
    task body saw *itself* as "already active" and returned without ever
    subscribing: every mirror started via the documented fire-and-forget
    path recorded nothing. The guard now exempts ``asyncio.current_task()``
    and self-registers (covering direct ``asyncio.create_task`` callers
    too).
    """
    current = asyncio.current_task()
    existing = _ACTIVE_MIRRORS.get(run_id)
    if existing is not None and not existing.done() and existing is not current:
        logger.info(
            "audit_mirror: run_id=%s already being mirrored — skipping subscribe",
            run_id,
        )
        return
    if current is not None:
        # Self-register so a caller that used asyncio.create_task directly
        # (without start_mirror_task) still gets the idempotency guard.
        _ACTIVE_MIRRORS[run_id] = current

    sse_url = f"{synapse_base.rstrip('/')}/api/orchestrations/runs/{run_id}/events"
    logger.info("audit_mirror: subscribing to %s", sse_url)

    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(None, read=None))

    try:
        async for event in _iter_sse_events(client, sse_url):
            event_type = event.get("type") or "unknown"
            record = {
                "ts": _now_iso(),
                "event": event_type,
                "run_id": run_id,
                "composite_key": key,
                "payload": event,
            }
            # Promote critical event fields to top-level for query ergonomics.
            if event_type == "step_error":
                record["step_id"] = event.get("orch_step_id")
                record["error_message"] = event.get("error") or event.get("message")
            elif event_type == "human_input_required":
                record["step_id"] = event.get("orch_step_id")
                record["prompt"] = event.get("prompt")
            elif event_type == "orchestration_error":
                record["error_message"] = event.get("error") or event.get("message")

            _append_record(key, record, runs_dir=runs_dir)

            # Terminal events end the subscription.
            if event_type in ("done", "orchestration_error"):
                logger.info(
                    "audit_mirror: terminal event %r for run_id=%s — closing",
                    event_type,
                    run_id,
                )
                break
    except httpx.HTTPError as e:
        # SSE stream broke — record the failure so we don't pretend
        # the run is still in flight.
        logger.warning(
            "audit_mirror: SSE stream failed for run_id=%s: %s", run_id, e
        )
        record_run_error(
            run_id,
            key,
            error_class=type(e).__name__,
            error_message=str(e),
            runs_dir=runs_dir,
        )
    except Exception as e:  # noqa: BLE001 — audit completeness over purity
        # CODEX FIX (2026-06-10 CONCERN 4): non-HTTP failures (JSON decode,
        # append/IO, parser bugs) used to tear the background task down with
        # NO ``run_error`` row — the run looked in-flight forever in the
        # audit. Record the terminal row (best-effort), then RE-RAISE so the
        # bug still surfaces to awaiting callers / asyncio's unhandled-task
        # logging instead of being swallowed like the expected network case
        # above. ``asyncio.CancelledError`` is ``BaseException`` on 3.8+ and
        # intentionally passes through untouched.
        logger.exception(
            "audit_mirror: mirror task failed for run_id=%s key=%s",
            run_id,
            key,
        )
        try:
            record_run_error(
                run_id,
                key,
                error_class=type(e).__name__,
                error_message=str(e),
                runs_dir=runs_dir,
            )
        except Exception:  # noqa: BLE001 — best-effort terminal row
            logger.exception(
                "audit_mirror: failed to write run_error row for run_id=%s",
                run_id,
            )
        raise
    finally:
        if own_client:
            await client.aclose()
        # Deregister only our own entry — never pop a successor mirror that
        # re-registered while we were tearing down. (``current`` is None only
        # outside a running task, where nothing was self-registered.)
        if current is None or _ACTIVE_MIRRORS.get(run_id) is current:
            _ACTIVE_MIRRORS.pop(run_id, None)


def start_mirror_task(
    run_id: str,
    key: str,
    *,
    synapse_base: str = DEFAULT_SYNAPSE_BASE,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> asyncio.Task[None]:
    """Spawn ``mirror_synapse_run`` as a background task + register it.

    Returns the task so the caller can await / cancel if needed.
    Typically the bridge route fires-and-forgets.
    """
    if run_id in _ACTIVE_MIRRORS and not _ACTIVE_MIRRORS[run_id].done():
        return _ACTIVE_MIRRORS[run_id]
    task = asyncio.create_task(
        mirror_synapse_run(
            run_id, key, synapse_base=synapse_base, runs_dir=runs_dir
        ),
        name=f"audit_mirror[{run_id}]",
    )
    _ACTIVE_MIRRORS[run_id] = task
    return task


# ────────────────────────────────────────────────────────────────────────────
# Read-side helpers (for /api/composite/runs endpoint, dashboard, tests)
# ────────────────────────────────────────────────────────────────────────────


def read_run_events(
    run_id: str, key: str, *, runs_dir: Path = DEFAULT_RUNS_DIR
) -> list[dict[str, Any]]:
    """Return every JSONL record for a given run_id (filtered from the
    per-key log). O(N) over the per-key log file — fine for the
    ``last 20 runs`` dashboard surface; if it ever needs scale, indexing
    by run_id can land later."""
    path = _audit_path(key, runs_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id:
                out.append(rec)
    return out


def active_mirror_run_ids() -> list[str]:
    """For debugging: which mirror tasks are currently subscribed."""
    return [r for r, t in _ACTIVE_MIRRORS.items() if not t.done()]


__all__ = [
    "DEFAULT_SYNAPSE_BASE",
    "DEFAULT_RUNS_DIR",
    "record_run_started",
    "record_run_cancelled",
    "record_run_error",
    "mirror_synapse_run",
    "start_mirror_task",
    "read_run_events",
    "active_mirror_run_ids",
]
