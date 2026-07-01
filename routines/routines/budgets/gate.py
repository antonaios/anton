"""Pre-call budget gate.

``get_invocation_block(scope_dict) → InvocationBlock | None`` is the
core primitive. Called from the ``@before_llm_call`` hook
(``enforce_budget_gate``) which is registered at app startup alongside
the existing central guards.

Scope resolution order (least → most specific):
  1. ``global``                              — overall cap
  2. ``provider`` × ``model``                — per-API-key granularity
  3. ``workspace_type`` × ``workspace_name`` — per-project ring-fence

First scope where current spend × 100 / cap ≥ hard_pct returns a block.
The gate evaluates least-specific first so operators see the broadest
blocker (the audit message is clearer that way — global > workspace).

Spend source: ``routines/telemetry/llm_calls.jsonl`` aggregated in
the current monthly UTC window. Records with ``status='blocked'`` are
excluded from spend (cost_usd=0 anyway, but the filter makes intent
explicit).

When a block fires the gate ALSO:
  * Opens an incident (or returns the existing open/paused one).
  * Writes a ``status='blocked'`` row to ``llm_calls.jsonl`` so the
    burn aggregator surfaces blocked-call counts.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from routines.budgets.incidents import (
    Incident,
    find_blocking_incident,
    record_overrun,
)
from routines.budgets.policy import (
    BudgetPolicy,
    BudgetWarn,
    Contributor,
    InvocationBlock,
    ScopeRef,
    monthly_utc_window,
)
from routines.budgets.storage import get_policy
from routines.hooks import before_llm_call
from routines.hooks.types import LLMCallHookContext

logger = logging.getLogger(__name__)


class InvocationBlocked(RuntimeError):
    """Raised (or signalled via hook return-False) when a scope is over cap.

    The dispatcher converts this into a structured refusal message; the
    chat router stashes the block on ``ctx.usage['budget_block']`` so
    the response body can surface the reason.
    """

    def __init__(self, block: InvocationBlock):
        super().__init__(block.reason)
        self.block = block


# ────────────────────────────────────────────────────────────────────────────
# get_invocation_block — the primitive
# ────────────────────────────────────────────────────────────────────────────


def get_invocation_block(
    scope: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[InvocationBlock]:
    """Check every applicable scope; return the first block or None.

    Args:
        scope: dict with optional keys ``provider``, ``model``,
            ``workspace_type``, ``workspace_name``. Missing keys collapse
            the corresponding scope check.
        now: injectable clock for tests.

    Returns:
        InvocationBlock if any applicable scope is over its hard
        threshold OR has an unresolved incident in the current period;
        None otherwise.

    Side-effect: when this returns a block AND no incident exists for the
    scope/period, ``record_overrun`` writes one.
    """
    now = now or datetime.now(timezone.utc)
    applicable = _applicable_scopes(scope)
    spend_by_scope = _current_spend_by_scope(applicable, now=now)

    for scope_ref in applicable:
        # 1) Look for an unresolved (open or acknowledged_paused) incident.
        existing = find_blocking_incident(scope_ref, now=now)
        if existing is not None:
            return _block_from_incident(existing, now=now)

        # 2) No incident — check the spend vs the policy.
        policy = get_policy(scope_ref)
        if policy is None:
            continue

        spend = spend_by_scope.get(scope_ref.id(), 0.0)
        if policy.cap_usd <= 0:
            # Treat a zero cap as "never block" — same semantics as no policy.
            continue
        pct = (spend / policy.cap_usd) * 100.0
        if pct >= policy.hard_pct:
            inc = record_overrun(
                scope_ref,
                current_pct=pct,
                hard_pct=policy.hard_pct,
                cap_usd=policy.cap_usd,
                current_spend_usd=spend,
                now=now,
            )
            return _build_block(scope_ref, policy, spend, pct, inc, now=now)

    return None


# ────────────────────────────────────────────────────────────────────────────
# get_invocation_warn — the non-fatal soft-warning primitive (P1 / #steal-kocoro)
# ────────────────────────────────────────────────────────────────────────────


def get_invocation_warn(
    scope: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[BudgetWarn]:
    """Check every applicable scope; return the first WARN-band hit or None.

    The non-fatal companion to :func:`get_invocation_block`. A scope is in
    the WARN band when ``warn_pct <= pct < hard_pct`` — spend has crossed
    the soft threshold but not the hard cap. Returns a ``BudgetWarn`` for
    the FIRST such scope in the SAME least-→-most-specific order the block
    uses (so the operator sees the broadest warning first), else None.

    Intended to be called ONLY after :func:`get_invocation_block` returned
    None (no block + no blocking incident): it relies on the SAME memoized
    spend scan, so the second call within one request is a cache hit (zero
    extra file reads). It opens NO incident and has NO side effects — a warn
    is informational, never a gate. A scope with no policy, a zero/negative
    cap, or zero spend never warns.
    """
    now = now or datetime.now(timezone.utc)
    applicable = _applicable_scopes(scope)
    spend_by_scope = _current_spend_by_scope(applicable, now=now)

    for scope_ref in applicable:
        policy = get_policy(scope_ref)
        if policy is None or policy.cap_usd <= 0:
            continue
        spend = spend_by_scope.get(scope_ref.id(), 0.0)
        if spend <= 0:
            continue
        pct = (spend / policy.cap_usd) * 100.0
        if policy.warn_pct <= pct < policy.hard_pct:
            reason = (
                f"scope {scope_ref.id()!r} at {pct:.1f}% of "
                f"${policy.cap_usd:.2f} cap "
                f"(warn {policy.warn_pct:.0f}%, hard {policy.hard_pct:.0f}%)"
            )
            return BudgetWarn(
                scope=scope_ref,
                reason=reason,
                current_pct=round(pct, 4),
                warn_pct=policy.warn_pct,
                hard_pct=policy.hard_pct,
                cap_usd=policy.cap_usd,
                current_spend_usd=round(spend, 6),
            )
    return None


# ────────────────────────────────────────────────────────────────────────────
# Applicable scopes for a given context
# ────────────────────────────────────────────────────────────────────────────


def _applicable_scopes(scope: Mapping[str, Any]) -> list[ScopeRef]:
    """Materialise the ScopeRefs the gate checks, least → most specific so the
    audit message names the broadest blocker first.
    """
    out: list[ScopeRef] = [ScopeRef(kind="global")]
    provider = scope.get("provider")
    model = scope.get("model")
    if provider:
        # All-models provider scope (``b="*"``) — the seam for a provider-wide
        # USD cap like the Agent-SDK monthly credit (#llm-routing-postjune15 B3).
        # Keyed on the NORMALIZED provider so the exact-scope_id policy LOOKUP
        # agrees with spend aggregation + the dashboard (which both normalize,
        # e.g. claude / claude-subprocess → anthropic). Otherwise an operator cap
        # keyed "anthropic" would be invisible to a call routed as "claude", and
        # the seed couldn't detect it → a duplicate / dead policy (review SEV-2).
        # The per-model scope below stays RAW for back-compat with existing
        # per-(provider,model) caps. Checked before the per-model scope; spend
        # aggregation honours the ``*`` wildcard so every model counts; a
        # per-model cap still fires independently.
        from routines.telemetry.cost_table import normalize_provider
        norm = normalize_provider(provider) or str(provider)
        out.append(ScopeRef(kind="provider", a=norm, b="*"))
    if provider and model:
        out.append(ScopeRef(kind="provider", a=str(provider), b=str(model)))
    ws_type = scope.get("workspace_type")
    ws_name = scope.get("workspace_name")
    if ws_type and ws_name:
        out.append(ScopeRef(kind="workspace", a=str(ws_type), b=str(ws_name)))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Spend aggregation
# ────────────────────────────────────────────────────────────────────────────
#
# #eff-hotpath-batch — the budget gate fires on EVERY cloud LLM call and used
# to re-scan the entire never-rotated ``llm_calls.jsonl`` line-by-line each time
# (O(total history) per call). Two complementary fixes land:
#
#   (Part B) Retention rotates/caps ``llm_calls.jsonl`` to the retention window,
#   so the file itself no longer grows unbounded — the scan input is bounded.
#
#   (here)   A process-local memo keyed on the file's identity signature
#   (size, mtime_ns) + the month window + the requested scope-set + a "kind"
#   tag (cost vs tokens). The scan output is a pure function of those inputs, so
#   when nothing has been appended since the last identical request we return
#   the cached totals with ZERO file reads. This is provably identical to the
#   uncached full scan — it caches the SAME computation, it does not change
#   which records are summed or the decision the gate makes. A new append
#   changes (size, mtime_ns) → cache miss → fresh full scan, so a just-written
#   spend row is always reflected. The lock keeps the memo consistent under the
#   threadpool + APScheduler concurrent-reader load.

_scan_cache: dict[tuple, dict[str, float]] = {}
_scan_cache_lock = threading.Lock()
_SCAN_CACHE_MAX = 64  # bound the memo so distinct scope-sets can't grow it forever


def _file_signature(path) -> tuple[int, int] | None:
    """Return (size, mtime_ns) for ``path`` or ``None`` if it's absent.

    Identity of the telemetry file for the memo key — any append changes the
    size (and almost always the mtime), invalidating the cache."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _safe_nonneg_int(value: Any) -> int:
    """Coerce a telemetry numeric field to a NON-NEGATIVE int; 0 on None/missing/
    negative/non-numeric/non-finite (codex SEV-2 + r2). A corrupt or hand-edited
    row must never subtract from a token total (a negative count) nor raise
    inside the scan loop — whose only ``except`` is ``OSError``, so an uncaught
    coercion error would sink the whole scan + the /budgets surface that calls
    it. ``OverflowError`` covers a non-finite ``inf`` (``json`` parses ``1e309`` /
    ``Infinity`` to ``float('inf')`` and ``int(inf)`` raises ``OverflowError``);
    a ``NaN`` raises ``ValueError`` and is already covered."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _scan_jsonl_totals(
    scopes: list[ScopeRef],
    *,
    now: datetime,
    field: str,
) -> dict[str, float]:
    """Sum a numeric ``field`` from ``llm_calls.jsonl`` per scope, current month.

    ``field="cost"`` sums ``cost_usd`` (float); ``field="tokens"`` sums
    ``tokens_in + tokens_out + cache_read_tokens + cache_creation_tokens`` (the
    cache-aware quota, #steal-kocoro P4; int, returned as float and int-coerced
    by the caller). One file scan; results bucketed against the requested scopes.
    Records with no valid ``ts``, outside the month window, or with
    ``status='blocked'`` are skipped — IDENTICAL filtering to the original
    per-call scan. Memoized on the file signature + window + scope-set + field
    so a stable file is not re-read.
    """
    from routines.telemetry import llm_writer as _writer
    path = _writer.LLM_CALLS_JSONL

    month_start, until = monthly_utc_window(now)
    totals: dict[str, float] = {s.id(): 0.0 for s in scopes}

    sig = _file_signature(path)
    if sig is None:
        return totals  # file absent → all zero (not cached; cheap)

    # Memo key: file identity + the MONTH START + the requested scope ids
    # (sorted for stability) + which numeric field we're summing. ``until``
    # (== now) is deliberately NOT in the key — it advances every call and
    # would defeat cross-call caching. The ONLY way two calls with the same
    # key could differ is a record dated in the future relative to one call's
    # ``until`` but not the other's. Records are stamped at append time so this
    # is effectively impossible; we still guard it: if the scan sees any
    # future-dated in-month record (``ts > until``) we refuse to cache, falling
    # back to the always-correct full scan. This keeps the gate decision
    # provably identical to the uncached path.
    cache_key = (
        str(path), sig, month_start.isoformat(),
        tuple(sorted(s.id() for s in scopes)), field,
    )
    cached = _scan_cache.get(cache_key)
    if cached is not None:
        return dict(cached)  # copy — callers may mutate their result

    saw_future_record = False
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "blocked":
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < month_start:
                    continue
                if ts > until:
                    # Future-dated record (clock skew / hand-edited row): it is
                    # excluded from THIS window but would be included once
                    # ``until`` advances past it — so the result is not a pure
                    # function of (file, month_start). Don't cache.
                    saw_future_record = True
                    continue
                if field == "cost":
                    value = float(rec.get("cost_usd") or 0.0)
                else:  # tokens
                    # P4 (#steal-kocoro): cache-aware token quota — fold the
                    # cache legs (read + creation) into the per-scope total so
                    # the dashboard quota reflects ALL tokens the scope moved,
                    # not just input+output. The cost field weights cache by its
                    # own rates (cost_table); this raw-count view counts every
                    # token at face value. ``_safe_nonneg_int`` makes a corrupt /
                    # hand-edited row fail-safe: None / negative / non-numeric →
                    # 0, so a bad field can neither subtract from a total nor
                    # raise inside this loop (codex SEV-2). Pre-P4 rows lack the
                    # cache fields → fold in as 0 → identical totals (additive).
                    value = float(
                        _safe_nonneg_int(rec.get("tokens_in"))
                        + _safe_nonneg_int(rec.get("tokens_out"))
                        + _safe_nonneg_int(rec.get("cache_read_tokens"))
                        + _safe_nonneg_int(rec.get("cache_creation_tokens"))
                    )
                if value <= 0:
                    continue
                for s in scopes:
                    if _record_matches_scope(rec, s):
                        totals[s.id()] += value
    except OSError as e:
        logger.warning("budgets gate: could not read %s: %s", path, e)
        return totals  # transient read error → don't cache a partial result

    if not saw_future_record:
        with _scan_cache_lock:
            # Cheap bound: if the memo grew past the cap, drop the whole thing
            # rather than tracking LRU — distinct scope-sets are few and the
            # file signature rolls on every append anyway, so stale keys are
            # short-lived.
            if len(_scan_cache) >= _SCAN_CACHE_MAX:
                _scan_cache.clear()
            _scan_cache[cache_key] = dict(totals)
    return totals


def _current_spend_by_scope(
    scopes: list[ScopeRef],
    *,
    now: datetime,
) -> dict[str, float]:
    """Sum cost_usd from ``llm_calls.jsonl`` per scope, in the current month.

    Thin wrapper over the memoized scanner — see ``_scan_jsonl_totals``.
    Records with no valid ``ts`` or with ``status='blocked'`` are skipped.

    Each row's ``cost_usd`` is cache-COMPLETE as of #steal-kocoro P4 (cache_read
    was already priced; cache_creation now is too — see ``compute_cost_usd``), so
    this gate-facing spend total intentionally reflects real cache spend.
    """
    return _scan_jsonl_totals(scopes, now=now, field="cost")


def current_tokens_by_scope(
    scopes: list[ScopeRef],
    *,
    now: datetime,
) -> dict[str, int]:
    """Sum ``tokens_in + tokens_out + cache_read_tokens + cache_creation_tokens``
    from ``llm_calls.jsonl`` per scope, in the current monthly UTC window — the
    cache-aware token quota (#steal-kocoro P4).

    Read-only sibling of ``_current_spend_by_scope`` — same scan / window /
    skip-``blocked`` logic (shares the memoized scanner), but accumulates
    tokens instead of cost. Drives the dashboard's token-usage display
    (track + warn); it does NOT gate. ``get_invocation_block`` /
    ``enforce_budget_gate`` never call this — folding the cache legs in changes
    only the displayed token quota, never a block decision. (The USD spend path
    ``_current_spend_by_scope`` IS cache-aware too — via each row's cost_usd —
    and DOES feed the #57 gate; that "cap reflects cache spend" behaviour is
    intended and separate from this display-only token figure.)
    """
    float_totals = _scan_jsonl_totals(scopes, now=now, field="tokens")
    return {k: int(v) for k, v in float_totals.items()}


def _record_matches_scope(rec: dict, scope: ScopeRef) -> bool:
    if scope.kind == "global":
        return True
    if scope.kind == "provider":
        # #budget-provider-namespace: the scope key (``ctx.provider`` =
        # ``claude``/``codex``) and the telemetry key (``provider_override`` =
        # ``claude-subprocess``/``claude-api``/``codex-subprocess``, or
        # ``provider_for(model)`` = ``anthropic``/``openai``) are different
        # alias families. Normalize BOTH to the canonical provider so a
        # per-provider cap aggregates cloud-lane spend instead of reading 0.
        if not _provider_matches(rec.get("provider"), scope.a):
            return False
        # ``b="*"`` is the per-LLM (all-models) cap the dashboard sets:
        # match any model so long as the provider matches. A concrete ``b``
        # keeps the original (provider, model)-pair match — unchanged.
        if scope.b == "*":
            return True
        return rec.get("model") == scope.b
    if scope.kind == "workspace":
        return (
            rec.get("workspace_type") == scope.a
            and rec.get("workspace_name") == scope.b
        )
    if scope.kind == "workspace_provider":
        # ``a = "<workspace_type>:<workspace_name>"``, ``b = "<provider>"``.
        # The per-project-per-LLM token cap.
        ws_type, _, ws_name = (scope.a or "").partition(":")
        return (
            rec.get("workspace_type") == ws_type
            and rec.get("workspace_name") == ws_name
            and _provider_matches(rec.get("provider"), scope.b)
        )
    return False  # pragma: no cover


def _provider_matches(record_provider: Any, scope_provider: Any) -> bool:
    """True if a telemetry row's provider belongs to the scope's provider.

    Both are run through :func:`normalize_provider` so the gate's per-provider
    scope (keyed on ``ctx.provider`` = ``claude``/``codex``/``ollama``) agrees
    with the telemetry keys (``claude-subprocess``/``codex-subprocess``/
    ``anthropic``/``openai``/...). The single normalization is the contract
    that keeps the two sides from drifting — #budget-provider-namespace.
    """
    from routines.telemetry.cost_table import normalize_provider
    norm_scope = normalize_provider(scope_provider if isinstance(scope_provider, str) else None)
    if norm_scope is None:
        return False
    return normalize_provider(
        record_provider if isinstance(record_provider, str) else None
    ) == norm_scope


def _parse_ts(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Block construction
# ────────────────────────────────────────────────────────────────────────────


def _build_block(
    scope: ScopeRef,
    policy: BudgetPolicy,
    spend: float,
    pct: float,
    incident: Incident,
    *,
    now: datetime,
) -> InvocationBlock:
    contributors = _top_contributors(scope, spend, now=now)
    breakdown = _format_contributors(contributors)
    reason = (
        f"scope {scope.id()!r} at {pct:.1f}% of ${policy.cap_usd:.2f} cap "
        f"(hard_pct={policy.hard_pct:.0f}%); incident={incident.id}"
    )
    if breakdown:
        reason = f"{reason}; top: {breakdown}"
    return InvocationBlock(
        scope=scope,
        reason=reason,
        current_pct=round(pct, 4),
        hard_pct=policy.hard_pct,
        cap_usd=policy.cap_usd,
        current_spend_usd=round(spend, 6),
        incident_id=incident.id,
        contributors=contributors,
    )


def _block_from_incident(inc: Incident, *, now: datetime) -> InvocationBlock:
    pause_note = (
        " (paused by operator)" if inc.status == "acknowledged_paused" else ""
    )
    contributors = _top_contributors(inc.scope, inc.current_spend_usd, now=now)
    breakdown = _format_contributors(contributors)
    reason = (
        f"scope {inc.scope.id()!r} blocked at {inc.current_pct:.1f}% of "
        f"${inc.cap_usd:.2f} cap; incident={inc.id}{pause_note}"
    )
    if breakdown:
        reason = f"{reason}; top: {breakdown}"
    return InvocationBlock(
        scope=inc.scope,
        reason=reason,
        current_pct=inc.current_pct,
        hard_pct=inc.hard_pct,
        cap_usd=inc.cap_usd,
        current_spend_usd=inc.current_spend_usd,
        incident_id=inc.id,
        contributors=contributors,
    )


# ────────────────────────────────────────────────────────────────────────────
# Contributors — who drove the spend within the blocked scope
# ────────────────────────────────────────────────────────────────────────────


def _top_contributors(
    blocked_scope: ScopeRef,
    scope_total_spend: float,
    *,
    now: datetime,
    limit: int = 2,
) -> list[Contributor]:
    """Return the top sub-scopes by spend within ``blocked_scope``.

    Always returns up to ``limit`` provider×model entries and up to
    ``limit`` workspace entries (so the operator sees both axes). For
    a workspace block the per-workspace breakdown is degenerate (the
    block IS the workspace) — in that case only the provider×model
    axis is meaningful and workspace entries are suppressed. Same
    logic in reverse for a provider block.

    One file scan; bucketed against the call records in the current
    monthly UTC window. Records outside the blocked scope are skipped
    (e.g. for a workspace block, only records matching that workspace
    contribute).
    """
    if scope_total_spend <= 0:
        return []

    from routines.telemetry import llm_writer as _writer
    path = _writer.LLM_CALLS_JSONL
    if not path.exists():
        return []

    month_start, until = monthly_utc_window(now)

    provider_buckets: dict[tuple[str, str], float] = {}
    workspace_buckets: dict[tuple[str, str], float] = {}

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json
                    rec = _json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("status") == "blocked":
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < month_start or ts > until:
                    continue
                cost = float(rec.get("cost_usd") or 0.0)
                if cost <= 0:
                    continue
                if not _record_matches_scope(rec, blocked_scope):
                    continue

                provider = rec.get("provider")
                model = rec.get("model")
                if provider and model:
                    key = (str(provider), str(model))
                    provider_buckets[key] = provider_buckets.get(key, 0.0) + cost

                ws_type = rec.get("workspace_type")
                ws_name = rec.get("workspace_name")
                if ws_type and ws_name:
                    key = (str(ws_type), str(ws_name))
                    workspace_buckets[key] = workspace_buckets.get(key, 0.0) + cost
    except OSError as e:
        logger.warning("budgets gate: contributors read failed: %s", e)
        return []

    out: list[Contributor] = []

    # Suppress the degenerate axis: if the blocked scope IS a workspace,
    # don't show a workspace breakdown (it'd be just itself at 100%).
    show_workspace = blocked_scope.kind != "workspace"
    show_provider = blocked_scope.kind != "provider"

    if show_provider:
        ranked = sorted(provider_buckets.items(), key=lambda kv: kv[1], reverse=True)
        for (prov, mod), s in ranked[:limit]:
            out.append(Contributor(
                kind="provider",
                a=prov,
                b=mod,
                spend_usd=round(s, 6),
                pct_of_scope=round(s / scope_total_spend * 100.0, 2),
            ))

    if show_workspace:
        ranked = sorted(workspace_buckets.items(), key=lambda kv: kv[1], reverse=True)
        for (wt, wn), s in ranked[:limit]:
            out.append(Contributor(
                kind="workspace",
                a=wt,
                b=wn,
                spend_usd=round(s, 6),
                pct_of_scope=round(s / scope_total_spend * 100.0, 2),
            ))

    return out


def _format_contributors(contributors: list[Contributor]) -> str:
    """Render contributors as a compact one-line breakdown for reason text."""
    if not contributors:
        return ""
    parts = [
        f"{c.a}/{c.b}=${c.spend_usd:.2f} ({c.pct_of_scope:.0f}%)"
        for c in contributors
    ]
    return ", ".join(parts)


# ────────────────────────────────────────────────────────────────────────────
# @before_llm_call hook
# ────────────────────────────────────────────────────────────────────────────


@before_llm_call
def enforce_budget_gate(ctx: LLMCallHookContext) -> bool | None:
    """Pre-call budget gate.

    Returns ``False`` to block — the dispatcher will skip the LLM call.
    The block payload is stashed on ``ctx.usage['budget_block']`` so
    callers can surface a structured refusal to the user.

    Also writes a ``status='blocked'`` row directly to ``llm_calls.jsonl``
    so the burn aggregator can count blocked attempts even though the
    after-hooks won't fire (the dispatcher short-circuits on False).
    """
    scope_dict = {
        "provider": ctx.provider,
        "model": ctx.model,
        "workspace_type": ctx.workspace.type if ctx.workspace else None,
        "workspace_name": ctx.workspace.name if ctx.workspace else None,
    }
    # One clock for BOTH the block + warn checks: they then reason about the
    # IDENTICAL monthly window (no month-boundary skew between two separate
    # now() calls) and the warn reuses the SAME memoized spend scan →
    # guaranteed cache hit, no second telemetry file read (codex r1 SEV-2).
    now = datetime.now(timezone.utc)
    block = get_invocation_block(scope_dict, now=now)
    if block is None:
        # No hard block — but spend may be in the WARN band (warn_pct ≤ pct
        # < hard_pct). A warn is INFORMATIONAL: stash a compact summary so
        # the after-hook telemetry + the dashboard can flag "approaching
        # cap", then PROCEED (return None). The warn scan reuses the SAME
        # memoized spend scan get_invocation_block just ran → cache hit, no
        # extra file read. Best-effort: a warn lookup must never break a
        # call that the gate already decided to allow.
        try:
            warn = get_invocation_warn(scope_dict, now=now)
        except Exception as e:  # noqa: BLE001 — warn is advisory, never fatal
            logger.warning("budgets gate: warn lookup failed: %s", e)
            warn = None
        if warn is not None and isinstance(ctx.usage, dict):
            ctx.usage["budget_warn"] = {
                # Full id (may embed the workspace/deal name) stays in the
                # in-process stash for the live operator chip — same as
                # budget_block. The PERSISTED telemetry field uses scope_kind
                # only (see _build_record) so a deal name never lands in
                # llm_calls.jsonl (codex security review SEV-2).
                "scope": warn.scope.id(),
                "scope_kind": warn.scope.kind,
                "current_pct": warn.current_pct,
                "warn_pct": warn.warn_pct,
                "hard_pct": warn.hard_pct,
                "cap_usd": warn.cap_usd,
                "current_spend_usd": warn.current_spend_usd,
                "reason": warn.reason,
            }
        return None

    # Stash on context so router (or any caller running the hook chain)
    # can surface the reason. Use a top-level usage key, not nested under
    # _telemetry, because callers shouldn't dig through internal keys.
    if isinstance(ctx.usage, dict):
        ctx.usage["budget_block"] = block.model_dump()
        ctx.usage["status"] = "blocked"
        ctx.usage["block_reason"] = block.reason

    _write_blocked_telemetry_row(ctx, block)
    _audit_blocked_call(ctx, block)
    return False


def _write_blocked_telemetry_row(
    ctx: LLMCallHookContext, block: InvocationBlock,
) -> None:
    """Emit one blocked LLMCallRecord. Cost is 0; status='blocked'.

    Schema is the same as a normal record (see ``llm_hooks._build_record``)
    plus the new ``block_reason`` / ``blocked_scope`` fields so the burn
    aggregator and audit can filter on them.
    """
    try:
        from routines.telemetry.cost_table import provider_for
        from routines.telemetry.llm_writer import write_llm_call

        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": ctx.run_id,
            "session_id": (
                ctx.usage.get("session_id") if isinstance(ctx.usage, dict) else None
            ),
            "workspace_type": ctx.workspace.type if ctx.workspace else None,
            "workspace_name": ctx.workspace.name if ctx.workspace else None,
            # #75: subprocess dispatcher stamps provider_override =
            # "claude-subprocess" so plan-credit calls segregate from
            # API calls (future) in burn queries.
            "provider": ctx.provider_override or (
                provider_for(ctx.model) if ctx.model else (ctx.provider or "unknown")
            ),
            "model": ctx.model or "unknown",
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read_tokens": None,
            "cache_creation_tokens": None,
            "duration_ms": 0,
            "cost_usd": 0.0,
            "status": "blocked",
            "error_class": None,
            "route": f"BLOCKED · {block.scope.id()}",
            "lane": (ctx.usage.get("lane") if isinstance(ctx.usage, dict) else None) or ctx.lane,
            "block_reason": block.reason,
            "blocked_scope": block.scope.id(),
            "incident_id": block.incident_id,
        }
        write_llm_call(record)
    except Exception as e:  # noqa: BLE001 — telemetry must not break the gate
        logger.warning("budgets gate: blocked telemetry write failed: %s", e)


def _audit_blocked_call(
    ctx: LLMCallHookContext, block: InvocationBlock,
) -> None:
    """One audit row per blocked invocation. Failure-tolerant."""
    try:
        from routines.api.deps import ROUTINES_REPO
        from routines.shared import audit
        audit.write_structured(
            actor={"type": "system", "id": "routine:budgets.gate.blocked"},
            entity_type="budget",
            entity_id=block.scope.id(),
            action="gate",
            routine="budgets.gate.blocked",
            run_id=ctx.run_id,
            status="blocked",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={
                "skill": ctx.skill.name if ctx.skill else None,
                "provider": ctx.provider,
                "model": ctx.model,
                "workspace_type": ctx.workspace.type if ctx.workspace else None,
                "workspace_name": ctx.workspace.name if ctx.workspace else None,
            },
            outputs={
                "scope": block.scope.id(),
                "current_pct": block.current_pct,
                "hard_pct": block.hard_pct,
                "incident_id": block.incident_id,
            },
            error=block.reason,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("budgets gate: audit write failed: %s", e)


__all__ = [
    "InvocationBlocked",
    "get_invocation_block",
    "get_invocation_warn",
    "enforce_budget_gate",
    "current_tokens_by_scope",
]
