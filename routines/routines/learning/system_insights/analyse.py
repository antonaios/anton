"""Three-layer analysis: cluster + aggregate + threshold-gate.

Input: dict of ``TelemetryEvent[]`` from ``readers.read_all_sources``.
Output: list of ``InsightProposal`` — already threshold-gated, ready for
the writer.

Three layers:

1. **Pattern clustering** — BERTopic on free-text error messages from
   ``audit_failures`` + ``llm_calls`` (status != "ok"). Reuses the same
   helpers + model pin as ``routines.learning.cluster`` (#40) — the
   learning loop runs one sentence-transformers download for both
   modules.

2. **Numeric aggregation** — per-job scheduler miss rates, per-skill
   latency p95 vs baseline, per-budget-scope incident counts vs
   4-week baseline, per-model LLM retry rates.

3. **Threshold gating** — only surface insights that
   (a) have ≥3 supporting events,
   (b) show ≥30% deviation from baseline (or ≥2σ for latency), and
   (c) fit within the 7-day window.

Threshold gating is load-bearing per the brief — operator attention is
the scarce resource; the routine must earn each proposal it produces.
Sparse weeks (no notable events) → zero proposals (silence is the
correct signal).
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from routines.learning.system_insights.readers import TelemetryEvent

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Thresholds (constants — exposed for tests; not operator-tunable in v1)
# ────────────────────────────────────────────────────────────────────────────


MIN_EVIDENCE_COUNT = 3                  # n>=3; never surface n=1 noise
MIN_MISS_RATE_PCT = 0.30                # 30% scheduler miss rate
MIN_INCIDENT_BASELINE_MULTIPLE = 3.0    # current >= 3x baseline incident count
MIN_LATENCY_SIGMA = 2.0                 # latency outlier >=2σ vs baseline
MIN_RETRY_RATE_PCT = 0.20               # 20% retry rate per model
MAX_EVIDENCE_SAMPLE = 3                 # keep proposal bodies compact


# ────────────────────────────────────────────────────────────────────────────
# InsightProposal
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class InsightProposal:
    """One operator-gated insight, ready for the writer.

    Fields:
        week: ISO week string ``YYYY-WNN`` (used in filename + frontmatter)
        topic_slug: kebab-case filesystem-safe slug (used in filename)
        observation: one-line summary (frontmatter ``observation:``)
        suggested_action: what the operator might do
        evidence_count: how many events backed this insight
        evidence_window_days: window the insight covered
        evidence_sample: up to MAX_EVIDENCE_SAMPLE representative events
            (preserved in the proposal body so the operator can sanity-check)
        source_query: shell/SQL command the operator can re-run to verify
        kind_hint: which analyser produced it (for grouping in the body)
    """

    week: str
    topic_slug: str
    observation: str
    suggested_action: str
    evidence_count: int
    evidence_window_days: int
    evidence_sample: list[dict[str, Any]] = field(default_factory=list)
    source_query: str = ""
    kind_hint: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Slug + week helpers
# ────────────────────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 60) -> str:
    """Filesystem-safe kebab-case slug. Empty → ``"insight"`` (never blank)."""
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not s:
        return "insight"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-") or "insight"
    return s


def iso_week_string(now: datetime) -> str:
    """``2026-W22`` per ISO-8601 calendar."""
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


# ────────────────────────────────────────────────────────────────────────────
# Layer 1 — Pattern clustering (free-text errors)
# ────────────────────────────────────────────────────────────────────────────


def _build_error_events(
    by_source: dict[str, list[TelemetryEvent]],
) -> list[tuple[TelemetryEvent, str]]:
    """Pull free-text error rows from the right sources.

    Returns ``[(event, text)]`` pairs ready for the clusterer. ``text`` is
    the field we cluster on (error class + message; falls back gracefully).
    """
    pairs: list[tuple[TelemetryEvent, str]] = []

    for ev in by_source.get("audit_failures", []):
        payload = ev.payload or {}
        ec = str(payload.get("error_class") or "Unknown")
        em = str(payload.get("error_message") or "")
        if not em.strip():
            continue
        pairs.append((ev, f"{ec}: {em}"))

    for ev in by_source.get("llm_calls", []):
        payload = ev.payload or {}
        status = str(payload.get("status") or "ok")
        if status == "ok":
            continue
        ec = str(payload.get("error_class") or status)
        # llm_calls rows rarely carry message text; cluster on class +
        # model + status so similar failures group.
        model = str(payload.get("model") or "unknown")
        text = f"{ec}: {status} model={model}"
        pairs.append((ev, text))

    return pairs


def _cluster_text_events(
    pairs: list[tuple[TelemetryEvent, str]],
    *,
    min_cluster_size: int = MIN_EVIDENCE_COUNT,
) -> list[list[tuple[TelemetryEvent, str]]]:
    """Cluster ``(event, text)`` pairs.

    Tries BERTopic first (reuses ``routines.learning.cluster`` helpers).
    Falls back to a deterministic literal-equality bucket when BERTopic
    isn't installed — tests don't need to download the model, and the
    bucket policy still surfaces repeated identical errors.
    """
    if len(pairs) < min_cluster_size:
        return []

    # Try the BERTopic pipeline (same model pin as #40). On import
    # failure or any error, fall back to literal bucketing — analysis
    # must not depend on a 80MB model download being available.
    try:
        from routines.learning.cluster import cluster_events as _cluster_v1
        from routines.learning.schema import FeedbackEvent

        feedback_events = [
            FeedbackEvent(
                timestamp=ev.ts.isoformat(),
                text=text,
                source="scan",
                classification=ev.kind,
            )
            for ev, text in pairs
        ]
        clusters = _cluster_v1(feedback_events, min_cluster_size=min_cluster_size)
        if clusters:
            # Map back to original (event, text) pairs by text-match.
            text_to_pair: dict[str, tuple[TelemetryEvent, str]] = {
                text: (ev, text) for ev, text in pairs
            }
            out: list[list[tuple[TelemetryEvent, str]]] = []
            for cluster in clusters:
                members: list[tuple[TelemetryEvent, str]] = []
                seen: set[int] = set()
                for fe in cluster.events:
                    key = id(fe)
                    if key in seen:
                        continue
                    seen.add(key)
                    pair = text_to_pair.get(fe.text)
                    if pair is not None:
                        members.append(pair)
                if len(members) >= min_cluster_size:
                    out.append(members)
            if out:
                return out
    except Exception as e:  # noqa: BLE001
        log.info(
            "system_insights: BERTopic cluster failed, falling back to "
            "literal bucketing: %s", e,
        )

    # Fallback: literal-equality buckets.
    buckets: dict[str, list[tuple[TelemetryEvent, str]]] = {}
    for ev, text in pairs:
        buckets.setdefault(text, []).append((ev, text))
    return [
        members for members in buckets.values()
        if len(members) >= min_cluster_size
    ]


def _cluster_proposals(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    week: str,
    window_days: int,
) -> list[InsightProposal]:
    """Emit one ``InsightProposal`` per error cluster meeting the threshold."""
    pairs = _build_error_events(by_source)
    clusters = _cluster_text_events(pairs)
    out: list[InsightProposal] = []
    for members in clusters:
        first_text = members[0][1]
        head, _, tail = first_text.partition(":")
        error_class = head.strip() or "error"
        snippet = (tail.strip() or first_text)[:80]
        slug = slugify(f"error-{error_class}-{snippet}")
        sample = [
            {
                "ts": ev.ts.isoformat(),
                "source": ev.source,
                "actor": ev.actor,
                "entity": ev.entity,
                "text": text,
            }
            for ev, text in members[:MAX_EVIDENCE_SAMPLE]
        ]
        observation = (
            f"Recurring `{error_class}` errors ({len(members)} occurrences "
            f"in the past {window_days}d)"
        )
        suggested = (
            "Investigate the root cause; if the error is genuine, file an "
            "issue. If the error class is noisy / expected, consider "
            "raising the failure to log-WARNING instead of @safe_audit."
        )
        source_query = (
            "Get-Content <repo>\\routines\\metrics\\audit_failures.jsonl "
            "| ConvertFrom-Json "
            f"| Where-Object {{ $_.error_class -eq '{error_class}' }}"
        )
        out.append(InsightProposal(
            week=week,
            topic_slug=slug,
            observation=observation,
            suggested_action=suggested,
            evidence_count=len(members),
            evidence_window_days=window_days,
            evidence_sample=sample,
            source_query=source_query,
            kind_hint="error-cluster",
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Layer 2 — Numeric aggregation
# ────────────────────────────────────────────────────────────────────────────


def _scheduler_miss_proposals(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    week: str,
    window_days: int,
) -> list[InsightProposal]:
    """One proposal per job with miss_pct >= MIN_MISS_RATE_PCT.

    Miss events are synthesised by ``read_scheduler_history`` already —
    we just gate on the rate.
    """
    out: list[InsightProposal] = []
    for ev in by_source.get("scheduler", []):
        if ev.kind != "scheduler.miss":
            continue
        payload = ev.payload or {}
        miss_pct = float(payload.get("miss_pct") or 0)
        miss_count = int(payload.get("miss_count") or 0)
        expected = int(payload.get("expected") or 0)
        actual = int(payload.get("actual") or 0)
        if miss_pct < MIN_MISS_RATE_PCT or miss_count < MIN_EVIDENCE_COUNT:
            continue
        job_id = ev.entity or "unknown"
        slug = slugify(f"scheduler-{job_id}-misses")
        observation = (
            f"Scheduler `{job_id}` fired {actual}/{expected} expected times "
            f"in the past {window_days}d ({miss_pct:.0%} miss rate)"
        )
        suggested = (
            f"Investigate why the `{job_id}` job missed fires. Common "
            f"causes: bridge restarts around the trigger time; "
            f"`catchup=skip-missed` plus a transient outage; subprocess "
            f"timeout. Consider switching `catchup` to `fire-on-startup` "
            f"(per #66) so a restart-at-fire-time still produces output."
        )
        source_query = (
            f"Get-Content <repo>\\routines\\runs\\scheduler.{job_id}.jsonl "
            f"| ConvertFrom-Json "
            f"| Where-Object {{ $_.ts -gt (Get-Date).AddDays(-{window_days}) }} "
            f"| Measure-Object"
        )
        out.append(InsightProposal(
            week=week,
            topic_slug=slug,
            observation=observation,
            suggested_action=suggested,
            evidence_count=miss_count,
            evidence_window_days=window_days,
            evidence_sample=[{
                "job_id": job_id,
                "expected": expected,
                "actual": actual,
                "miss_count": miss_count,
                "miss_pct": miss_pct,
            }],
            source_query=source_query,
            kind_hint="scheduler-miss",
        ))
    return out


def _budget_incident_proposals(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    week: str,
    window_days: int,
    baseline_window_weeks: int = 4,
) -> list[InsightProposal]:
    """Per-scope incident count surge proposals.

    For v1 we approximate baseline by ``MIN_EVIDENCE_COUNT`` floor: if a
    scope sees ≥3 incidents in 7d, that's anomalous on its own (the gate
    is monthly so 3 incidents/week represents the same scope tripping
    repeatedly). The ``baseline_window_weeks`` arg is reserved for the
    eventual historical-baseline upgrade once incidents.db has enough
    history.
    """
    out: list[InsightProposal] = []
    incidents = [
        ev for ev in by_source.get("budget", [])
        if ev.kind == "budget.incident"
    ]
    by_scope: dict[str, list[TelemetryEvent]] = {}
    for ev in incidents:
        by_scope.setdefault(ev.entity or "unknown", []).append(ev)

    for scope, evs in by_scope.items():
        if len(evs) < MIN_EVIDENCE_COUNT:
            continue
        sample = [
            {
                "ts": ev.ts.isoformat(),
                "scope": scope,
                "current_pct": ev.payload.get("current_pct"),
                "cap_usd": ev.payload.get("cap_usd"),
                "current_spend_usd": ev.payload.get("current_spend_usd"),
                "status": ev.payload.get("status"),
            }
            for ev in evs[:MAX_EVIDENCE_SAMPLE]
        ]
        slug = slugify(f"budget-incidents-{scope}")
        observation = (
            f"{len(evs)} budget incidents on scope `{scope}` in the past "
            f"{window_days}d — review cap or investigate the workload"
        )
        suggested = (
            f"Open the dashboard `/budgets` tab and review the `{scope}` "
            f"scope's cap. If recent usage is legitimate (e.g. a new "
            f"workflow), raise the cap via the existing ack flow. If "
            f"unexpected, investigate the workload triggering the gate "
            f"(check `routines/runs/budgets.gate.blocked.jsonl`)."
        )
        source_query = (
            "Get-Content <repo>\\routines\\runs\\budgets.incident.jsonl "
            "| ConvertFrom-Json "
            f"| Where-Object {{ $_.inputs.scope_kind -eq '{evs[0].payload.get('scope_kind')}' }}"
        )
        out.append(InsightProposal(
            week=week,
            topic_slug=slug,
            observation=observation,
            suggested_action=suggested,
            evidence_count=len(evs),
            evidence_window_days=window_days,
            evidence_sample=sample,
            source_query=source_query,
            kind_hint="budget-surge",
        ))
    _ = baseline_window_weeks  # reserved for the historical-baseline upgrade
    return out


def _latency_outlier_proposals(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    week: str,
    window_days: int,
) -> list[InsightProposal]:
    """Per-model latency outlier proposals based on within-window stats.

    v1 approach: compute median + stdev across all in-window calls for a
    model; flag a model whose p95 (or max) is >= MIN_LATENCY_SIGMA σ
    above the median. With only a 7-day window this catches "this model
    suddenly went slow" — the proper 4-week baseline lives in the
    aggregate-history follow-on.
    """
    out: list[InsightProposal] = []
    by_model: dict[str, list[float]] = {}
    by_model_evs: dict[str, list[TelemetryEvent]] = {}

    for ev in by_source.get("llm_calls", []):
        payload = ev.payload or {}
        dur = payload.get("duration_ms")
        if not isinstance(dur, (int, float)) or dur <= 0:
            continue
        model = str(payload.get("model") or ev.entity or "unknown")
        by_model.setdefault(model, []).append(float(dur))
        by_model_evs.setdefault(model, []).append(ev)

    for model, durations in by_model.items():
        if len(durations) < MIN_EVIDENCE_COUNT:
            continue
        median = statistics.median(durations)
        try:
            stdev = statistics.stdev(durations)
        except statistics.StatisticsError:
            continue
        if stdev <= 0:
            continue
        # Outliers = calls > median + MIN_LATENCY_SIGMA * stdev
        threshold = median + MIN_LATENCY_SIGMA * stdev
        outliers = [
            (ev, d) for ev, d in zip(by_model_evs[model], durations)
            if d > threshold
        ]
        if len(outliers) < MIN_EVIDENCE_COUNT:
            continue
        max_dur = max(d for _, d in outliers)
        slug = slugify(f"latency-{model}-outliers")
        observation = (
            f"Model `{model}` shows {len(outliers)} latency outliers in the "
            f"past {window_days}d — max {max_dur:.0f}ms vs median "
            f"{median:.0f}ms (σ={stdev:.0f}ms, {MIN_LATENCY_SIGMA}σ "
            f"threshold)"
        )
        suggested = (
            f"Check if `{model}` is overloaded; review provider-side "
            f"latency / throttling; consider routing to a smaller model "
            f"for non-critical paths."
        )
        source_query = (
            "Get-Content <repo>\\routines\\telemetry\\llm_calls.jsonl "
            "| ConvertFrom-Json "
            f"| Where-Object {{ $_.model -eq '{model}' }} "
            f"| Sort-Object duration_ms -Descending | Select-Object -First 10"
        )
        sample = [
            {
                "ts": ev.ts.isoformat(),
                "model": model,
                "duration_ms": d,
                "status": ev.payload.get("status"),
                "tokens_in": ev.payload.get("tokens_in"),
                "tokens_out": ev.payload.get("tokens_out"),
            }
            for ev, d in sorted(outliers, key=lambda p: -p[1])[:MAX_EVIDENCE_SAMPLE]
        ]
        out.append(InsightProposal(
            week=week,
            topic_slug=slug,
            observation=observation,
            suggested_action=suggested,
            evidence_count=len(outliers),
            evidence_window_days=window_days,
            evidence_sample=sample,
            source_query=source_query,
            kind_hint="latency-outlier",
        ))
    return out


def _retry_rate_proposals(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    week: str,
    window_days: int,
) -> list[InsightProposal]:
    """Per-model retry-rate proposals.

    Surfaces when a model's retry rows / total rows >= MIN_RETRY_RATE_PCT
    AND there are at least MIN_EVIDENCE_COUNT retries.
    """
    out: list[InsightProposal] = []
    by_model_status: dict[str, dict[str, int]] = {}

    for ev in by_source.get("llm_calls", []):
        payload = ev.payload or {}
        model = str(payload.get("model") or ev.entity or "unknown")
        status = str(payload.get("status") or "ok")
        by_model_status.setdefault(model, {}).setdefault(status, 0)
        by_model_status[model][status] += 1

    for model, counts in by_model_status.items():
        total = sum(counts.values())
        if total < MIN_EVIDENCE_COUNT:
            continue
        retries = counts.get("retry", 0) + counts.get("error", 0)
        if retries < MIN_EVIDENCE_COUNT:
            continue
        rate = retries / total
        if rate < MIN_RETRY_RATE_PCT:
            continue
        slug = slugify(f"retry-{model}")
        observation = (
            f"Model `{model}` retry/error rate {rate:.0%} ({retries}/"
            f"{total} calls) in the past {window_days}d — review provider "
            f"health or skill prompting"
        )
        suggested = (
            f"Inspect recent error/retry rows for `{model}`; check provider "
            f"status; if specific skills dominate, consider tightening "
            f"their prompt or adding pre-validation."
        )
        source_query = (
            "Get-Content <repo>\\routines\\telemetry\\llm_calls.jsonl "
            "| ConvertFrom-Json "
            f"| Where-Object {{ $_.model -eq '{model}' -and $_.status -ne 'ok' }}"
        )
        out.append(InsightProposal(
            week=week,
            topic_slug=slug,
            observation=observation,
            suggested_action=suggested,
            evidence_count=retries,
            evidence_window_days=window_days,
            evidence_sample=[{
                "model": model,
                "total_calls": total,
                "retries_or_errors": retries,
                "rate": rate,
                "status_breakdown": counts,
            }],
            source_query=source_query,
            kind_hint="retry-rate",
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ────────────────────────────────────────────────────────────────────────────


def analyse_window(
    by_source: dict[str, list[TelemetryEvent]],
    *,
    now: Optional[datetime] = None,
    window_days: int = 7,
) -> list[InsightProposal]:
    """Run all three layers on the per-source dict.

    Returns a deterministic list of proposals (sorted by topic_slug) — the
    writer is responsible for path collisions and idempotent overwrites.
    Sparse weeks (no events meeting any threshold) return ``[]``. Silence
    is the correct signal.
    """
    now = now or datetime.now(timezone.utc)
    week = iso_week_string(now)

    proposals: list[InsightProposal] = []
    proposals.extend(_cluster_proposals(
        by_source, week=week, window_days=window_days,
    ))
    proposals.extend(_scheduler_miss_proposals(
        by_source, week=week, window_days=window_days,
    ))
    proposals.extend(_budget_incident_proposals(
        by_source, week=week, window_days=window_days,
    ))
    proposals.extend(_latency_outlier_proposals(
        by_source, week=week, window_days=window_days,
    ))
    proposals.extend(_retry_rate_proposals(
        by_source, week=week, window_days=window_days,
    ))

    proposals.sort(key=lambda p: p.topic_slug)
    return proposals


__all__ = [
    "InsightProposal",
    "analyse_window",
    "iso_week_string",
    "slugify",
    "MIN_EVIDENCE_COUNT",
    "MIN_MISS_RATE_PCT",
    "MIN_INCIDENT_BASELINE_MULTIPLE",
    "MIN_LATENCY_SIGMA",
    "MIN_RETRY_RATE_PCT",
    "MAX_EVIDENCE_SAMPLE",
]


_ = math  # reserved for future log-scale / percentile helpers
_ = timedelta
