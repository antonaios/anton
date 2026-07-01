"""Durable cron suspension — persisted paused-jobs set.

The cron registry lives in the **ephemeral** jobstore: every bridge boot
re-registers every spec ACTIVE, so an APScheduler pause made via
``POST /api/scheduler/jobs/{id}/pause`` silently lifts at the next
restart. That bit the operator on 2026-06-10 — two bridge restarts
re-armed crons he believed suspended. This module is the durability
layer: a JSON-persisted set of suspended job ids that
``register_all_jobs()`` consults at registration time and that the
pause/resume endpoints keep in sync.

File: ``<state>/scheduler-paused.json`` — same directory resolution as
``schedules.db`` (``AGENTIC_SCHEDULER_STATE_DIR`` env override, else
``<routines-repo>/state``; resolved per-call so test monkeypatching
works). Format::

    {"version": 1, "paused": ["daily-digest", "sector-news"]}

Scope: only ids from the cron spec registry are persisted (the route
layer enforces this). One-shot ``-catchup`` twins are never persisted —
suspension of the base spec suppresses its catch-up at boot, and a
pending live twin is removed by the pause endpoint. Jobs in the
SQLAlchemy ``default`` jobstore need no entry here: APScheduler persists
their ``next_run_time=None`` in the DB row, so their pause is already
durable.

Failure posture: a missing file is the normal empty state; an unreadable
or malformed file logs at ERROR and is treated as empty (we cannot
recover ids from garbage) — loud, so a hand-edit that breaks the JSON
doesn't silently re-arm a suspended fleet. Writes are atomic
(tmp + ``os.replace``) and serialised under a module lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from routines.scheduler.scheduler import _default_state_dir

logger = logging.getLogger(__name__)

SUSPENSION_FILENAME = "scheduler-paused.json"
_FORMAT_VERSION = 1

# Serialises read-modify-write cycles across endpoint threads. Cross-process
# races aren't a concern — only the bridge process writes this file.
_io_lock = threading.Lock()


def suspension_path() -> Path:
    """Resolve the suspension file path (per-call, honours the env override)."""
    return _default_state_dir() / SUSPENSION_FILENAME


def load_suspended() -> frozenset[str]:
    """Read the persisted suspended-job-id set.

    Missing file → empty set (normal state — only ``FileNotFoundError``
    qualifies). Any other unreadable state (permissions, a directory
    squatting on the path) or malformed/wrong-version content → ERROR
    log + empty set; never raises, so a corrupt file can't take the
    bridge lifespan down."""
    path = suspension_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError as e:
        _log_unreadable(path, e)
        return frozenset()
    try:
        data = json.loads(raw)
        version = data.get("version")
        if version != _FORMAT_VERSION:
            raise ValueError(f"unsupported version {version!r} (expected {_FORMAT_VERSION})")
        paused = data["paused"]
        if not isinstance(paused, list) or not all(isinstance(j, str) for j in paused):
            raise ValueError(f"'paused' must be a list of strings, got {type(paused).__name__}")
        return frozenset(paused)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        _log_unreadable(path, e)
        return frozenset()


def _log_unreadable(path: Path, e: Exception) -> None:
    logger.error(
        "scheduler suspension: %s is unreadable (%s: %s) — treating as EMPTY; "
        "any durably-paused cron will RE-ARM on this boot. Fix or delete the file.",
        path, type(e).__name__, e,
    )


def _write(paused: frozenset[str]) -> None:
    """Atomic write: tmp sibling + os.replace. Caller holds ``_io_lock``."""
    path = suspension_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(
        {"version": _FORMAT_VERSION, "paused": sorted(paused)},
        indent=2,
    )
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def suspend(job_id: str) -> frozenset[str]:
    """Add ``job_id`` to the persisted set. Idempotent. Returns the new set.

    Raises ``OSError`` on write failure — callers surface that loudly
    rather than let a pause silently stay ephemeral."""
    with _io_lock:
        paused = load_suspended() | {job_id}
        _write(paused)
    logger.info("scheduler suspension: %r persisted as paused (set=%s)", job_id, sorted(paused))
    return paused


def unsuspend(job_id: str) -> frozenset[str]:
    """Remove ``job_id`` from the persisted set. Idempotent. Returns the new set."""
    with _io_lock:
        paused = load_suspended() - {job_id}
        _write(paused)
    logger.info("scheduler suspension: %r removed from paused set (set=%s)", job_id, sorted(paused))
    return paused


__all__ = [
    "SUSPENSION_FILENAME",
    "suspension_path",
    "load_suspended",
    "suspend",
    "unsuspend",
]
