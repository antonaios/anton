"""Step 10 — material-variance Inbox proposal (operator-gated).

When the compare step flags a material variance, the routine emits one
operator-gated proposal carrying the variance headline back toward the vault —
the same #76 substrate the deal-tracker (#43) reuses, written into
``Routines/earnings-variance/`` so it surfaces in
``GET /api/proposals/pending`` and routes through the standard proposals
lifecycle (Review and Route / Reject / Skip / Request revision). It is NEVER
auto-routed — the operator decides whether the variance flag lands on the
company page.

#44-earnings-variance-chip: the variance alert gets its OWN Inbox chip. The
proposal lands in the dedicated ``Routines/earnings-variance/`` directory, which
``_PROPOSAL_DIRS`` (in ``routines/api/routes/proposals.py``) maps to the
``earnings-variance`` kind — and the pending scanner derives the chip-kind from
the DIRECTORY, so the new dir → new chip automatically. Structurally the
proposal still carries ``type: deliverable-outcome`` frontmatter (plus
``kind: earnings-variance`` + ``skill: earnings`` as the semantic discriminator),
so on Route the existing ``_route_deliverable_outcome`` handler appends the
``headline`` under ``section`` on ``target`` unchanged. The earlier shared
``deliverable-outcome`` chip is no longer used by this routine; the
deliverable-outcome routing for the #76 skills/deal-tracker/comps producers is
untouched.

Best-effort: a capture miss logs and returns ``None`` — the results section has
already been written to the vault, so a proposal failure must never fail the
run (mirrors #43 / #76 discipline).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import frontmatter

from routines.earnings.calendar import CompanyEntry
from routines.earnings.report import Comparison, ExtractedEarnings
from routines.skills._runtime.capture import SKIP_STATUSES, _slug

log = logging.getLogger(__name__)

VARIANCE_KIND = "earnings-variance"
VARIANCE_SECTION = "Earnings variance alerts"

# #44-earnings-variance-chip — the variance alert lands in its OWN proposal
# directory so it surfaces under a dedicated ``earnings-variance`` Inbox chip
# (the pending scanner derives the chip-kind from the DIRECTORY, see
# ``_PROPOSAL_DIRS`` in ``routines/api/routes/proposals.py``). It still carries
# ``type: deliverable-outcome`` frontmatter so Route reuses the #76 append-to-note
# handler unchanged. This is deliberately NOT ``PROPOSAL_DIR_REL`` (the shared
# ``Routines/deliverable-outcomes`` dir used by the #76 skills runtime, the
# deal-tracker, and comps) — routing those is untouched.
EARNINGS_VARIANCE_DIR_REL = "Routines/earnings-variance"


@dataclass
class EmitResult:
    """Outcome of a variance-proposal emit. The explicit status lets the caller
    decide whether the alert is DURABLE enough to roll the calendar past it, vs
    retry-worthy (#44 Codex SEV-2)."""

    status: str               # written | exists | triaged | unparseable | unsafe | not_material
    path: Optional[Path] = None

    @property
    def durable(self) -> bool:
        """True when the alert is durably resolved — safe to roll the reporting
        date. ``written`` (fresh file), ``exists`` (a parseable proposal is
        already filed at this idempotent slot — never overwritten), ``triaged``
        (operator already actioned the slot), ``unsafe`` (undeliverable note name
        — can NEVER be filed, so don't wedge the calendar forever), and
        ``not_material`` are all terminal. ``unparseable`` is NOT durable — a
        corrupt existing slot is left for a later retry (and the operator to
        notice) rather than silently rolled past."""
        return self.status in ("written", "exists", "triaged", "unsafe", "not_material")

# Earnings data is public (published results), so the proposal is public-tier —
# distinct from the deal-tracker's "internal" default. Extraction still ran
# locally (#no-mnpi-to-cloud — was cited as §5.4) but the captured conclusion
# is public.
_SENSITIVITY = "public"


def _safe_note_name(name: str) -> bool:
    """A vault note stem must be a single safe path segment."""
    n = (name or "").strip()
    return bool(n) and ".." not in n and not any(c in n for c in ("/", "\\", ":"))


def variance_headline(entry: CompanyEntry, extracted: ExtractedEarnings, comparison: Comparison) -> str:
    """One-line conclusion for the proposal + the routed bullet."""
    reasons = "; ".join(comparison.material_reasons) or "material variance"
    return f"{entry.name} {extracted.period_label} — {reasons}"


def _proposal_body(headline: str, *, target: str, section: str, reasons: list[str], now: datetime) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    bullet_lines = "\n".join(f"- {r}" for r in reasons) if reasons else "- material variance"
    return (
        f"# {headline}\n\n"
        f"## Material variance\n\n"
        f"{bullet_lines}\n\n"
        f"On **Route**, this is appended (append-only — CLAUDE.md §3 rule 9) to "
        f"`{target}` under the `{section}` section.\n\n"
        f"*Emitted by the `earnings` tracker variance alert (#44 step 10) on {ts}. "
        f"Routes through the standard proposals lifecycle (#8 + #58): "
        f"Review and Route / Reject / Skip / Request revision.*\n"
    )


def _metric_fields(comparison: Comparison) -> dict[str, Any]:
    """Compact machine fields for the proposal record (the consensus variances)."""
    out: dict[str, Any] = {}
    for ln in comparison.vs_consensus:
        out[f"{ln.metric}_vs_consensus_pct"] = (
            round(ln.delta_pct, 4) if ln.delta_pct is not None else None
        )
    return out


def emit_variance_proposal(
    entry: CompanyEntry,
    extracted: ExtractedEarnings,
    comparison: Comparison,
    *,
    vault_root: Path,
    run_id: str = "",
    now: Optional[datetime] = None,
) -> EmitResult:
    """Emit one operator-gated variance proposal. Returns an :class:`EmitResult`
    whose ``status`` tells the caller whether the alert is durable (``written`` /
    ``triaged``), undeliverable (``unsafe``), or retry-worthy (``unparseable``) —
    so the pipeline rolls the calendar only when the alert is truly resolved
    (#44 Codex SEV-2). Raises only on a genuine transient write failure, which the
    pipeline treats as retry-worthy."""
    if not comparison.material:
        return EmitResult(status="not_material")
    if not _safe_note_name(entry.stem):
        log.info("earnings capture: %r not an addressable note name — skipping", entry.stem)
        return EmitResult(status="unsafe")

    now = now or datetime.now(timezone.utc)
    fact_date: date = extracted.reported_date or now.date()
    date_str = fact_date.isoformat()
    headline = variance_headline(entry, extracted, comparison)
    target_rel = f"Companies/{entry.stem}.md"
    provenance = f"runs:earnings.{run_id}" if run_id else "runs:earnings"

    proposal_dir = vault_root / EARNINGS_VARIANCE_DIR_REL
    slug = _slug(entry.name or entry.stem)
    period = _slug(extracted.period_label)
    # Stable disambiguator from the note STEM (the unique vault filename) so two
    # companies whose DISPLAY names slugify the same don't share a proposal slot
    # and silently suppress each other's alerts (#44 Codex SEV-2). Re-emitting the
    # same company+period yields the same suffix, so slot idempotency still holds.
    disambig = hashlib.sha1((entry.stem or entry.name).encode("utf-8")).hexdigest()[:8]
    path = proposal_dir / f"{date_str}-{slug}-{period}-{disambig}-earnings-variance.md"

    metadata: dict[str, Any] = {
        "type": "deliverable-outcome",      # routes via the #76 handler (directory-derived kind)
        "kind": VARIANCE_KIND,              # semantic kind (#44 step 10)
        "skill": "earnings",
        "status": "pending-review",
        "date": date_str,
        "target": target_rel,
        "section": VARIANCE_SECTION,
        "headline": headline,
        "fields": {
            "ticker": entry.ticker,
            "period_label": extracted.period_label,
            "reported_date": date_str,
            "guidance_change": extracted.guidance_change,
            "material_reasons": comparison.material_reasons,
            **_metric_fields(comparison),
        },
        "provenance": provenance,
        "run_id": run_id,
        "sensitivity": _SENSITIVITY,
        "tldr": headline,
    }
    body = _proposal_body(
        headline, target=target_rel, section=VARIANCE_SECTION,
        reasons=comparison.material_reasons, now=now,
    )
    return _write_proposal(path, metadata=metadata, body=body)


def _classify_existing(path: Path) -> EmitResult:
    """Classify an EXISTING proposal slot WITHOUT touching it (exclusive-create /
    never-overwrite). ``triaged`` (operator-actioned, durable), ``unparseable``
    (corrupt / NOT one of our proposals — NOT durable, retry-worthy), or ``exists``
    (a parseable variance proposal already filed at this idempotent slot — durable;
    re-emitting must NOT clobber it, which would lose operator edits — #44 Codex
    SEV-2).

    A file at the deterministic slot is only treated as DURABLE when it is
    actually one of OUR variance proposals (``kind == VARIANCE_KIND``). A foreign
    file, or a crash-partial with no/blank frontmatter, is NOT durable — otherwise
    a half-written or unrelated file could suppress a genuinely-unwritten alert
    and let the calendar roll past it (#44 Codex re-review SEV-1)."""
    try:
        existing = frontmatter.load(path)
    except Exception as e:  # noqa: BLE001 — unreadable existing file
        # Can't confirm it's NOT operator-triaged, so don't stomp it — and it's
        # NOT durable, so the caller leaves the company due for a later retry
        # rather than rolling past a corrupt slot (#44 Codex SEV-2).
        log.warning("earnings capture: existing %s unparseable (%s) — skipping (not overwriting)", path, e)
        return EmitResult(status="unparseable", path=path)
    meta = existing.metadata if isinstance(existing.metadata, dict) else {}
    if str(meta.get("kind") or "").strip() != VARIANCE_KIND:
        # Parseable but NOT one of our variance proposals (foreign file or a
        # crash-partial with no frontmatter) — don't overwrite it, but don't treat
        # it as a durable alert either (so the caller won't roll past the real,
        # still-unwritten alert).
        log.warning("earnings capture: existing %s is not an earnings-variance proposal "
                    "(kind=%r) — not durable, leaving for the operator", path, meta.get("kind"))
        return EmitResult(status="unparseable", path=path)
    status = str(meta.get("status") or "").strip().lower()
    if status in SKIP_STATUSES:
        log.info("earnings capture: skipping %s — operator-triaged status=%r", path, status)
        return EmitResult(status="triaged", path=path)
    log.info("earnings capture: proposal slot %s already exists (status=%r) — leaving untouched", path, status)
    return EmitResult(status="exists", path=path)


def _write_proposal(path: Path, *, metadata: dict[str, Any], body: str) -> EmitResult:
    """EXCLUSIVE-CREATE write of the proposal — NEVER overwrites an existing slot.
    Returns ``triaged`` (operator-actioned slot left untouched), ``unparseable``
    (corrupt existing file left untouched — retry-worthy), ``exists`` (a parseable
    proposal already filed at this idempotent slot — left untouched), or
    ``written`` (fresh file). A genuine write failure (tmp write / publish) RAISES
    so the pipeline treats it as retry-worthy and leaves the company due (mirrors
    the #43/#76 write block, with explicit statuses for #44's roll gate).

    Concurrency: the slot path is deterministic per company+period, so two
    overlapping sweeps (a scheduled run vs a manual ``/api`` fire) target the same
    file. We (1) never overwrite a slot we can see already exists, and (2) publish
    via a UNIQUE temp + an EXCLUSIVE link so the publish itself fails-if-exists —
    closing both the operator-edit clobber and the fixed-``*.tmp``-name race the
    previous fixed temp path had (#44 Codex SEV-2)."""
    if path.is_file():
        return _classify_existing(path)

    post = frontmatter.Post(body)
    post.metadata.update(metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = frontmatter.dumps(post) + "\n"
    # Per-writer UNIQUE temp name (pid + uuid) so concurrent writers never share a
    # single ``*.tmp`` and clobber each other's half-written file (#44 Codex SEV-2).
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(serialised, encoding="utf-8")   # inside try → cleaned up even if it fails
        try:
            # Atomic EXCLUSIVE publish: link fails-if-exists, so a slot another
            # writer claimed between our is_file() check and here is never stomped.
            os.link(tmp, path)
        except FileExistsError:
            # Another writer won the race — classify their slot, don't overwrite.
            return _classify_existing(path)
        except (OSError, NotImplementedError, AttributeError):
            # Hard-link unsupported on this FS — exclusive-create the final file
            # directly (O_EXCL fails-if-exists, so it STILL never overwrites; no
            # guarded-replace TOCTOU window — #44 Codex SEV-2). The brief CR-write
            # tolerance covers the tiny partial-read window the pending scanner
            # already treats as not-yet-ready.
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return _classify_existing(path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialised)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
    log.info("earnings capture: wrote %s", path)
    return EmitResult(status="written", path=path)


__all__ = [
    "VARIANCE_KIND",
    "VARIANCE_SECTION",
    "EARNINGS_VARIANCE_DIR_REL",
    "EmitResult",
    "variance_headline",
    "emit_variance_proposal",
]
