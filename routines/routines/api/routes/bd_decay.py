"""BD-decay skill bridge route (#21 — fifth SKILL.md migration).

``POST /api/workflows/bd-decay`` — fires a deterministic file-walk over
``Companies/<X>.md``, classifies every file into stale / fresh / untracked
buckets, and returns the structured :class:`BDDecayResult`. The handler:

  1. Reads the skill registry for governance metadata (sensitivity, scope,
     cost caps) — no inlined constants.
  2. Wraps the in-process scan call in the real ``tool_call_hooks`` context
     manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope: any``,
     ``sensitivity: internal``) the guard is a structural NO-OP for the
     common case; the only firing path is the cross-skill MNPI gate.
  3. Calls ``bd.decay.scan`` + ``bd.decay.format_stale_for_morning_brief``
     directly (no subprocess — the routine is sub-second on a typical
     500-company vault).
  4. Computes the Iron Law's three-bucket breakdown (``scanned``,
     ``stale``, ``fresh``, ``untracked``) by re-walking ``Companies/`` and
     classifying each file — the routine's ``scan`` returns only the
     stale list, but the route surfaces the FULL taxonomy so the operator
     can sanity-check that "untracked" did not silently collapse into
     "stale".

The existing CLI (``bd-decay scan``) + the 06:30 morning-brief cron
continue to call ``bd.decay.scan`` directly; this route is the on-demand
operator surface (dashboard tile + Cmd-K). Neither the CLI nor the cron
flows through this route.
"""

from __future__ import annotations

import logging
import time
from datetime import date as date_cls
from pathlib import Path
from typing import Literal, Optional

import frontmatter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.bd import decay as _decay
from routines.shared import audit
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ── request / response models ────────────────────────────────────────────────


OutputFormat = Literal["markdown", "json", "both"]


class BDDecayRequest(BaseModel):
    """On-demand BD-decay request from the dashboard or Cmd-K.

    Defaults mirror the cron job's behaviour (today=today, both formats
    rendered). ``today`` is an optional ISO override for testing /
    deterministic replay. ``format`` selects which of the two renderings
    to include in the response; ``both`` (the default) returns the JSON
    stale list AND the rendered markdown snippet so the dashboard can
    pick which to display."""

    today: Optional[str] = None
    format: OutputFormat = "both"
    # workspace fields are conventional across all skill routes (#61) — for
    # this any-scope, internal skill they pass through the central guard
    # without effect (except for MNPI inputs, which the guard refuses).
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class StaleEntryOut(BaseModel):
    """One stale BD entry surfaced in the response. Mirrors the routine's
    :class:`StaleEntry` dataclass; ``days_over`` is the routine's existing
    field name (= ``days_since_contact - threshold_days``). Aliased to
    ``days_overdue`` in the spec wording but the routine and tests use
    ``days_over``."""

    company_path: str
    company_name: str
    sector: str
    bd_state: str
    bd_last_contact: str
    bd_owner: str
    days_since_contact: int
    threshold_days: int
    days_over: int


class BDDecayCounts(BaseModel):
    """The three-bucket taxonomy (Iron Law: stale ≠ untracked).

    ``scanned`` is the total file count walked under Companies/;
    ``stale + fresh + untracked == scanned`` is the taxonomy-fidelity
    invariant the operator can sanity-check."""

    scanned: int
    stale: int
    fresh: int
    untracked: int


class BDDecayResult(BaseModel):
    """Structured BD-decay result. Pure-return data — distinct from
    vault-health (writes report), deal-tracker (appends Excel row).
    ``rendered_markdown`` is populated when ``format ∈ {"markdown", "both"}``.
    ``stale`` (the JSON list) is populated when ``format ∈ {"json", "both"}``."""

    status: Literal["ok", "error"]
    run_id: str
    today: str
    active_thresholds: dict[str, int] = Field(default_factory=dict)
    counts: BDDecayCounts
    stale: list[StaleEntryOut] = Field(default_factory=list)
    rendered_markdown: str = ""
    duration_ms: int = 0
    error: Optional[str] = None


# ── helpers ──────────────────────────────────────────────────────────────────


def _classify_companies(
    vault_root: Path,
    today: date_cls,
    stale_paths: set[str],
) -> BDDecayCounts:
    """Walk ``Companies/`` and bucket every file (Iron Law's taxonomy).

    The routine's ``scan`` returns ONLY the stale list; this helper does
    the second pass so the response carries the full breakdown. A file is:

      * ``stale`` — its ``company_path`` is in ``stale_paths``
      * ``untracked`` — no ``bd_state`` field, or sticky state
        (DECAY_THRESHOLDS[state] < 0), or unparseable ``bd_last_contact``
      * ``fresh`` — has ``bd_state``, non-sticky, parse-valid
        ``bd_last_contact``, but not in the stale list

    This MUST stay distinct from the stale-list logic. Collapsing
    untracked into stale is the Iron Law violation this helper exists to
    prevent. Tested.
    """
    companies_dir = vault_root / "Companies"
    if not companies_dir.is_dir():
        return BDDecayCounts(scanned=0, stale=0, fresh=0, untracked=0)

    scanned = 0
    fresh = 0
    untracked = 0
    stale_count = 0
    for f in sorted(companies_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        scanned += 1
        rel = str(f.relative_to(vault_root)).replace("\\", "/")
        if rel in stale_paths:
            stale_count += 1
            continue

        try:
            meta = frontmatter.load(f).metadata or {}
        except Exception:  # noqa: BLE001 — malformed YAML → untracked
            untracked += 1
            continue

        bd_state = meta.get("bd_state")
        if not bd_state:
            untracked += 1
            continue
        bd_state = str(bd_state).strip().lower()

        threshold = _decay.DECAY_THRESHOLDS.get(bd_state, -1)
        if threshold < 0:
            # Sticky state — never decays; treated as untracked from a
            # "needs-attention" perspective (deliberately excluded from
            # the stale sleeve).
            untracked += 1
            continue

        last_contact = meta.get("bd_last_contact")
        if not last_contact:
            # No last-contact and non-sticky state means the routine
            # already flagged it stale; this branch is reached only if
            # `scan` was mocked. Treat as untracked here to be safe — the
            # stale path above already accounted for it.
            untracked += 1
            continue

        try:
            date_cls.fromisoformat(str(last_contact))
        except (ValueError, TypeError):
            untracked += 1
            continue

        # Has bd_state, non-sticky, parseable last_contact, not in stale
        # list → it's fresh.
        fresh += 1

    return BDDecayCounts(
        scanned=scanned,
        stale=stale_count,
        fresh=fresh,
        untracked=untracked,
    )


def _entry_out(s: _decay.StaleEntry) -> StaleEntryOut:
    return StaleEntryOut(
        company_path=s.company_path,
        company_name=s.company_name,
        sector=s.sector,
        bd_state=s.bd_state,
        bd_last_contact=s.bd_last_contact,
        bd_owner=s.bd_owner,
        days_since_contact=s.days_since_contact,
        threshold_days=s.threshold_days,
        days_over=s.days_over,
    )


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/bd-decay", response_model=BDDecayResult)
@anton_skill("bd-decay")
def run_workflow_bd_decay(req: BDDecayRequest) -> BDDecayResult:
    """Run a BD-decay sweep on demand. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks``, lifecycle, dedup,
    ``SkillScopeRefused``→403). This body is just the sweep. Behaviour-identical."""
    if req.today:
        try:
            today = date_cls.fromisoformat(req.today)
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail=f"today must be ISO YYYY-MM-DD: {e}",
            )
    else:
        today = date_cls.today()

    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()

    # Stage 1 — scan. Sub-second on a typical vault; no LLM, no
    # network. The Iron Law applies at the route boundary:
    # an exception is NOT a clean pass — surface verbatim.
    try:
        stale = _decay.scan(VAULT, today=today)
    except Exception as e:  # noqa: BLE001 — scan errors map to 500
        log.error("bd-decay scan failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"bd-decay scan failed: {e}",
        )

    # Stage 2 — three-bucket taxonomy (Iron Law). Re-walk to
    # classify every Companies file; the stale-list logic in
    # `_classify_companies` keeps untracked DISTINCT from stale.
    stale_paths = {s.company_path for s in stale}
    counts = _classify_companies(VAULT, today, stale_paths)

    # Stage 3 — render. Markdown via the routine's own formatter
    # (sorted by days_over desc, top 10 + "...and N more" tail).
    rendered_markdown = ""
    if req.format in ("markdown", "both"):
        rendered_markdown = _decay.format_stale_for_morning_brief(stale)

    stale_out: list[StaleEntryOut] = []
    if req.format in ("json", "both"):
        stale_out = [_entry_out(s) for s in stale]

    return BDDecayResult(
        status="ok",
        run_id=run_id,
        today=today.isoformat(),
        active_thresholds=dict(_decay.DECAY_THRESHOLDS),
        counts=counts,
        stale=stale_out,
        rendered_markdown=rendered_markdown,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
