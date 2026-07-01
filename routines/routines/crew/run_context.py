"""In-memory registry of LIVE crew runs' LLM-routing context
(#crew-cloud-promotion, Phase A).

This is the AUTHORITATIVE server-side source the loopback ``/api/crew/_llm``
endpoint re-derives a promoted call's lane / model / sensitivity from — it never
trusts the values a subprocess sends. The crew subprocess authenticates by
echoing its ``ANTON_CREW_RUN_ID`` (the run_id the bridge handed it on stdin/env);
the endpoint serves a call ONLY for a run_id that is (a) registered here and
(b) still live.

``run_crew`` registers a context immediately before launching the subprocess;
the worker thread drops it in a ``finally`` when the run ends, and a TTL reaper
backstops a run that dies without cleanup — mirroring the SSE event-queue TTL
backstop in ``crew.py``. The trust model: the run_id is an 8-char audit id
minted server-side and never leaves the bridge↔subprocess channel, so possessing
it == being that subprocess.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass, field

from routines.crew.overrides import RolePromotion

logger = logging.getLogger(__name__)

_DEFAULT_BRIDGE_LLM_URL = "http://127.0.0.1:8765/api/crew/_llm"
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_url(url: str) -> bool:
    """True only for an ``http(s)`` URL whose host is a loopback address — the
    bridge endpoint a promoted crew may POST to is loopback BY DESIGN, so a
    promoted role's prompt never leaves the box and never bypasses the gate."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.hostname in _LOOPBACK_HOSTNAMES


def bridge_llm_url() -> str:
    """The loopback URL a promoted crew subprocess POSTs its LLM calls to.

    Operator-overridable via ``ANTON_BRIDGE_LLM_URL`` (the bridge's own
    host:port may differ) — but FAIL-CLOSED: a non-loopback override is refused
    (logged) and the default loopback endpoint is used instead. A promoted crew
    must never be pointed off-box, which would both leak the prompt and bypass
    the ``/api/crew/_llm`` sensitivity/budget gates."""
    override = os.environ.get("ANTON_BRIDGE_LLM_URL")
    if override:
        if _is_loopback_url(override):
            return override
        logger.error(
            "ANTON_BRIDGE_LLM_URL=%r is not a loopback http(s) URL — refusing it "
            "and using the default %s (a promoted crew must POST only to the "
            "local gated endpoint)", override, _DEFAULT_BRIDGE_LLM_URL,
        )
    return _DEFAULT_BRIDGE_LLM_URL


@dataclass(frozen=True)
class RunLLMContext:
    """The server-side LLM-routing facts for one live crew run.

    ``cloud_roles`` is the authoritative per-role promotion (lane + cloud model)
    the bridge resolved at launch; the ``_llm`` endpoint serves a role ONLY if
    it is present here, with EXACTLY this lane/model — a subprocess cannot widen
    its own access. ``sensitivity`` is the run's resolved tier (the gate's input);
    ``mnpi_explicit`` carries the operator-assigned-MNPI provenance for the P5
    path (Phases C; pinned False in Phase A)."""

    run_id: str
    verb: str
    sensitivity: str
    workspace_type: str
    workspace_name: str
    cost_cap_tokens: int
    cloud_roles: dict[str, RolePromotion] = field(default_factory=dict)
    mnpi_explicit: bool = False


_runs: dict[str, RunLLMContext] = {}
_timers: dict[str, threading.Timer] = {}
_lock = threading.Lock()


def register(ctx: RunLLMContext, *, ttl_s: float) -> None:
    """Record a live run's context and schedule a TTL-reaper drop.

    Idempotent on run_id (a re-register replaces the context AND cancels the
    prior timer, so timers can't pile up). The reaper is a daemon Timer so it
    never blocks shutdown; ``drop`` is also called explicitly by the worker
    thread on completion, which cancels this timer."""
    timer = threading.Timer(max(1.0, ttl_s), drop, args=(ctx.run_id,))
    timer.daemon = True
    with _lock:
        _runs[ctx.run_id] = ctx
        old = _timers.get(ctx.run_id)
        _timers[ctx.run_id] = timer
    if old is not None:
        old.cancel()
    timer.start()


def get(run_id: str) -> RunLLMContext | None:
    with _lock:
        return _runs.get(run_id)


def drop(run_id: str) -> None:
    """Forget a run's context and cancel its pending TTL timer. Idempotent;
    safe to call from the timer itself (cancelling an already-fired timer is a
    no-op)."""
    with _lock:
        _runs.pop(run_id, None)
        timer = _timers.pop(run_id, None)
    if timer is not None:
        timer.cancel()


def _clear() -> None:
    """Test-only: forget every registered run + cancel all timers."""
    with _lock:
        timers = list(_timers.values())
        _runs.clear()
        _timers.clear()
    for t in timers:
        t.cancel()


__all__ = [
    "RunLLMContext",
    "bridge_llm_url",
    "register",
    "get",
    "drop",
]
