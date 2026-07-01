"""The 11-step earnings pipeline (plan §7C) — orchestration.

For each watched public company that is due (step 1, done by the caller via
:func:`routines.earnings.calendar.scan_due`), this runs steps 2-11:

  2. fetch the announcement (Firecrawl)
  3. catch-up — if not yet published, return ``not_published`` and DON'T roll the
     date; the cron re-fires on a later sweep (idempotent re-run, not a sleep)
  4. extract structured fields (local Ollama)
  5. compare vs consensus (frontmatter + announcement-stated)
  6. compare vs prior period / prior year (read back from the page)
  7. render the 1-page card
  8. atomic-append the dated section to ``Companies/<name>.md``
  9. append the data point to ``Sectors/<sector>.md``
 10. emit a material-variance proposal (operator-gated) if material
 11. roll ``next-reporting-date`` forward (done inside step 8's atomic write)

Idempotency: if the period's section already exists on the page, the whole
company is a no-op (``skipped_exists``) — no duplicate append, no re-roll, no
duplicate proposal. Per-company failures are isolated so one bad company never
kills the sweep.

Clients (Ollama + Firecrawl) are injected so the bridge route / cron pass real
ones and tests pass fakes — no network in the suite.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from routines.earnings import calendar as cal
from routines.earnings import compare as cmp_mod
from routines.earnings import extract as extract_mod
from routines.earnings import materiality as mat_mod
from routines.earnings import render as render_mod
from routines.earnings import vault as vault_mod
from routines.earnings.capture import emit_variance_proposal
from routines.earnings.fetch import fetch_announcement
from routines.earnings.report import Comparison, ExtractedEarnings, PriorPeriod
from routines.shared.ollama_client import OllamaError

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Wrong-issuer hard gate (#44 Codex SEV-1)
# ─────────────────────────────────────────────────────────────────────────────
# A search-fallback fetch (no operator-pinned source URL) takes the top web hit,
# which on a busy reporting day can be a DIFFERENT company's announcement. Writing
# it to the watched note AND rolling the calendar on it corrupts the page and
# hides the real result. So before any write we confirm the issuer the model
# lifted from the announcement (``company_name`` / ``ticker``) actually matches
# the watched ``CompanyEntry``. A PINNED source URL is operator-vouched and skips
# this gate (the brief's "gate OR pinned-URL").

# ONLY true legal-form / article tokens are stripped before name comparison.
# Descriptive words like "group" / "holdings" / "international" are KEPT distinctive
# — stripping them would collapse "Acme Holdings plc" into "Acme plc" and let a
# different entity through the gate (#44 Codex re-review SEV-1).
_COMPANY_STOPWORDS = {
    "plc", "inc", "ltd", "limited", "corp", "corporation", "co", "company",
    "the", "sa", "ag", "nv", "spa", "ab", "oyj", "asa", "se", "llc", "lp", "llp",
}


def _name_tokens(name: str) -> set[str]:
    """Distinctive lowercase name tokens (corporate-form words + punctuation
    stripped). Tokens shorter than 3 chars are dropped so an initial/stopword
    can't carry a match."""
    cleaned = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return {t for t in cleaned.split() if len(t) >= 3 and t not in _COMPANY_STOPWORDS}


def _split_ticker(t: str) -> tuple[str, str]:
    """Split a ticker into ``(root, exchange)`` — "WTB.L" → ("WTB", "L"),
    "LON:WTB" → ("WTB", "LON"), "WTB LN" → ("WTB", "LN"), "WTB" → ("WTB", "").
    Keeping the exchange distinct means two DIFFERENT listings that share a root
    symbol ("ABC.L" vs "ABC.N") are NOT treated as the same issuer (#44 Codex
    SEV-1)."""
    s = (t or "").strip().upper()
    root, exch = s, ""
    if ":" in s:                       # exchange prefix, e.g. LON:WTB
        exch, _, root = s.partition(":")
    elif "." in s:                     # exchange suffix, e.g. WTB.L
        root, _, exch = s.partition(".")
    elif " " in s:                     # Bloomberg style, e.g. WTB LN
        root, _, exch = s.partition(" ")
    clean = lambda x: re.sub(r"[^A-Z0-9]", "", x)  # noqa: E731
    return clean(root), clean(exch)


def _issuer_matches(entry: "cal.CompanyEntry", company_name: str, ticker: str) -> bool:
    """True when the announcement's issuer (``company_name`` / ``ticker``)
    positively IS the watched company. Conservative by design — when we cannot
    confirm the issuer we return False so a wrong/unknown company is rejected
    rather than captured (#44 Codex SEV-1).

    Both NAME and TICKER are identity signals; the rule rejects on ANY conflict
    and accepts only with a positive match and no conflict:
      * An EXACT security id — same root AND same STATED exchange — is definitive
        and accepts outright (overrides a fuller/abbreviated name, e.g. watched
        "Acme Plc"/ACM.L vs announced "Acme Group plc"/ACM.L).
      * A ticker-ROOT conflict (ACM vs GLBX, ACM vs ACME) → reject: a different
        security is a different issuer even if a name token coincides
        ("Acme Plc"/ACM.L vs a different "Acme Inc"/ACME).
      * A distinctive-NAME conflict (tokens differ) → reject: catches a
        suffix-dropped ticker on the WRONG company ("WTB" + "Globex Corp"), and a
        one-token SUBSET ("Acme Capital" ≠ "Acme Holdings").
      * Beyond an exact security id, acceptance REQUIRES a positive distinctive-
        NAME match (with no conflicting ticker root). A ticker root alone is NOT
        enough when the exchange is absent/different and the name is missing —
        "ABC.L" vs a bare "ABC" (or "ABC.AX") for an unnamed issuer could be a
        DIFFERENT company sharing the root, so we reject rather than guess
        (#44 Codex re-review SEV-1). A name match still admits a dual-listing /
        exchange-format variance ("RIO.L Rio Tinto" vs "RIO.AX Rio Tinto")."""
    er, ee = _split_ticker(entry.ticker)
    ar, ae = _split_ticker(ticker)
    if er and er == ar and ee and ee == ae:
        return True                           # exact security id → definitive
    e_tok, a_tok = _name_tokens(entry.name or entry.stem), _name_tokens(company_name)
    if er and ar and er != ar:
        return False                          # different ticker root → different issuer
    if e_tok and a_tok and e_tok != a_tok:
        return False                          # different distinctive name → different issuer
    return bool(e_tok) and e_tok == a_tok     # positive NAME match, no conflict


def _replay_from_stored(
    entry: "cal.CompanyEntry",
    period_label: str,
    *,
    priors: list[PriorPeriod],
) -> Optional[tuple[ExtractedEarnings, Comparison]]:
    """Reconstruct ``(extracted, comparison)`` for an ALREADY-captured period from
    its frozen machine record, so a self-heal sweep (section exists but a prior
    side-effect failed) re-emits from the SAME data that's in the section — never
    from a divergent re-extraction (#44 Codex). Returns ``None`` for a legacy
    section with no machine record (caller falls back to the fresh extraction)."""
    stored = next((p for p in priors if p.period_label == period_label), None)
    # A legacy / pre-fix section has no frozen replay fields — don't trust a
    # defaulted material=False (it could silently drop a real alert). Fall back to
    # the fresh extraction in that case (the caller keeps the fresh comparison).
    if stored is None or not stored.replay_complete:
        return None
    ex = ExtractedEarnings(
        fiscal_year=stored.fiscal_year, fiscal_period=stored.fiscal_period,
        reported_date=stored.reported_date, currency=stored.currency,
        revenue_m=stored.revenue_m, revenue_yoy=stored.revenue_yoy,
        ebitda_m=stored.ebitda_m, ebitda_margin=stored.ebitda_margin, eps=stored.eps,
        guidance_change=stored.guidance_change,
        consensus_revenue_m=stored.consensus_revenue_m,
        consensus_ebitda_m=stored.consensus_ebitda_m,
        consensus_eps=stored.consensus_eps,
    )
    # Use the FROZEN consensus (baked onto ``ex.consensus_*``), NOT the live
    # frontmatter — pass an empty frontmatter so compare() falls back to the
    # stored values. Otherwise an operator consensus edit between capture and
    # self-heal would make the replayed proposal disagree with the section.
    comp = cmp_mod.compare(ex, frontmatter_consensus={}, priors=priors)
    # The materiality verdict + reasons are taken from the FROZEN capture-time
    # record, not recomputed — so the alert matches what the section reported.
    comp.material = stored.material
    comp.material_reasons = list(stored.material_reasons)
    return ex, comp

# A captured announcement's reported_date must be within ±window of the anchor
# (the scheduled due date, capped at the run date). The window is cadence-aware:
# it must stay BELOW the adjacent-period distance so a prior/next period's page is
# rejected, while tolerating a legitimately late report. Quarterly ≈ 91d apart →
# 60d; semi-annual ≈ 182d → 110d; annual ≈ 365d → 220d (#44 Codex SEV-2/3).
STALE_REPORT_WINDOW_DAYS = 60   # quarterly default (also the fallback)

_CADENCE_WINDOW_DAYS = {
    "quarterly": 60, "quarter": 60, "q": 60,
    "semi-annual": 110, "semiannual": 110, "half-year": 110, "interim": 110, "h": 110,
    "annual": 220, "yearly": 220, "fy": 220,
}


def _recency_window_days(cadence: str) -> int:
    return _CADENCE_WINDOW_DAYS.get((cadence or "quarterly").strip().lower(), STALE_REPORT_WINDOW_DAYS)


@dataclass
class CompanyOutcome:
    """Result of running the pipeline for one company."""

    company: str
    status: str               # see _STATUSES below
    section: str = ""
    material: bool = False
    proposal_path: Optional[str] = None
    next_reporting_date: Optional[str] = None
    source_url: str = ""
    detail: str = ""
    # False when the section was written but a durable side-effect (sector point /
    # material proposal) FAILED, so the date was left unrolled and the company
    # stays due for retry. The status is still "captured"/"skipped_exists" (the
    # section IS on the page), so the audit can't infer partiality from status
    # alone — this flag makes "captured-but-not-fully-durable" explicit so the run
    # audits ``partial``, not silently ``ok`` (#44 Codex SEV-2).
    side_effects_ok: bool = True


# captured       — new section appended (the happy path)
# skipped_exists — section already present (catch-up no-op)
# not_published  — announcement not yet fetchable (benign); cron re-fires later
# fetch_error    — fetch transport/search EXCEPTION (operational); cron re-fires
# extract_failed — fetched but extraction yielded no headline numbers / errored
# wrong_issuer   — search hit was a DIFFERENT company; rejected before any write
# missing_page   — company note vanished between scan and write
# contended      — page write couldn't win a clean window; deferred to next sweep
# error          — unexpected failure (isolated; sweep continues)
_STATUSES = (
    "captured", "skipped_exists", "not_published", "fetch_error",
    "extract_failed", "wrong_issuer", "missing_page", "contended", "error",
)


@dataclass
class SweepResult:
    """Aggregate result of one pipeline sweep."""

    run_date: str
    outcomes: list[CompanyOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s: 0 for s in _STATUSES}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out

    @property
    def captured(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "captured")

    @property
    def proposals(self) -> int:
        return sum(1 for o in self.outcomes if o.proposal_path)


def process_company(
    entry: cal.CompanyEntry,
    *,
    vault_root: Path,
    fc_client: Any,
    ollama_client: Any,
    thresholds: mat_mod.MaterialityThresholds,
    today: date,
    run_id: str = "",
    model: str = extract_mod.DEFAULT_MODEL,
) -> CompanyOutcome:
    """Run steps 2-11 for one company. Never raises — failures are captured in
    the returned outcome so the caller's sweep continues."""
    name = entry.name or entry.stem
    try:
        # Step 2 — fetch.
        markdown, source_url, fetched_via = fetch_announcement(entry, fc_client)
        # Step 3 — catch-up: nothing usable yet, leave the date for the next sweep.
        # Distinguish a BENIGN miss ("not published yet" → ``not_published`` =
        # terminal-ok) from an operational fetch FAILURE (transport/search
        # exception → ``fetch_error`` = partial), so a Firecrawl outage doesn't
        # masquerade as a clean sweep in the audit (#44 Codex re-review SEV-2).
        if not markdown:
            if fetched_via == "error":
                return CompanyOutcome(company=name, status="fetch_error",
                                      detail="fetch transport/search error — will retry next sweep")
            return CompanyOutcome(company=name, status="not_published",
                                  detail="announcement not yet published")

        # Step 4 — extract (local Ollama).
        try:
            extracted = extract_mod.extract_earnings(
                text=markdown, source_url=source_url, client=ollama_client, model=model,
            )
        except (OllamaError, ValueError) as e:
            log.warning("earnings: extraction failed for %s (%s)", name, e)
            return CompanyOutcome(company=name, status="extract_failed",
                                  source_url=source_url, detail=str(e))
        if not extracted.has_headline_numbers():
            return CompanyOutcome(company=name, status="extract_failed",
                                  source_url=source_url,
                                  detail="no headline figures parsed — treating as not-yet-published")
        # Wrong-issuer HARD GATE — only on the SEARCH-fallback path (bytes that
        # came from an un-vouched top web hit, per the fetch's ACTUAL provenance,
        # not a config guess). A pinned scrape is operator-vouched, so we trust
        # it; an unpinned search hit could be a same-day DIFFERENT company's RNS,
        # which we must NOT write to the watched note or roll the calendar on. On
        # a mismatch we reject before any write (#44 Codex SEV-1).
        if fetched_via == "search" and not _issuer_matches(entry, extracted.company_name, extracted.ticker):
            log.warning("earnings: %s search hit issuer mismatch (extracted name=%r ticker=%r) — rejecting",
                        name, extracted.company_name, extracted.ticker)
            return CompanyOutcome(
                company=name, status="wrong_issuer", source_url=source_url,
                detail=(f"announcement issuer (name={extracted.company_name!r} "
                        f"ticker={extracted.ticker!r}) does not match watched company"),
            )
        # Period identity required — a capture without (fiscal_year, fiscal_period)
        # has no stable idempotency key, so an "unknown-period" section would roll
        # the calendar AND risk re-appending on later runs. Treat as not-yet-clean
        # rather than capture an un-anchored section (Codex SEV-1).
        if extracted.fiscal_year is None or not extracted.fiscal_period:
            log.warning("earnings: %s extraction has no fiscal period identity — not capturing", name)
            return CompanyOutcome(company=name, status="extract_failed", source_url=source_url,
                                  detail="no fiscal period identity (year + period) parsed")
        # A reported_date is REQUIRED — without it we can't validate recency (the
        # staleness guard below would be bypassed) and the capture is low-quality.
        # Treat a missing date as not-yet-clean; the cron re-fires (Codex SEV-2).
        if extracted.reported_date is None:
            log.warning("earnings: %s extraction has no reported_date — not capturing", name)
            return CompanyOutcome(company=name, status="extract_failed", source_url=source_url,
                                  detail="no reported_date parsed")
        # Date sanity — a results announcement can't be reported AFTER the run
        # date. And it shouldn't be far before the company's EXPECTED reporting
        # date (a stale / wrong-period page a search hit might surface). The
        # staleness window is anchored on the scheduled due date, NOT the run
        # date — so a legitimate result captured during a LONG catch-up (bridge
        # outage > the window) is still accepted, while a prior-quarter page
        # (~a cadence before the due date) is rejected (Codex SEV-2).
        if extracted.reported_date > today:
            log.warning("earnings: %s reported_date %s after run date %s — suspect",
                        name, extracted.reported_date, today)
            return CompanyOutcome(company=name, status="extract_failed", source_url=source_url,
                                  detail=f"reported_date {extracted.reported_date} after run date {today}")
        # Anchor on the EARLIER of the scheduled due date and the run date. Using
        # the due date keeps a long catch-up (bridge outage past the window) valid;
        # capping at today keeps it correct if the due date has already rolled
        # forward (a result can't be stale relative to a future due date). A
        # genuine prior-quarter page is still rejected (it predates both).
        due = entry.next_reporting_date or today
        anchor = min(due, today)
        window_days = _recency_window_days(entry.cadence)
        window = timedelta(days=window_days)
        # Reject a page reported too far BEFORE the anchor (a prior-period page) OR
        # too far AFTER it (a LATER-period page a search hit might surface in a
        # long catch-up — we'd otherwise capture the wrong period and roll past the
        # one we're due for). The ±window is cadence-aware so it stays below the
        # adjacent-period distance while tolerating a late H1/FY report (#44 Codex).
        if not (anchor - window <= extracted.reported_date <= anchor + window):
            log.warning("earnings: %s reported_date %s outside ±%dd of anchor %s — suspect wrong-period page",
                        name, extracted.reported_date, window_days, anchor)
            return CompanyOutcome(company=name, status="extract_failed", source_url=source_url,
                                  detail=f"reported_date {extracted.reported_date} outside recency window of anchor {anchor}")

        # Steps 5 + 6 — compare vs consensus + priors.
        priors = vault_mod.read_prior_periods(entry.path)
        comparison = cmp_mod.compare(
            extracted, frontmatter_consensus=entry.consensus, priors=priors,
        )
        # Step 10 gate (computed now; emitted after the vault write).
        mat_mod.assess(comparison, extracted, thresholds)

        # Step 7 — render the card.
        section_md = render_mod.render_card(entry, extracted, comparison)

        # Step 8 — append the dated section (idempotent; does NOT roll the date).
        cap = vault_mod.write_company_capture(
            entry.path, entry, extracted, section_md, today=today,
            vault_root=vault_root,
        )
        if cap.status == "missing_page":
            return CompanyOutcome(company=name, status="missing_page", section=cap.section,
                                  source_url=source_url)
        if cap.status == "contended":
            # Couldn't win a clean write window (an external editor kept changing
            # the page) — defer to the next sweep rather than clobber. The company
            # stays due (no roll), so nothing is lost (#44 Codex SEV-1).
            return CompanyOutcome(company=name, status="contended", section=cap.section,
                                  source_url=source_url, detail="page contended — deferred")
        suppress_side_effects = False
        if cap.status == "skipped_exists":
            # Self-heal: the section already exists (a prior sweep wrote it). Re-run
            # the side-effects from the FROZEN stored record so a divergent
            # re-extraction can't emit an alert / sector point that disagrees with
            # the captured section (#44 Codex). ``priors`` was read before the
            # write, so it includes this already-captured period.
            replayed = _replay_from_stored(entry, extracted.period_label, priors=priors)
            if replayed is not None:
                extracted, comparison = replayed
            else:
                # Legacy section with NO frozen machine record to replay from. We
                # can't reconstruct the captured figures, so re-emitting side-
                # effects from the (possibly divergent) fresh re-extraction would
                # write a sector point / variance alert that DISAGREES with the
                # already-written section. Suppress those divergent side-effects.
                # We do NOT roll the date in this case — rolling without durable
                # side-effects would break the gate invariant and could lose a
                # legacy alert; instead the run audits PARTIAL so the operator can
                # reconcile the unreplayable legacy section manually (#44 Codex
                # re-review SEV-1). The company stays due (a fresh re-capture or an
                # operator edit resolves it).
                suppress_side_effects = True
                log.warning("earnings: %s skipped_exists with no replay-complete record (legacy) — "
                            "suppressing divergent side-effects and NOT rolling (operator must reconcile)", name)

        # Steps 9 + 10 run for BOTH "appended" and "skipped_exists" (both
        # idempotent). The date is rolled (step 11) ONLY after the durable
        # side-effects (sector point + material proposal) succeed — so if the
        # section was written but a side-effect FAILED, the date stays put, the
        # company remains DUE, and the next sweep retries the whole capture,
        # re-doing the lost side-effect. Without this gate the roll would hide the
        # company from scan_due and the sector point / alert would be lost forever
        # (#44 Codex SEV-2).
        sector_ok = True
        if entry.sector and not suppress_side_effects:
            try:
                sector_path = vault_root / "Sectors" / f"{entry.sector}.md"
                bullet = render_mod.render_sector_point(entry, extracted)
                dedup_key = f"Companies/{entry.stem}|{entry.name}]] {extracted.period_label}"
                sres = vault_mod.append_sector_point(
                    sector_path, entry.sector, bullet, dedup_key=dedup_key,
                    vault_root=vault_root,
                )
                # "appended"/"skipped_duplicate" are durable; "contended" is
                # retry-worthy — keep the company due so the next sweep retries.
                sector_ok = sres.get("status") in ("appended", "skipped_duplicate")
            except Exception as e:  # noqa: BLE001 — transient FS error: keep due, retry
                sector_ok = False
                log.warning("earnings: sector append failed for %s (%s) — "
                            "leaving next-reporting-date unrolled for retry", name, e)

        proposal_path: Optional[str] = None
        proposal_ok = True
        if comparison.material and not suppress_side_effects:
            try:
                res = emit_variance_proposal(
                    entry, extracted, comparison,
                    vault_root=vault_root, run_id=run_id,
                )
                proposal_path = str(res.path) if res.path else None
                # Roll only when the alert is DURABLE (written / triaged / unsafe).
                # An ``unparseable`` existing slot is retry-worthy → don't roll.
                proposal_ok = res.durable
            except Exception as e:  # noqa: BLE001 — emit FAILED: keep the company due
                proposal_ok = False
                log.warning("earnings: variance proposal emit failed for %s (%s) — "
                            "leaving next-reporting-date unrolled for retry", name, e)

        # Durability of this sweep's side-effects: a real failure (sector/proposal)
        # OR a legacy section we deliberately could NOT faithfully re-emit. Either
        # way the side-effects are NOT durably in place, so we must NOT roll.
        side_effects_ok = (proposal_ok and sector_ok) and not suppress_side_effects

        # Step 11 — roll the date only once the durable side-effects succeeded.
        # Pass the date we dispatched on so roll_reporting_date can detect (and
        # respect) a concurrent operator edit to the calendar during the long run.
        next_date: Optional[str] = None
        if side_effects_ok:
            rolled = vault_mod.roll_reporting_date(
                entry.path, entry, extracted, today=today,
                expected_current=entry.next_reporting_date,
                vault_root=vault_root,
            )
            next_date = rolled.isoformat() if rolled else None

        status = "captured" if cap.status == "appended" else cap.status
        detail = ""
        if cap.status == "skipped_exists":
            detail = "period already captured"
        if suppress_side_effects:
            detail = ("period already captured (legacy record — no replay; side-effects "
                      "suppressed, date NOT rolled — operator to reconcile)")
        elif not (proposal_ok and sector_ok):
            detail = "durable side-effect (sector/proposal) failed — date left unrolled for retry"
        return CompanyOutcome(
            company=name, status=status, section=cap.section,
            material=(comparison.material and not suppress_side_effects),
            proposal_path=proposal_path,
            next_reporting_date=next_date, source_url=source_url, detail=detail,
            side_effects_ok=side_effects_ok,
        )
    except Exception as e:  # noqa: BLE001 — one company must not kill the sweep
        log.exception("earnings: unexpected failure for %s", name)
        return CompanyOutcome(company=name, status="error", detail=f"{type(e).__name__}: {e}")


def run_sweep(
    *,
    vault_root: Path,
    today: date,
    fc_client: Any,
    ollama_client: Any,
    only: Optional[str] = None,
    include_overdue: bool = True,
    run_id: str = "",
    model: str = extract_mod.DEFAULT_MODEL,
) -> SweepResult:
    """Step 1 + the per-company loop. ``only`` restricts the sweep to a single
    company (matched on note stem or name, case-insensitive) for a manual fire."""
    companies_dir = vault_root / "Companies"
    due = cal.scan_due(companies_dir, today, include_overdue=include_overdue)

    if only:
        needle = only.strip().lower()
        due = [e for e in due if e.stem.lower() == needle or (e.name or "").lower() == needle]

    thresholds = mat_mod.load_thresholds(vault_root)
    result = SweepResult(run_date=today.isoformat())
    for entry in due:
        outcome = process_company(
            entry, vault_root=vault_root, fc_client=fc_client, ollama_client=ollama_client,
            thresholds=thresholds, today=today, run_id=run_id, model=model,
        )
        result.outcomes.append(outcome)
    return result


__all__ = ["CompanyOutcome", "SweepResult", "process_company", "run_sweep"]
