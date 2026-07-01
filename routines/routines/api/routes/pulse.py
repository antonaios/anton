"""GET /api/vault-pulse — recent file mtimes under the vault."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from routines.api.deps import VAULT

router = APIRouter()

SKIP_DIRS = {".git", ".obsidian", ".smart-env", ".recall-index"}

# ── short-TTL cache (#eff-hotpath-batch) ─────────────────────────────────────
# BEFORE: every poll did a full ``VAULT.rglob("*.md")`` + ``stat()`` on every
# file — O(all vault .md) per request, and the dashboard polls this panel.
# AFTER: the computed pulse is cached for ``_PULSE_TTL_SECONDS`` keyed by
# (vault root, hours, limit). The pulse is a "recent activity" panel, so a few
# seconds of staleness is acceptable — the TTL collapses a burst of dashboard
# polls into one rglob. Process-local; the lock keeps the cache dict consistent
# under concurrent requests (handlers run in a threadpool).
_PULSE_TTL_SECONDS = 60.0
_pulse_cache: dict[tuple[str, int, int], tuple[float, "PulseResponse"]] = {}
_pulse_cache_lock = threading.Lock()


class ActivityItem(BaseModel):
    path: str
    ago: str
    kind: Literal["CREATED", "UPDATED"]


class PulseResponse(BaseModel):
    items: list[ActivityItem]


def _ago(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


@router.get("/vault-pulse", response_model=PulseResponse)
def vault_pulse(
    hours: int = Query(default=24, ge=1, le=24 * 14),
    limit: int = Query(default=5, ge=1, le=50),
) -> PulseResponse:
    # Re-read VAULT off the live module so tests that monkeypatch
    # ``routines.api.routes.pulse.VAULT`` (or deps.VAULT) take effect.
    from routines.api.routes import pulse as _self
    vault = _self.VAULT

    cache_key = (str(vault), hours, limit)
    now_mono = time.monotonic()
    cached = _pulse_cache.get(cache_key)
    if cached is not None and (now_mono - cached[0]) < _PULSE_TTL_SECONDS:
        return cached[1]

    response = _compute_pulse(vault, hours, limit)
    with _pulse_cache_lock:
        _pulse_cache[cache_key] = (now_mono, response)
    return response


def _compute_pulse(vault, hours: int, limit: int) -> PulseResponse:
    """Full ``rglob`` of the vault for recently-touched .md files. Uncached —
    the route wraps this in the short-TTL cache above."""
    if not vault.exists():
        return PulseResponse(items=[])

    cutoff = datetime.now().timestamp() - (hours * 3600)
    hits: list[tuple[float, str]] = []

    for path in vault.rglob("*.md"):
        if any(d in path.parts for d in SKIP_DIRS):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            try:
                rel = path.relative_to(vault).as_posix()
            except ValueError:
                rel = str(path)
            hits.append((mtime, rel))

    hits.sort(reverse=True)
    now = datetime.now().timestamp()
    items: list[ActivityItem] = []
    for t, rel in hits[:limit]:
        is_new = (now - t) < 300
        items.append(
            ActivityItem(
                path=rel,
                ago=_ago(now - t),
                kind="CREATED" if is_new else "UPDATED",
            )
        )
    return PulseResponse(items=items)
