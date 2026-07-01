"""Ticker-multiples skill bridge route (#21-ticker-multiples).

``POST /api/workflows/ticker-multiples`` — a lightweight, current trading-
multiples quick-look on one or more PUBLIC tickers. Renamed from the unbuilt
SESSION-27B ``comps-pull`` per ``COMPS-REDESIGN-2026-06-01.md``: a dashboard
data helper, NOT a deliverable build. FIREWALLED from the valuation Comps
template, the precedent-transactions tracker, and the deal Valuation folder.

The handler:

  1. Reads the ticker-multiples skill's frontmatter from the registry —
     workspace_scope (any), sensitivity (internal), cost caps. No inlined
     constants.
  2. Validates each ticker as a PUBLIC identifier (defence in depth — the
     symbol is sent verbatim to external providers; mirrors markets.py /
     equity_research.py).
  3. Wraps the in-process ``build_ticker_multiples`` call in
     ``tool_call_hooks`` so the central ``enforce_skill_sensitivity`` guard
     (#61) fires on the ``@before_tool_call`` path. For this skill
     (``workspace_scope: any``, ``sensitivity: internal``) the guard is a
     NO-OP for the common case; the only firing path is the §5.2 cross-skill
     MNPI gate → 403.
  4. Calls ``build_ticker_multiples()`` which REUSES
     ``markets.comps.build_comps`` per ticker (target + peers) — no provider
     plumbing is duplicated. By default writes NOTHING to the vault
     (``write_note=False``); the returned snapshot IS the deliverable.

The pre-existing ``POST /api/workflows/comps`` route (in ``routes/markets.py``)
stays live as the canonical direct-caller snapshot endpoint. This workflow
route is the §14-governed surface that flows through the central guard, on top
of the SAME ``build_comps`` call. Authored as a SEPARATE file from
``markets.py`` for concurrency safety.

The audit row (``runs/tool.ticker-multiples.jsonl``) is written by the
``audit_tool_call`` after-hook on the same ``tool_call_hooks`` path.
"""

from __future__ import annotations

import logging
import time
from datetime import date as _date
from typing import Literal

from fastapi import APIRouter, HTTPException

from routines.shared import audit
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id
from routines.skills.ticker_multiples.scripts.ticker_multiples import (
    TICKER_PATTERN,
    TickerMultiplesInput,
    TickerMultiplesResult,
    build_ticker_multiples,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ── Public-ticker validation (mirrors markets.py / equity_research.py) ───────
#
# The pattern is the canonical ``TICKER_PATTERN`` from the skill module (single
# source of truth — ``build_ticker_multiples`` enforces the SAME pattern at the
# #no-mnpi-to-cloud boundary, was cited as §5.4). The route layer raises a
# user-facing 400; the skill raises a
# ValueError for any direct caller that bypasses this route.


def _validate_symbols(raw: list[str]) -> list[str]:
    """Normalise + validate every requested ticker as a PUBLIC identifier.

    Rejects the whole request (400) if any entry is non-public — a deal
    codename / target name slipping into a public-provider call is exactly the
    leak this guards against."""
    out: list[str] = []
    for raw_sym in raw:
        s = (raw_sym or "").strip().upper()
        if not s:
            continue
        if not TICKER_PATTERN.fullmatch(s):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Symbol rejected as non-public identifier: {s!r}. "
                    "Ticker-multiples accepts only public tickers (e.g. "
                    "JDW.L, IHG.L, ^FTSE). Never pass deal codenames, "
                    "target names, or buyer names."
                ),
            )
        out.append(s)
    if not out:
        raise HTTPException(status_code=400, detail="at least one ticker required")
    return out


# ── request model ─────────────────────────────────────────────────────────────


class TickerMultiplesRequest(TickerMultiplesInput):
    """On-demand ticker-multiples request from the dashboard or Cmd-K.

    Extends the routine's :class:`TickerMultiplesInput` (tickers + peers_limit
    + years + write_note) with the conventional workspace fields (#61). For
    this any-scope, internal skill they pass through the central guard without
    effect except for MNPI inputs, which the guard refuses (403)."""

    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


# ── route ──────────────────────────────────────────────────────────────────────


@router.post("/ticker-multiples", response_model=TickerMultiplesResult)
@anton_skill("ticker-multiples")
def run_workflow_ticker_multiples(req: TickerMultiplesRequest) -> TickerMultiplesResult:
    """Run a ticker-multiples snapshot on demand. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks``, lifecycle, dedup,
    ``SkillScopeRefused``→403). This body is just the analysis. Behaviour-identical."""
    symbols = _validate_symbols(req.tickers)
    run_id = current_run_id() or audit.new_run_id()
    as_of = _date.today().isoformat()
    t0 = time.monotonic()

    # Re-build the routine input from the validated symbols (the request also
    # carries workspace fields, which the routine input doesn't need).
    routine_input = TickerMultiplesInput(
        tickers=symbols,
        peers_limit=req.peers_limit,
        years=req.years,
        write_note=req.write_note,
    )

    try:
        result = build_ticker_multiples(
            routine_input,
            run_id=run_id,
            as_of=as_of,
        )
    except Exception as e:  # noqa: BLE001 — provider/build errors → 500
        log.error("ticker-multiples build failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"ticker-multiples build failed: {e}",
        )

    result.duration_ms = int((time.monotonic() - t0) * 1000)
    return result
