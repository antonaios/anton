"""Ticker-multiples snapshot build — the deterministic surface (#21-ticker-multiples).

A LIGHT, deterministic data helper: given one or more public tickers, return a
CURRENT trading-multiples snapshot for each (the target itself + a peer set) by
reusing ``routines.markets.comps.build_comps`` — the SAME orchestration the
equity-research sub-leaf uses. No provider plumbing is duplicated here.

Design constraints (firewall — per COMPS-REDESIGN-2026-06-01):
  * NEVER stamps the valuation Comps template.
  * NEVER touches the precedent-transactions tracker.
  * NEVER writes the deal Valuation folder.
  * By default writes NOTHING to the vault (``write_note=False``); the
    deliverable is the returned :class:`TickerMultiplesResult`. The optional
    ``write_note`` flag forwards to ``build_comps`` and appends a public-tier
    table to ``Companies/<X>.md`` — that is the ONLY (opt-in) side effect, and
    it is the same Companies-note path the legacy ``/api/workflows/comps``
    snapshot already used. The valuation template / tracker / deal folder are
    out of scope entirely.

Every figure carries the provider tag it was sourced from (Iron Law: source
every figure / never invent). A ticker the provider has no data for surfaces as
a row with ``None`` multiples — the snapshot NEVER fabricates a number to fill a
gap; a missing figure stays ``None`` and the absence is honest.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from routines.markets.comps import build_comps
from routines.markets.types import CompRow

log = logging.getLogger(__name__)


# ── #no-mnpi-to-cloud firewall (was cited as §5.4): public-ticker validation ─
#
# The symbol is sent VERBATIM to external market providers, so a non-public
# string (deal codename, target/buyer name, free-form text) must NEVER reach
# ``build_comps``. This is the canonical pattern; the bridge route
# (``routes/ticker_multiples.py``) imports it for its 400 guard, and
# ``build_ticker_multiples`` enforces it too so a DIRECT caller (test, cron,
# future dispatcher) can't bypass the route-level check.
# Accepts: JDW.L, IHG.L, ^FTSE, AAPL, BRK.B, MSFT, BP-A.L. Rejects: spaces,
# codenames, names, free-form text.
TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")


def is_public_ticker(symbol: str) -> bool:
    """True iff ``symbol`` is a well-formed PUBLIC ticker (e.g. ``JDW.L``)."""
    return bool(TICKER_PATTERN.fullmatch(symbol))


# ── input ────────────────────────────────────────────────────────────────────


class TickerMultiplesInput(BaseModel):
    """Request shape for a ticker-multiples snapshot.

    ``tickers`` is the load-bearing input: one or more public identifiers
    (e.g. ``["JDW.L", "IHG.L"]``). For each, the skill pulls the ticker itself
    plus a peer set and returns a current-multiples snapshot. ``peers_limit``
    bounds the peer set per ticker (0 = the target only, no peers).

    ``write_note`` is OFF by default — the snapshot is a quick-look; the
    returned data IS the deliverable. When the operator explicitly opts in, the
    flag forwards to ``build_comps`` which appends a public-tier table to
    ``Companies/<X>.md`` (the SAME Companies-note path the legacy snapshot used
    — NOT the valuation template, tracker, or deal folder).
    """

    tickers: list[str] = Field(
        ...,
        min_length=1,
        max_length=25,
        description=(
            "One or more public tickers, e.g. ['JDW.L', 'IHG.L']. Capped at 25 "
            "— this is a quick-look, not a bulk pull; the bound caps external "
            "provider calls (each ticker pulls a target + peer set)."
        ),
    )
    peers_limit: int = Field(
        8, ge=0, le=15,
        description="Peers to pull per ticker (0 = target only, no peers).",
    )
    years: int = Field(
        5, ge=1, le=10,
        description="Fundamentals history years passed to the provider.",
    )
    write_note: bool = Field(
        False,
        description=(
            "Opt-in: append a public-tier table to Companies/<X>.md. OFF by "
            "default — the snapshot is the deliverable. NEVER writes the "
            "valuation template, the tracker, or the deal Valuation folder."
        ),
    )


# ── output ───────────────────────────────────────────────────────────────────


class MultiplesRow(BaseModel):
    """One row of a ticker-multiples snapshot — the target itself first, then
    its peers. A subset of the markets :class:`CompRow`, surfaced verbatim from
    the provider. Every numeric is ``Optional`` — a figure the provider didn't
    return stays ``None`` (the snapshot never invents a value to fill a gap)."""

    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    fiscal_year: Optional[int] = None
    revenue: Optional[float] = None
    ebitda: Optional[float] = None
    ebitda_margin: Optional[float] = None
    revenue_growth_5y_cagr: Optional[float] = None
    pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    is_target: bool = False


class TickerSnapshot(BaseModel):
    """The current-multiples snapshot for one requested ticker (target + peers)."""

    target_symbol: str
    target_name: Optional[str] = None
    rows: list[MultiplesRow] = Field(default_factory=list)
    provider: Optional[str] = None
    note_path: Optional[str] = None        # set only when write_note=True
    warnings: list[str] = Field(default_factory=list)


class TickerMultiplesResult(BaseModel):
    """Structured ticker-multiples result. Pure-return data by default (no file
    write); cousin shape to the recall-query / bd-decay single-shot skills.

    One :class:`TickerSnapshot` per requested ticker, plus the run-level
    ``provider`` tag and an ``as_of`` date stamp so the operator can sanity-
    check freshness. ``warnings`` aggregates per-snapshot provider warnings at
    the run level."""

    status: str = "ok"
    run_id: str = ""
    as_of: str = ""                        # ISO date — when the snapshot was pulled
    provider: Optional[str] = None
    snapshots: list[TickerSnapshot] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int = 0


# ── build ────────────────────────────────────────────────────────────────────


def _to_multiples_row(row: CompRow, *, is_target: bool) -> MultiplesRow:
    """Project a markets :class:`CompRow` onto the snapshot row shape.

    Verbatim passthrough — no derived/invented fields. A ``None`` on the
    provider's ``CompRow`` stays ``None`` here."""
    return MultiplesRow(
        symbol=row.symbol,
        name=row.name,
        currency=row.currency,
        fiscal_year=row.fiscal_year,
        revenue=row.revenue,
        ebitda=row.ebitda,
        ebitda_margin=row.ebitda_margin,
        revenue_growth_5y_cagr=row.revenue_growth_5y_cagr,
        pe=row.pe,
        ev_ebitda=row.ev_ebitda,
        net_debt_ebitda=row.net_debt_ebitda,
        dividend_yield=row.dividend_yield,
        is_target=is_target,
    )


def build_ticker_multiples(
    inputs: TickerMultiplesInput,
    *,
    run_id: str = "",
    as_of: str = "",
) -> TickerMultiplesResult:
    """Build a current-multiples snapshot for each requested ticker.

    REUSES ``markets.comps.build_comps`` per ticker (target + peers) — the
    single source of truth for the provider chain. The first non-stub provider
    name encountered is reported at the run level; per-snapshot provider tags
    are preserved on each :class:`TickerSnapshot`.

    Deterministic + side-effect-free unless ``write_note=True`` (which forwards
    to ``build_comps`` → ``Companies/<X>.md`` only). Raises nothing for a
    missing-data ticker — that surfaces as a snapshot with ``None`` multiples
    and a provider warning, honest about the gap.
    """
    snapshots: list[TickerSnapshot] = []
    run_warnings: list[str] = []
    run_provider: Optional[str] = None

    # Normalise/dedupe tickers while preserving order (a duplicate ticker would
    # pull the same data twice for no gain).
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in inputs.tickers:
        sym = (raw or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            ordered.append(sym)

    # Defence in depth at the #no-mnpi-to-cloud boundary (was cited as §5.4):
    # the bridge route validates symbols,
    # but a DIRECT caller of this function (test, cron, future dispatcher) would
    # bypass it. Validate here too — closest to build_comps / the external
    # provider — so a non-public string can never be sent verbatim downstream.
    for sym in ordered:
        if not is_public_ticker(sym):
            raise ValueError(
                f"non-public ticker symbol rejected: {sym!r}. ticker-multiples "
                "accepts only public tickers (e.g. JDW.L, IHG.L, ^FTSE) — never "
                "deal codenames, target names, or buyer names (§5.4 firewall)."
            )

    for sym in ordered:
        comps = build_comps(
            sym,
            peers_limit=inputs.peers_limit,
            years=inputs.years,
            write_note=inputs.write_note,
        )
        if run_provider is None and comps.provider:
            run_provider = comps.provider

        rows: list[MultiplesRow] = []
        for i, r in enumerate(comps.rows):
            # build_comps puts the target row first (index 0), peers after.
            rows.append(_to_multiples_row(r, is_target=(i == 0)))

        snapshots.append(
            TickerSnapshot(
                target_symbol=comps.target_symbol,
                target_name=comps.target_name,
                rows=rows,
                provider=comps.provider,
                note_path=comps.note_path,
                warnings=list(comps.warnings),
            )
        )
        for w in comps.warnings:
            run_warnings.append(f"{sym}: {w}")

    return TickerMultiplesResult(
        status="ok",
        run_id=run_id,
        as_of=as_of,
        provider=run_provider,
        snapshots=snapshots,
        warnings=run_warnings,
    )
