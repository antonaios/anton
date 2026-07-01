"""#63 phase 2b — skill-run suspend/resume control surface.

Two endpoints over the cooperatively-suspended skill runs persisted by the
``@anton_skill`` wrapper (see :mod:`routines.skills._runtime.suspensions`):

  * ``POST /api/skills/{run_id}/resume`` — deliver the operator's answer and
    re-invoke the suspended body. Idempotent + run-bound: an atomic claim means
    a network retry / double-click can't re-run the body (409); an expired
    suspension can't resume (410); an unknown id is 404.
  * ``GET  /api/skills/suspended`` — the "waiting on you" list: pending
    suspensions that haven't lapsed, newest first (drives an inbox surface).

Sessionless by design — skill routes mint their own ``run_id`` and carry no
session, so a suspension is keyed (and resume bound) by ``run_id`` alone.

This module shares the ``/api/skills`` prefix with ``skills_providers`` (the
Tier-2 provider matrix); the paths are disjoint. Authored as a SEPARATE file so
suspend/resume can evolve without touching the provider-matrix surface.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routines.skills._runtime.anton_skill import (
    SkillResumeNotReady,
    get_skill_entry,
    resume_skill,
)
from routines.skills._runtime.suspensions import get_suspension_store

router = APIRouter(prefix="/api/skills", tags=["skills"])
log = logging.getLogger(__name__)


class ResumeRequest(BaseModel):
    """The operator's answer to a suspended skill's prompt.

    ``resume_token`` is REQUIRED — the nonce from the suspension's awaiting
    payload / the "waiting on you" list. It's the anti-ABA claim key: a stale
    resume for a prior prompt carries an old token and can't claim a freshly
    re-pended suspension that reuses the run_id.

    ``input`` is free-form (whatever the skill's resume branch expects — a
    chosen option string, a dict of edits, a confirmation flag); omit it to
    resume with no value (a bare "continue")."""

    resume_token: str = Field(..., min_length=1)
    input: Any = None


@router.post("/{run_id}/resume")
def resume_suspended_skill(run_id: str, req: ResumeRequest) -> Any:
    """Resume a suspended skill run with the operator's answer.

    Returns the skill's result (a completed run, 200) OR another 202 awaiting-
    payload (a multi-step skill that suspended again). HTTP map: 404 unknown id ·
    410 expired · 409 already resumed/discarded, stale/mismatched token, or
    unregistered skill."""
    store = get_suspension_store()
    store.sweep_expired()  # lazily age out lapsed rows so the checks below are honest

    susp = store.get(run_id)
    if susp is None:
        raise HTTPException(
            status_code=404,
            detail=f"no suspended skill run for run_id {run_id!r}",
        )
    if susp.status == "expired" or susp.is_expired():
        raise HTTPException(
            status_code=410,
            detail=(
                f"suspended run {run_id!r} expired at {susp.expires_at} — "
                "re-fire the skill to start a fresh run"
            ),
        )
    if susp.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id!r} is already {susp.status} — cannot resume twice",
        )
    # Re-invocation needs the body; a suspension whose skill isn't registered
    # this process (e.g. renamed/removed since it paused) can't resume — say so
    # BEFORE claiming, so the pending row stays resumable if the skill returns.
    if get_skill_entry(susp.skill) is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"skill {susp.skill!r} is not registered with @anton_skill in this "
                "process — cannot resume run "
                f"{run_id!r} (restart the bridge if the skill was just added)"
            ),
        )

    # Atomic claim — exactly one caller wins, and only with the matching token.
    # A failed claim → re-fetch + map precisely (the row may have flipped between
    # the read above and the claim): missing 404 · expired 410 · resumed/discarded
    # 409 · pending-but-token-mismatch (a stale/ABA resume) 409.
    if not store.claim_for_resume(run_id, req.resume_token):
        current = store.get(run_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} no longer exists")
        if current.status == "expired" or current.is_expired():
            raise HTTPException(status_code=410, detail=f"run {run_id!r} expired")
        if current.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id!r} is already {current.status} — cannot resume twice",
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"stale or mismatched resume_token for run {run_id!r} — re-fetch the "
                "current suspension (GET /api/skills/suspended) and use its token"
            ),
        )

    try:
        return resume_skill(
            susp.skill,
            run_id=run_id,
            state=susp.state,
            gov=(susp.workspace_type, susp.workspace_name, susp.sensitivity),
            operator_input=req.input,
        )
    except SkillResumeNotReady as e:
        # The claim was taken but the body was NEVER admitted — a readiness
        # precondition lapsed between suspend and resume and the central guard
        # refused in tool_call_hooks.__enter__ BEFORE the body ran. Roll the
        # claim back to pending (same token) so the operator can retry once the
        # precondition is met → 409. A POST-admission precondition failure does
        # NOT raise this (it's a real run failure; no rollback, so a retry can't
        # double-run side effects) (codex-5.5 R2 SEV-1).
        store.release_claim(run_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {run_id!r} is not ready to resume ({e}) — the suspension is "
                "still pending; retry once the precondition is met"
            ),
        )


@router.get("/suspended")
def list_suspended_skills(
    workspace_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """The "waiting on you" list — pending, non-expired suspensions (newest
    first). Optional ``workspace_type`` filter. Each item carries its prompt,
    options, expiry, and ``resume_url`` (never the internal state checkpoint)."""
    store = get_suspension_store()
    store.sweep_expired()
    items = store.list_pending(workspace_type=workspace_type, limit=limit)
    return {"count": len(items), "pending": [s.as_public_dict() for s in items]}
