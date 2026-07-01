"""Comps-build skill bridge route (#21-comps, per COMPS-REDESIGN-2026-06-01).

``POST /api/workflows/comps-build`` — operator-gated 4-stage pipeline. NOT to
be confused with ``/api/workflows/comps`` (the ticker-multiples legacy snapshot
that lives in ``routes/markets.py``); the two surfaces are deliberately split
per the redesign rename.

The handler:

  1. Reads the comps skill's frontmatter from the registry — workspace_scope
     (project), sensitivity (internal), cost caps.
  2. Wraps the stage dispatch in ``tool_call_hooks`` so the central
     ``enforce_skill_sensitivity`` guard (#61) fires on the
     ``@before_tool_call`` path: it refuses non-project workspaces (the LBO
     pattern) and MNPI inputs (cross-skill gate).
  3. Refuses Stage 1+ calls without the prior-stage approval token — the
     gate IS the contract (HTTP 422). Stage 2 / Stage 3 calls without the
     full chain of tokens get the same refusal.
  4. On Stage 3 complete, fires the #76 capture (best-effort — a capture
     miss does not fail the deliverable; the workbook already succeeded).

The audit row (``runs/tool.comps-build.jsonl``) is written by the
``audit_tool_call`` after-hook on the same ``tool_call_hooks`` path.
"""

from __future__ import annotations

import logging
from datetime import date as _date

from fastapi import APIRouter, HTTPException

from routines.api.deps import VAULT
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.capture import emit_deliverable_proposal
from routines.skills.comps.scripts.comps import (
    CompsBuildInput,
    CompsSkillError,
    MissingApprovalToken,
    StageResult,
    TargetBriefMissing,
    TemplateStampFailed,
    UnsourcedFigureError,
    new_run_id,
    run as run_comps,
)
from routines.skills.registry import load_skill_metadata

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


@router.post("/comps-build", response_model=StageResult)
@anton_skill("comps", capture=False)
def run_workflow_comps_build(inputs: CompsBuildInput) -> StageResult:
    """Run one stage of the comps-build pipeline. See module docstring for
    the gate / error contract.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks`` workspace-scope/MNPI gate →
    403, lifecycle, dedup, ctx.result). The wrapper resolves the skill registry
    key from the decorator name ``comps`` (the URL path is ``comps-build``).

    ``capture=False`` is REQUIRED here, not defensive: comps is MULTI-STAGE
    (Stage 0-3) and must #76-capture ONLY on ``result.stage == "complete"``. The
    wrapper's auto-capture fires unconditionally on every Completed segment, so
    it would wrongly propose on every intermediate stage. The conditional
    hand-rolled capture is KEPT in the body, which is why the body retains its
    OWN ``load_skill_metadata("comps")`` lookup (for the capture's
    ``captures_to_vault`` + ``sensitivity``; the wrapper loads its own copy for
    governance). The capture stays in-body so it reads the route module's
    ``VAULT`` binding (unchanged from the hand-rolled version).

    Behaviour-identical: the HMAC/stage-gate logic and the comps ``run_id``
    (``new_run_id()`` — comps' OWN minting, part of the stage execution identity,
    NOT the audit/request id) are untouched; every inner HTTPException (422
    gate / Iron-Law-piece-1 / target-brief, 502 stamp, 500 skill-error) passes
    through unchanged. PRECEDENCE (operator-accepted 2026-06-08, governance-
    first): the wrapper runs the workspace-scope/MNPI gate before the body, so a
    doubly-invalid request (missing token AND non-project/MNPI) returns 403
    before the body's 422; single-fault contracts unchanged."""
    # The body keeps its OWN registry lookup — the conditional #76 capture below
    # needs meta.captures_to_vault + meta.sensitivity. (The wrapper loads its own
    # copy for governance; this one is for the capture template.)
    meta = load_skill_metadata("comps")

    # Stage-gate refusal happens INSIDE the skill code (MissingApprovalToken);
    # the central guard (now owned by the wrapper) handles workspace_scope +
    # MNPI BEFORE this body runs.
    rid = new_run_id()
    today = _date.fromisoformat(inputs.today) if inputs.today else None

    try:
        result = run_comps(
            inputs,
            vault_root=VAULT,
            run_id=rid,
            today=today,
        )
    except MissingApprovalToken as e:
        # The propose/approve gate IS the contract; surface as 422
        # with the operator-readable refusal message.
        raise HTTPException(status_code=422, detail=str(e))
    except UnsourcedFigureError as e:
        # Iron Law piece 1 fired pre-stamp — no workbook produced.
        raise HTTPException(status_code=422, detail=str(e))
    except TargetBriefMissing as e:
        raise HTTPException(status_code=422, detail=str(e))
    except TemplateStampFailed as e:
        raise HTTPException(status_code=502, detail=str(e))
    except CompsSkillError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # #76 — deliverable→vault capture, fired ONLY on Stage 3 complete.
    # Best-effort: the workbook already succeeded, so a capture miss
    # must NOT fail the run — log and return the result regardless.
    if result.stage == "complete" and meta.captures_to_vault is not None:
        try:
            flat = result.model_dump()
            # The capture template uses {target} (CapturesToVault aliases
            # deal_name → target in flatten_result, but our wire shape
            # already carries `target` as a distinct field, so this is
            # explicit).
            emit_deliverable_proposal(
                "comps",
                meta.captures_to_vault,
                flat,
                vault_root=VAULT,
                sensitivity=meta.sensitivity,
            )
        except Exception:  # noqa: BLE001 — capture is non-critical
            log.warning(
                "comps-build: deliverable→vault capture failed for deal %r "
                "(run_id=%s) — deliverable unaffected",
                result.deal_name, result.run_id, exc_info=True,
            )

    return result
