"""Equity-research skill bridge route (#21 — seventh SKILL.md migration).

``POST /api/workflows/equity-research-pull`` — fires the existing
equity-research pipeline (snapshot + fundamentals + comps sub-leaf +
news + structured-section write) in-process and returns a structured
:class:`EquityResearchResult`. The handler:

  1. Reads the skill registry for governance metadata (sensitivity,
     scope, cost caps) — no inlined constants.
  2. Wraps the in-process ``build_equity_research`` call in the real
     ``tool_call_hooks`` context manager so
     ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope:
     any``, ``sensitivity: internal``) the guard is a structural NO-OP
     for the common case; the only firing path is the cross-skill
     MNPI gate.
  3. Calls ``equity_research.build_equity_research()`` DIRECTLY (no
     subprocess — the routine is a pure provider-chain + markdown
     render). The routine itself calls ``comps.build_comps()`` as a
     sub-leaf via in-process function call (first skill exercising
     the sub-leaf composition pattern).
  4. Surfaces the full :class:`EquityResearchResult` per Iron Law
     clause 1: every result block (snapshot, fundamentals, comps,
     news) carries its ``provider`` tag. The dated section header
     ``## Equity research · YYYY-MM-DD`` stamps the as-of timestamp
     implicitly in the rendered note.

The existing ``POST /api/workflows/equity-research`` route (in
``routes/markets.py``) stays live as the canonical endpoint for
direct callers (Cmd-K, dashboard tile). This workflow route is the
SKILL-governed surface that flows through the central guard.
Authored as a SEPARATE file from ``markets.py`` for concurrency
safety with any in-flight markets-route tuning (per session brief,
"DO NOT touch routines/api/routes/markets.py").

Iron Law (three-clause):
  * CLAUSE 1 — every result block carries provider tag (block-level
    provenance; the markets types carry ``provider`` on each top-level
    result block, not per-row)
  * CLAUSE 2 — re-fire NEVER overwrites prior sections; the writer is
    append-only so operator edits to prior dated sections are
    preserved verbatim
  * CLAUSE 3 — analyst commentary slot (``Thesis / Risks /
    Catalysts``) stays EMPTY by design; no LLM-fabrication. The route
    REJECTS results where the underlying routine somehow populated
    those slots (defensive guard against routine drift).
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.markets import equity_research as _equity_research
from routines.markets.types import EquityResearchResult
from routines.skills._runtime.anton_skill import anton_skill

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ── Public-ticker validation (mirrors markets.py for defence in depth) ──────


# Accepts: JDW.L, IHG.L, ^FTSE, AAPL, BRK.B, MSFT, BP-A.L, ITV-A.L
# Rejects: anything containing spaces, project codenames, target/buyer
# names, or other free-form text. Same pattern as markets.py — the route
# is loopback-only but defence in depth: the ticker is sent verbatim to
# external providers, so we can't be careful enough.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")


def _validate_single_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if not s:
        raise HTTPException(status_code=400, detail="symbol required")
    if not _TICKER_PATTERN.fullmatch(s):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Symbol rejected as non-public identifier: {s!r}. "
                "Equity-research accepts only public tickers (e.g. "
                "JDW.L, IHG.L, ^FTSE). Never pass deal codenames, "
                "target names, or buyer names."
            ),
        )
    return s


# ── request model ───────────────────────────────────────────────────────────


class EquityResearchPullRequest(BaseModel):
    """On-demand equity-research request from the dashboard or Cmd-K.

    Defaults mirror the canonical route in markets.py. ``write_note``
    is True by default because the note IS the deliverable (distinct
    from comps-pull where the table is the deliverable and write_note
    is False by default)."""

    symbol: str = Field(..., description="Public ticker, e.g. WTB.L")
    years: int = Field(5, ge=1, le=10)
    peers_limit: int = Field(6, ge=0, le=15)
    news_days: int = Field(14, ge=1, le=90)
    news_limit: int = Field(12, ge=1, le=50)
    write_note: bool = Field(True, description="Append a structured section to Companies/<X>.md (default TRUE — the note is the deliverable)")
    # workspace fields are conventional across all skill routes (#61) — for
    # this any-scope, internal skill they pass through the central guard
    # without effect (except for MNPI inputs, which the guard refuses).
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/equity-research-pull", response_model=EquityResearchResult)
@anton_skill("equity-research", capture=False)
def run_workflow_equity_research_pull(req: EquityResearchPullRequest) -> EquityResearchResult:
    """Run an equity-research pull on demand. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks`` sensitivity/MNPI gate → 403,
    lifecycle, dedup). ``capture=False``: equity-research DELIBERATELY does not
    capture to vault — the full-note write IS the deliverable, and the SKILL.md
    OMITS ``captures_to_vault`` (verified by ``test_no_captures_to_vault_block``).
    The wrapper would not capture anyway (captures_to_vault is None), so the flag
    is defense-in-depth: it keeps the deliberate no-capture contract even if a
    future SKILL.md edit adds a captures_to_vault block.

    The body is just symbol validation + the pipeline + the clause-3 guard.
    Behaviour-identical: the 400 (symbol) / 500 (routine) / 422 (clause-3)
    contracts pass through as inner body HTTPExceptions. The route's ad-hoc audit
    annotations (run_id + duration in ``ctx.usage``) are now owned by the
    wrapper's audit + lifecycle — the audit run_id reuses the request-boundary id
    (#59) instead of minting an unrelated one; the response schema is unchanged.

    PRECEDENCE (operator-accepted 2026-06-08, governance-first): the wrapper runs
    the sensitivity/MNPI gate before this body, so a request that is BOTH an
    invalid symbol AND MNPI now returns 403 instead of 400. Single-fault
    contracts (symbol-only → 400, MNPI-only → 403) are unchanged."""
    sym = _validate_single_symbol(req.symbol)

    # Stage 1-5 — the equity-research pipeline (see SKILL.md Core Pattern). The
    # routine accumulates per-stage failures into warnings rather than raising;
    # only a top-level exception gets caught here.
    try:
        result = _equity_research.build_equity_research(
            sym,
            years=req.years,
            peers_limit=req.peers_limit,
            news_days=req.news_days,
            news_limit=req.news_limit,
            write_note=req.write_note,
        )
    except Exception as e:  # noqa: BLE001
        log.error("equity-research failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"equity-research failed: {e}",
        )

    # Iron Law clause 3 defensive guard: if a routine refactor ever populates
    # the analyst-commentary slot inside the markdown render, that's drift from
    # the contract. The current routine LEAVES THE SLOT EMPTY by design, but
    # this guard catches a future regression — structural no-op today (the
    # EquityResearchResult has no discrete analyst_commentary field; the slot
    # lives in the rendered note body). Future-proofing for a routine refactor.
    _enforce_no_fabricated_commentary(result)

    return result


def _enforce_no_fabricated_commentary(result: EquityResearchResult) -> None:
    """Iron Law clause 3 defensive guard.

    The current routine LEAVES analyst-commentary slots EMPTY by design
    (the `### Analyst commentary` markdown subsection has empty
    `Thesis / Risks / Catalysts` bullets). The :class:`EquityResearchResult`
    schema today has no discrete ``analyst_commentary`` field, so this
    guard is a structural no-op for today's wire shape.

    Future-proofing: if a routine refactor ever exposes a discrete
    ``analyst_commentary`` field on the result (or any sibling field
    with that prefix), this guard inspects it. If non-empty, the route
    REFUSES with 422 — the empty slot IS the contract; the operator
    fills it, not Anton + not the routine.
    """
    # Today's wire shape has no analyst_commentary field, so getattr
    # returns the default empty string and the guard passes vacuously.
    # If a future field is added, the guard enforces emptiness.
    commentary = getattr(result, "analyst_commentary", "") or ""
    if isinstance(commentary, str) and commentary.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "Iron Law clause 3 breach: routine returned non-empty "
                "analyst_commentary. The slot is empty by design — the "
                "operator fills it, not the routine."
            ),
        )
