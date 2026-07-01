"""SQLite TTL cache for provider calls.

Without this, the dashboard's SparkTicker (6 symbols, refresh on focus
+ background pulse) would burn ~360 OpenBB calls/hour while you stare at
the screen. With it, 6 calls/min refreshes are absorbed.

Cache key is (function_name, json(args)). Values are JSON-encoded via a
typed envelope (``_encode`` / ``_decode``) so the cached value round-trips
back to its original type — a ``Quote`` in, a ``Quote`` out.

#ops-cache-json: the value format used to be ``pickle``. ``pickle.loads``
on a writable on-disk cache file is an arbitrary-code-execution sink — a
local attacker who can write ``cache.db`` gets code-exec the next time the
cache is read. JSON has no such sink. The trade-off is that JSON can't
serialise arbitrary objects, so the providers' Pydantic DTOs are encoded
as a ``{"__t": "model", ...}`` envelope and reconstructed on read against a
registry of the known markets types. Anything that doesn't decode cleanly
(an old pickled row, a truncated write, a hand-tampered blob) is treated as
a CACHE MISS and refetched — no ``pickle.loads`` path remains reachable.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from routines.markets.types import (
    CompRow, CompsResult, EquityResearchResult, EquityResearchSnapshot,
    Fundamentals, FundamentalsRatios, FundamentalsYear, MacroBarResponse,
    MacroRow, NewsItem, NewsResult, PeerItem, PeersResult, Quote,
)

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".markets-cache"
CACHE_DB = CACHE_DIR / "cache.db"

# Bump when the on-disk value format changes incompatibly. A stored envelope
# whose ``__v`` doesn't match (or which isn't a recognised envelope at all —
# e.g. a legacy pickle blob) is treated as a miss and refetched.
SCHEMA_VERSION = 1

_F = TypeVar("_F", bound=Callable[..., Any])

# Registry of the Pydantic DTOs that flow through the cache. The class's
# ``__name__`` is stored in the envelope; decode looks the class up here.
# Adding a new cached DTO type means adding it here — on the WRITE side an
# unregistered model is refused (``_encode`` raises, the write is skipped +
# logged) so a never-cacheable type is loud, not a silent recurring miss; on
# the READ side an unknown class name decodes to a cache miss (safe: refetch),
# never an arbitrary import. (Names are unique across the markets types, so a
# bare ``__name__`` key is unambiguous.)
_MODEL_REGISTRY: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        Quote, Fundamentals, FundamentalsYear, FundamentalsRatios,
        NewsItem, NewsResult, PeerItem, PeersResult,
        CompRow, CompsResult, EquityResearchSnapshot, EquityResearchResult,
        MacroRow, MacroBarResponse,
    )
}


class _CacheDecodeError(Exception):
    """Raised when a stored value can't be decoded into its original type.

    Callers treat this as a cache miss (refetch). It is deliberately distinct
    from ``json.JSONDecodeError`` so an unexpected JSON shape (e.g. an old
    pickle blob that happens to be valid-ish bytes) is funnelled through the
    same miss path rather than crashing.
    """


def _connect() -> sqlite3.Connection:
    """Open a hardened connection to the markets TTL cache.

    REL-1 / #ops-sqlite-wal: the dashboard's SparkTicker refreshes from a
    threadpool while APScheduler background pulses fire on their own
    threads, so two writers can collide on cache.db. WAL + busy_timeout
    turn an immediate ``database is locked`` into a brief wait. Each call
    site keeps its existing ``with sqlite3.connect(...) as conn:`` context
    manager (implicit transaction committed on exit), so isolation_level is
    left at the sqlite3 default. Mirrors sessions/store.py.
    """
    conn = sqlite3.connect(str(CACHE_DB))
    # busy_timeout FIRST so the journal_mode=WAL pragma itself waits rather than
    # hitting an immediate ``database is locked`` if a fresh connection races
    # while WAL is being established (codex round).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )


# ── value (de)serialization ──────────────────────────────────────────────
#
# JSON can't carry Pydantic models or tuples losslessly, so we wrap the value
# in a typed envelope that records how to rebuild it. Plain JSON primitives
# (str / int / float / bool / None / dict) pass through untouched; lists and
# tuples recurse (tuple-ness preserved so ``market_cap_and_shares`` decodes
# back to a tuple, not a list); BaseModels become a ``model`` envelope tagged
# with their class name.


def _encode(value: Any) -> Any:
    """Turn a cached value into a JSON-serialisable, round-trippable shape."""
    if isinstance(value, BaseModel):
        cls_name = type(value).__name__
        # Identity check, not just name membership: a foreign class that merely
        # shares a registered DTO's __name__ (e.g. an unrelated ``Quote``) must
        # NOT be written as that name — on read it would rehydrate as the WRONG
        # registered DTO if shape-compatible. Refuse anything that isn't the
        # exact registered class. An unregistered/foreign model would otherwise
        # decode to a permanent cache miss; refusing at write time makes the gap
        # loud (logged once via the write path) rather than silent + recurring.
        if _MODEL_REGISTRY.get(cls_name) is not type(value):
            raise TypeError(
                f"markets cache: {cls_name} is not the registered _MODEL_REGISTRY "
                "class for that name; add it there to make this type cacheable"
            )
        return {
            "__t": "model",
            "__cls": cls_name,
            # mode="json" so nested datetimes / enums become primitives, and a
            # symmetric ``model_validate`` rebuilds the exact model on decode.
            "v": value.model_dump(mode="json"),
        }
    if isinstance(value, tuple):
        return {"__t": "tuple", "v": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        # Re-key defensively: a dict whose keys collide with our envelope tags
        # is vanishingly unlikely (cache values are provider DTOs/primitives),
        # but recurse into values regardless to handle nested models.
        return {k: _encode(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Anything else (an un-registered object) is not JSON-round-trippable. Raise
    # so the write path logs + skips rather than persisting a lossy blob.
    raise TypeError(f"markets cache: cannot JSON-encode value of type {type(value)!r}")


def _decode(node: Any) -> Any:
    """Inverse of ``_encode``. Raises ``_CacheDecodeError`` on any unknown or
    malformed envelope so the read path treats it as a miss."""
    if isinstance(node, dict):
        tag = node.get("__t")
        if tag == "model":
            cls_name = node.get("__cls")
            cls = _MODEL_REGISTRY.get(cls_name) if isinstance(cls_name, str) else None
            if cls is None:
                raise _CacheDecodeError(f"unknown cached model class {cls_name!r}")
            try:
                return cls.model_validate(node["v"])
            except Exception as e:  # noqa: BLE001 — any validation failure → miss
                raise _CacheDecodeError(f"model_validate({cls_name}) failed: {e}") from e
        if tag == "tuple":
            raw = node.get("v")
            if not isinstance(raw, list):
                raise _CacheDecodeError("malformed tuple envelope")
            return tuple(_decode(v) for v in raw)
        if tag is not None:
            raise _CacheDecodeError(f"unknown envelope tag {tag!r}")
        return {k: _decode(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decode(v) for v in node]
    return node


def _serialize(value: Any) -> bytes:
    """Encode a value to a versioned JSON blob for on-disk storage."""
    payload = {"__v": SCHEMA_VERSION, "value": _encode(value)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _deserialize(blob: Any) -> Any:
    """Decode an on-disk blob. Raises ``_CacheDecodeError`` for an old pickle
    row, a wrong schema version, malformed JSON, a missing value, or any value
    that fails to rebuild — every one of those is a cache miss, never a crash,
    never a ``pickle.loads``.

    The whole parse+decode path is wrapped so that *any* ``Exception`` (e.g. a
    ``RecursionError`` from a maliciously deep JSON row) degrades to a miss.
    ``BaseException`` (``KeyboardInterrupt`` / ``SystemExit``) is deliberately
    NOT caught."""
    try:
        text = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else str(blob)
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("__v") != SCHEMA_VERSION:
            raise _CacheDecodeError(
                f"unrecognised cache schema: {type(payload)} / "
                f"v={payload.get('__v') if isinstance(payload, dict) else None}"
            )
        if "value" not in payload:
            # A row like ``{"__v": 1}`` must be a miss, not decode to ``None``
            # (which would be served to a caller expecting a Quote/list/tuple).
            raise _CacheDecodeError("cache envelope missing 'value' key")
        return _decode(payload["value"])
    except _CacheDecodeError:
        raise
    except Exception as e:  # noqa: BLE001 — legacy pickle, truncated/garbage,
        # or pathological (RecursionError) rows all funnel to a miss.
        raise _CacheDecodeError(f"not valid cache JSON: {e}") from e


def _make_key(fn_name: str, args: tuple, kwargs: dict) -> str:
    """Stable hash of the call shape."""
    # Skip `self` from method calls.
    if args and not isinstance(args[0], (str, int, float, list, tuple, dict, type(None))):
        args = args[1:]
    payload = repr((fn_name, args, sorted(kwargs.items())))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached(ttl_seconds: float) -> Callable[[_F], _F]:
    """Decorator: cache the return value for `ttl_seconds`."""

    def decorate(fn: _F) -> _F:
        _init_db()

        @functools.wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            key = _make_key(fn.__qualname__, args, kwargs)
            now = time.time()

            with _connect() as conn:
                row = conn.execute(
                    "SELECT value, expires_at FROM cache WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is not None and row[1] > now:
                    try:
                        return _deserialize(row[0])
                    except _CacheDecodeError as e:
                        # Old-format (pickle), tampered, or otherwise
                        # unparseable entry → treat as a miss and refetch.
                        # No pickle.loads is ever attempted.
                        log.warning("cache: decode failed for %s: %s", key[:12], e)

            value = fn(*args, **kwargs)

            try:
                blob = _serialize(value)
                with _connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                        (key, blob, now + ttl_seconds),
                    )
            except (TypeError, ValueError, sqlite3.Error) as e:
                log.warning("cache: write failed: %s", e)

            return value

        return wrapped  # type: ignore[return-value]

    return decorate
