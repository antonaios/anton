"""Injection scan + audit (#sec-injection-guard 3a) — DETECT-AND-AUDIT, NEVER BLOCK.

``scan_ingested_text`` runs the heuristic detector (``heuristics``) over untrusted
EXTERNAL text at an ingestion boundary and, on a detection, writes a
deal-name-free audit row (``runs/guards.injection.jsonl`` via the canonical
``audit.write_structured`` per-routine co-write + activity stream). In 3a it
NEVER raises and NEVER blocks — a detection is recorded for forensics + 3b
tuning; the caller proceeds unchanged. (Graduating high-confidence detections to
a fail-closed block is 3b, gated on real-traffic false-positive tuning + a
Shannon re-run.)

Deal-name hygiene (the #57/P1 lesson — keep target/buyer names out of audit
surfaces): the audit records the SOURCE label, the score, the matched ANTON
keyword pattern (OUR own phrase, e.g. "ignore previous instructions" — never the
untrusted content), the text length, and a 12-char SHA-256 prefix (lets an
analyst correlate repeated attacks WITHOUT storing the text). The raw untrusted
text — which can embed a deal/target name or MNPI — is NEVER written, here or to
the logs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from routines.guards.injection import heuristics

logger = logging.getLogger(__name__)


@dataclass
class InjectionVerdict:
    """The outcome of one ingestion-boundary scan. ``flagged`` means
    ``score >= threshold``; in 3a a flagged verdict is audited but NOT acted on
    (never-block). All fields are deal-name-free — safe for audit/telemetry."""

    flagged: bool
    score: float
    source: str
    matched_pattern: Optional[str]   # OUR keyword phrase, never the untrusted text
    text_chars: int
    text_sha256_12: str
    threshold: float
    bounded: bool   # True if a char / per-token / word cap or the ratio budget cut the scan short

    def public_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "score": round(self.score, 4),
            "flagged": self.flagged,
            "matched_pattern": self.matched_pattern,
            "text_chars": self.text_chars,
            "text_sha256_12": self.text_sha256_12,
            "threshold": self.threshold,
            "bounded": self.bounded,
        }


def _sha256_12(text: str) -> str:
    """SHA-256 (12-char prefix) of a CAPPED prefix of ``text`` — never the full
    (possibly huge) string, so a giant input can't force unbounded hashing after
    the heuristic scan has already bounded itself (codex SEV-2). The cap matches
    the scan's char cap, so identical scanned content yields the same hash."""
    capped = text[: heuristics._max_scan_chars()]
    return hashlib.sha256(capped.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_source(source: object) -> str:
    """Canonicalise the caller's ``source`` label: restrict to a safe charset +
    bound the length, so an audit/log line can't carry an over-long or
    content-bearing source regardless of caller (codex / data-handling P3). The
    three shipped call sites pass static labels; this is defence in depth.

    NEVER raises — a pathological ``source`` whose ``__str__`` raises must not
    SUPPRESS the scan (codex r3 SEV-3); it falls back to ``"unknown"``."""
    try:
        text = source if isinstance(source, str) else str(source)
    except Exception:  # noqa: BLE001 — a bad __str__ must not abort detection
        return "unknown"
    # Cap BEFORE the regex so a pathological huge source can't burn regex time
    # before the length cap (codex r4 SEV-3); 256 is ample for any real label.
    cleaned = re.sub(r"[^A-Za-z0-9_:.\-]", "", text[:256])[:64]
    return cleaned or "unknown"


def scan_ingested_text(
    text: Any,
    *,
    source: str,
    run_id: Optional[str] = None,
    audit: bool = True,
) -> InjectionVerdict:
    """Heuristic-scan untrusted external ``text`` from an ingestion boundary.

    DETECT-AND-AUDIT, NEVER BLOCK (3a): returns a verdict and, when flagged,
    best-effort audits it. NEVER raises — a scanner failure must not break
    ingestion (a guard is not allowed to deny service). ``source`` is a short
    static label (e.g. ``"pdf-intake"`` / ``"sectornews:auto-feed"`` /
    ``"dealtracker:extract"``) — pass NO deal name. ``audit=False`` for pure
    detection (tests / a future caller that wants the verdict without a row).
    """
    safe_source = "unknown"
    try:
        # Canonicalise the source INSIDE the guard: a pathological ``source``
        # whose ``__str__`` raises must not be the one thing that breaks the
        # never-raise contract (codex SEV-3). Label only — never a deal name (P3).
        safe_source = _safe_source(source)
        thr = heuristics.threshold()
        if not isinstance(text, str) or not text:
            return InjectionVerdict(
                flagged=False, score=0.0, source=safe_source, matched_pattern=None,
                text_chars=0, text_sha256_12=_sha256_12(text if isinstance(text, str) else ""),
                threshold=thr, bounded=False,
            )
        # early_exit_at=thr → a real injection flags on its first window (fast);
        # the precise global max above the threshold doesn't change the verdict.
        score, matched, bounded = heuristics.heuristic_score(text, early_exit_at=thr)
        flagged = score >= thr
        verdict = InjectionVerdict(
            flagged=flagged, score=score, source=safe_source,
            matched_pattern=matched if flagged else None,
            text_chars=len(text), text_sha256_12=_sha256_12(text),
            threshold=thr, bounded=bounded,
        )
        if audit and (flagged or bounded):
            _audit_detection(verdict, run_id)
        return verdict
    except Exception as e:  # noqa: BLE001 — a guard must NEVER break the workflow it inspects
        # Class only, never the exception text (which could echo the untrusted
        # input / a path / a deal name).
        logger.warning("injection scan failed for source=%s: %s", safe_source, type(e).__name__)
        return InjectionVerdict(
            flagged=False, score=0.0, source=safe_source, matched_pattern=None,
            text_chars=0, text_sha256_12="", threshold=0.0, bounded=False,
        )


def _audit_detection(verdict: InjectionVerdict, run_id: Optional[str]) -> None:
    """Append a deal-name-free row to ``runs/guards.injection.jsonl`` (+ the
    structured activity stream / SQLite) — a ``flagged`` DETECTION or an
    ``inconclusive`` (bounded, unflagged) scan. Best-effort — an audit failure
    must not break ingestion (the call already proceeded)."""
    try:
        from routines.api.deps import ROUTINES_REPO
        from routines.shared import audit

        rid = run_id or audit.new_run_id()
        detected = verdict.flagged
        audit.write_structured(
            actor={"type": "system", "id": "guard:injection"},
            entity_type="injection_scan",
            entity_id=verdict.text_sha256_12 or "unknown",
            action="injection_detected" if detected else "injection_scan_inconclusive",
            routine="guards.injection",
            run_id=rid,
            status="flagged" if detected else "inconclusive",
            audit_dir=ROUTINES_REPO / "runs",
            inputs=verdict.public_dict(),
        )
    except Exception as e:  # noqa: BLE001 — best-effort audit
        logger.warning("injection audit write failed: %s", type(e).__name__)


__all__ = ["InjectionVerdict", "scan_ingested_text"]
