"""#59 — X-ANTON-Run-Id header middleware.

Every state-mutating POST (and every other request, for symmetry on the
read side) carries a correlation id. The client mints a UUID per logical
action and includes it on retries; if absent the middleware mints one
server-side. The id is bound to the canonical ``anton_run_id`` ContextVar
at [[llm_call_counter]] (shipped by #67) so downstream code — audit
rows, the per-skill ``llm_calls`` counter, the per-session coalescing
lock — reads the same value via ``current_run_id()``.

Pairs with ``session_lock.py``: the lock acquire/release uses this
ContextVar's value to detect same-id retries vs. genuinely concurrent
runs on the same session_id.

HARNESS half (dashboard ``lib/api.ts`` minting UUIDs per logical action +
reusing on retry) is the follow-on. Until that lands, this middleware
still mints server-side so the response always carries an id — the
deduplication benefit is reduced (each client retry looks like a fresh
run_id) but the 409-on-double-fire behaviour is in place for any client
that DOES retry with the same id.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from routines.skills._runtime.llm_call_counter import bind_run_id

RUN_ID_HEADER = "X-ANTON-Run-Id"


def _mint_run_id() -> str:
    """Full UUID4 (36 chars). The audit module's 8-char id is for legacy
    audit rows; #59 standardises on full UUIDs at the request boundary so
    the dashboard's per-logical-action id (#59-harness, deferred) can be
    a plain ``crypto.randomUUID()`` without truncation rules."""
    return str(uuid.uuid4())


def _coerce_run_id(raw: str | None) -> str:
    """Accept the client-supplied run-id ONLY if it is a well-formed UUID;
    otherwise mint one server-side (F-13 / HR S-16).

    The run-id is load-bearing for SECURITY-relevant keys: the per-session
    coalescing lock owner, the #67 per-skill ``llm_calls`` counter, and the
    suspend/resume claim. Trusting an arbitrary caller string verbatim let a
    caller COLLIDE with or HIJACK those keys (e.g. supply another run's id to
    force a 409, or a crafted value that aliases an internal key). Constrain
    the client value to a canonical RFC-4122 UUID — unguessable and
    collision-safe — and normalise its encoding (braces / upper-case / ``urn:``
    forms all reduce to one key, so equivalent encodings can't dodge dedup).
    The #59 retry-dedup contract is preserved: a well-behaved client minting a
    stable ``crypto.randomUUID()`` per logical action still round-trips."""
    if not raw:
        return _mint_run_id()
    candidate = raw.strip()
    # Bound the length before parsing (defensive against a pathological header).
    if not candidate or len(candidate) > 200:
        return _mint_run_id()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError, TypeError):
        return _mint_run_id()
    # Require a RANDOM (v4) RFC-4122 UUID — reject the nil UUID and other
    # caller-choosable constants (``00000000-…``, deterministic v3/v5, etc.)
    # that ``uuid.UUID()`` would otherwise accept. Only an unguessable random id
    # gives the collision-safety the security keys rely on (codex-5.5 F-13 r1).
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        return _mint_run_id()
    return str(parsed)


class RunIdMiddleware(BaseHTTPMiddleware):
    """Read-validate or mint X-ANTON-Run-Id; bind to ContextVar; echo on response.

    Header name is case-insensitive on read (Starlette ``Headers``
    normalises) and emitted as ``X-ANTON-Run-Id`` on write — this is the
    canonical casing for grep + the deferred dashboard client. A client value
    is accepted only if it is a canonical UUID (F-13); otherwise it is replaced
    with a server-minted one.
    """

    async def dispatch(self, request: Request, call_next):
        run_id = _coerce_run_id(request.headers.get(RUN_ID_HEADER))

        # bind_run_id is a sync contextmanager — safe to use in an async
        # function. It calls ContextVar.set/reset which are sync ops.
        # Starlette + anyio propagate the ContextVar to sync route
        # handlers via run_in_threadpool (anyio copies the context per
        # worker-thread invocation).
        with bind_run_id(run_id):
            response: Response = await call_next(request)

        response.headers[RUN_ID_HEADER] = run_id
        return response


__all__ = ["RUN_ID_HEADER", "RunIdMiddleware"]
