"""ContextVar-scoped per-``run_id`` LLM-call counter (#67).

Why a ``ContextVar``: chat dispatch (and any future skill dispatcher) is
async. ``threading.local`` doesn't survive an ``await``; ``ContextVar``
does — asyncio.Task creation copies the current context, so a hook fired
deep inside an awaited LLM call still reads the ``run_id`` set at the
dispatch entry.

Why a thread-safe ``dict`` for the count: the bridge is single-process,
but the FastAPI thread-pool (worker threads for sync route handlers) can
land multiple in-flight runs on the same module. The ``Lock`` makes
``increment / reset / count_for`` atomic without contention beyond a
single-process scale.

A run_id of ``None`` (no skill dispatcher set one) is the **chat lane** —
calls pass ungoverned by this counter. The #57 budget gate still applies
on top (cost-safety) — that's the load-bearing guard for chat. The
``llm_calls`` cap is the per-skill defence-in-depth.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_run_id_var: ContextVar[Optional[str]] = ContextVar("anton_run_id", default=None)
_counts: dict[str, int] = {}
# #74.5 per-tool sub-caps: a SECOND counter keyed by ``(run_id, tool_name)`` so a
# runaway ``vault_read`` loop is bounded SEPARATELY from the LLM-call cap. Shares
# the same ``run_id`` (the ContextVar set by ``skill_run``) and the same ``_lock``;
# ``reset(run_id)`` clears both so the single ``skill_run`` exit path stays correct.
_tool_counts: dict[tuple[str, str], int] = {}
_lock = threading.Lock()


def set_run_id(run_id: str) -> None:
    """Bind ``run_id`` to the current async / thread context.

    Idempotent for the same value — re-setting the same ``run_id`` is a
    no-op for the counter (no reset, no double-counting). To start a
    fresh accounting window, ``reset()`` the prior id explicitly."""
    _run_id_var.set(run_id)


def current_run_id() -> Optional[str]:
    """Return the run_id currently bound to this context, or ``None`` when
    the call originates from an ungoverned lane (chat)."""
    return _run_id_var.get()


def increment_for_current_run() -> int:
    """Increment the LLM-call counter for the current run_id.

    Returns the new count (post-increment). When no run_id is bound,
    returns ``0`` and does nothing — chat lane calls don't accrue against
    a per-skill cap."""
    rid = current_run_id()
    if rid is None:
        return 0
    with _lock:
        _counts[rid] = _counts.get(rid, 0) + 1
        return _counts[rid]


def count_for(run_id: str) -> int:
    """Read the current count for ``run_id``. Returns 0 if unknown."""
    with _lock:
        return _counts.get(run_id, 0)


def increment_tool_for_current_run(tool_name: str) -> int:
    """Increment the per-``(run_id, tool_name)`` counter for the current run
    (#74.5 per-tool sub-caps).

    Returns the new count (post-increment). When no run_id is bound (chat
    lane), returns ``0`` and does nothing — tool calls outside a skill
    dispatch don't accrue against a per-skill per-tool cap."""
    rid = current_run_id()
    if rid is None:
        return 0
    with _lock:
        key = (rid, tool_name)
        _tool_counts[key] = _tool_counts.get(key, 0) + 1
        return _tool_counts[key]


def tool_count_for(run_id: str, tool_name: str) -> int:
    """Read the current per-tool count for ``(run_id, tool_name)``. 0 if
    unknown (#74.5)."""
    with _lock:
        return _tool_counts.get((run_id, tool_name), 0)


def check_and_increment_tool(
    tool_name: str, cap: Optional[int]
) -> tuple[bool, int]:
    """Atomically test-and-increment the per-tool counter for the current run
    (#74.5). ONE critical section — no check-then-act race.

    Computes ``attempted = current + 1``. If ``cap`` is not None and
    ``attempted > cap`` → returns ``(False, attempted)`` WITHOUT incrementing
    (the caller raises; a blocked call must not consume budget). Otherwise
    increments and returns ``(True, attempted)``.

    Returns ``(True, 0)`` when no run_id is bound (chat lane — ungoverned).

    The read-and-increment MUST be one ``_lock`` acquisition: doing
    ``tool_count_for() + 1`` then ``increment_tool_for_current_run()`` as two
    separate lock takes lets two concurrent same-``(run_id, tool)`` calls both
    observe the same count and both pass, exceeding ``cap``."""
    rid = current_run_id()
    if rid is None:
        return (True, 0)
    with _lock:
        key = (rid, tool_name)
        attempted = _tool_counts.get(key, 0) + 1
        if cap is not None and attempted > cap:
            return (False, attempted)
        _tool_counts[key] = attempted
        return (True, attempted)


def reset(run_id: str) -> None:
    """Drop ALL counters for ``run_id`` — both the LLM-call counter and every
    per-tool counter (#74.5). Call from ``finally`` blocks at skill-dispatch
    exit so the dicts don't grow unboundedly."""
    with _lock:
        _counts.pop(run_id, None)
        # Clear every (run_id, *) per-tool entry for this run.
        for key in [k for k in _tool_counts if k[0] == run_id]:
            _tool_counts.pop(key, None)


@contextmanager
def skill_run(run_id: Optional[str] = None) -> Iterator[str]:
    """Scope a fresh ``run_id`` for the duration of a skill dispatch.

    Usage::

        with skill_run() as rid:
            result = dispatch_skill(...)

    Generates a UUID if ``run_id`` is omitted. Sets the ContextVar at
    entry, resets at exit (even on exception). Survives async boundaries
    via ContextVar task-copy semantics — start a task inside this block
    and the task still sees the same ``rid``.

    Yields the run_id so the caller can correlate logs / audit / #59
    headers downstream when those land."""
    rid = run_id or str(uuid.uuid4())
    token = _run_id_var.set(rid)
    try:
        yield rid
    finally:
        _run_id_var.reset(token)
        reset(rid)


@contextmanager
def bind_run_id(run_id: str) -> Iterator[str]:
    """Bind ``run_id`` to the ContextVar for the duration of the block —
    WITHOUT touching the counter.

    Distinction from ``skill_run``: ``skill_run`` owns a counter window
    (mints id if absent, resets the counter dict on exit). ``bind_run_id``
    is for request-boundary binders (#59 X-ANTON-Run-Id middleware) that
    just need ``current_run_id()`` to return the right value inside the
    request; the counter dict belongs to inner ``skill_run`` scopes.

    Two scopes can nest cleanly: the request-level ``bind_run_id`` sets
    the id; an inner ``skill_run(current_run_id())`` reuses it and owns
    the counter. ContextVar.set is stack-based so the token-reset on
    skill_run exit restores the middleware's binding, not None.
    """
    token = _run_id_var.set(run_id)
    try:
        yield run_id
    finally:
        _run_id_var.reset(token)


__all__ = [
    "bind_run_id",
    "check_and_increment_tool",
    "count_for",
    "current_run_id",
    "increment_for_current_run",
    "increment_tool_for_current_run",
    "reset",
    "set_run_id",
    "skill_run",
    "tool_count_for",
]
