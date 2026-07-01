"""Minimal in-process token-bucket rate limiter (Shannon run #2, AUTH-VULN-03).

Bounds the rate of the operator-confirmation CHALLENGE endpoints
(``/api/mnpi/attestations/challenge`` + ``/api/sensitivity/overrides/challenge``)
so a local flood can't churn the single-use nonce pool (cap 256, TTL 120s)
faster than the operator consumes a nonce — closing the nonce-pool-exhaustion
DoS Shannon run #2 flagged. The challenge endpoints are CSRF-callable BY DESIGN
(the nonce is the F-8 second wall: a cross-origin caller can mint but can't read
the response), so the nonce pool's newest-kept eviction already protects a
just-minted operator nonce within its short mint→grant window; this rate limit
removes the remaining "flood the pool" abuse by capping issuance to a human pace.

In-process + one lock (the bridge is single-process) — same model as the nonce
pool and ``job_registry``. The guarded endpoints are loopback-only and the
platform is single-operator, so one global bucket per named endpoint is the
right granularity (per-IP keying would be moot on a 127.0.0.1-only bind).

``now`` is injectable so tests are deterministic (mirrors ``_issue_nonce``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last: float  # monotonic seconds of the last refill


_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def allow(
    bucket: str,
    *,
    capacity: float,
    refill_per_sec: float,
    now: float | None = None,
) -> bool:
    """Token-bucket admission for ``bucket``.

    Returns True when a token was available (and consumes one), False when the
    bucket is empty — the caller maps False to HTTP 429. A bucket starts FULL
    (``capacity`` tokens) on first use, so the first ``capacity`` calls in a
    burst pass, then admission settles to ``refill_per_sec`` sustained. Tokens
    refill continuously by elapsed wall-clock time, capped at ``capacity``.
    """
    now = time.monotonic() if now is None else now
    with _lock:
        b = _buckets.get(bucket)
        if b is None:
            _buckets[bucket] = _Bucket(tokens=capacity - 1.0, last=now)
            return True
        elapsed = now - b.last
        if elapsed > 0:
            b.tokens = min(capacity, b.tokens + elapsed * refill_per_sec)
            b.last = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False


def _reset_for_tests() -> None:
    with _lock:
        _buckets.clear()


__all__ = ["allow", "_reset_for_tests"]
