"""Dismissals API (#62) — queryable index over reject + skip actions.

Two endpoints:

  * ``GET  /api/dismissals?since=&until=&action=&kind=&include_undone=``
      → list of dismissals rows, newest-first.
  * ``POST /api/dismissals/{id}/undo``
      → non-destructive: removes the sidecar so the proposal reappears in
      ``GET /api/proposals/pending``; for reject, also moves the file back
      from ``_processing/rejected/`` to its original location. Marks the
      row as undone so a second undo returns 409.

The sidecar files remain source-of-truth for skip-expiry semantics + the
reject audit trail. The SQLite table at ``routines/state/dismissals.db``
is the queryable index — together they make the Inbox lifecycle fully
auditable. See ``routines/dismissals/__init__.py`` for the design notes.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from routines.api.deps import RUNS_DIR, VAULT
from routines.dismissals import (
    Dismissal,
    DismissalAlreadyUndone,
    DismissalNotFound,
    get_dismissal,
    mark_undone,
    query_dismissals,
)
from routines.shared import audit

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Pydantic response models
# ────────────────────────────────────────────────────────────────────────────


DismissalAction = Literal["reject", "skip", "auto-expire", "revision-request"]


class DismissalRow(BaseModel):
    id: str
    proposal_id: str
    proposal_kind: str
    original_path: str
    current_path: str
    dismissed_at: str
    dismissed_by: str
    action: DismissalAction
    reason: Optional[str] = None
    reappears_at: Optional[str] = None
    undone_at: Optional[str] = None


class DismissalsListResponse(BaseModel):
    total: int
    dismissals: list[DismissalRow]


class UndoResponse(BaseModel):
    ok: bool = True
    restored_proposal_id: str
    restored_to: str


# ────────────────────────────────────────────────────────────────────────────
# GET /api/dismissals
# ────────────────────────────────────────────────────────────────────────────


def _to_row(d: Dismissal) -> DismissalRow:
    return DismissalRow(
        id=d.id,
        proposal_id=d.proposal_id,
        proposal_kind=d.proposal_kind,
        original_path=d.original_path,
        current_path=d.current_path,
        dismissed_at=d.dismissed_at.isoformat(timespec="seconds"),
        dismissed_by=d.dismissed_by,
        action=d.action,  # type: ignore[arg-type]
        reason=d.reason,
        reappears_at=(
            d.reappears_at.isoformat(timespec="seconds") if d.reappears_at else None
        ),
        undone_at=(
            d.undone_at.isoformat(timespec="seconds") if d.undone_at else None
        ),
    )


@router.get("/dismissals", response_model=DismissalsListResponse)
def list_dismissals(
    since: Optional[str] = Query(None, description="ISO datetime — lower bound (inclusive) on dismissed_at"),
    until: Optional[str] = Query(None, description="ISO datetime — upper bound (exclusive) on dismissed_at"),
    action: Optional[DismissalAction] = Query(None, description="Filter to a single action"),
    kind: Optional[str] = Query(None, description="Filter to a single proposal_kind"),
    include_undone: bool = Query(True, description="Include rows already undone"),
    limit: int = Query(500, ge=1, le=2000),
) -> DismissalsListResponse:
    """Walk the dismissals table with optional filters. Newest-first."""
    since_dt = _parse_iso_param(since, "since") if since else None
    until_dt = _parse_iso_param(until, "until") if until else None

    rows = query_dismissals(
        since=since_dt,
        until=until_dt,
        action=action,
        kind=kind,
        include_undone=include_undone,
        limit=limit,
    )
    return DismissalsListResponse(
        total=len(rows),
        dismissals=[_to_row(r) for r in rows],
    )


# ────────────────────────────────────────────────────────────────────────────
# POST /api/dismissals/{id}/undo
# ────────────────────────────────────────────────────────────────────────────


@router.post("/dismissals/{dismissal_id}/undo", response_model=UndoResponse)
def undo_dismissal(dismissal_id: str) -> UndoResponse:
    """Restore the proposal to ``GET /api/proposals/pending``.

    Behaviour by action:
      * ``skip``              → delete the ``.skip.json`` sidecar at
        ``original_path`` so the scanner no longer hides the proposal.
      * ``revision-request``  → delete the ``.revision.json`` sidecar at
        ``original_path`` (#58 — undo a kick-back to the source routine).
      * ``reject``            → move the file from ``_processing/rejected/``
        back to its original path + delete the ``.rejected.json`` sidecar.
      * ``auto-expire``       → 422 (system-initiated; nothing to undo).

    Errors:
      * 404 — dismissal id unknown
      * 409 — dismissal already undone (idempotency); or the proposal
              has been re-routed (file no longer where we expect it)
      * 422 — auto-expire action (system action; not user-undoable)
    """
    started = time.monotonic()
    run_id = audit.new_run_id()

    d = get_dismissal(dismissal_id)
    if d is None:
        raise HTTPException(404, f"dismissal {dismissal_id!r} not found")
    if d.undone_at is not None:
        raise HTTPException(
            409,
            f"dismissal {dismissal_id!r} already undone at "
            f"{d.undone_at.isoformat(timespec='seconds')}",
        )
    if d.action == "auto-expire":
        raise HTTPException(
            422,
            "auto-expire is a system-initiated dismissal and cannot be undone",
        )

    original_abs = VAULT / d.original_path
    current_abs = VAULT / d.current_path

    if d.action == "skip":
        # Sidecar lives next to the original file.
        sidecar = current_abs.with_name(current_abs.name + ".skip.json")
        if not current_abs.is_file():
            raise HTTPException(
                409,
                f"proposal file no longer at {d.current_path!r} — it may have "
                "been re-routed; cannot undo dismissal",
            )
        if sidecar.is_file():
            try:
                sidecar.unlink()
            except OSError as e:
                raise HTTPException(500, f"failed to remove skip sidecar: {e}") from e
        restored_to = d.original_path

    elif d.action == "revision-request":
        # Sidecar (`.revision.json`) lives next to the original file. Removing
        # it unblocks the proposal in the pending scanner. If the source
        # routine has already re-fired and replaced the file, the sidecar
        # is gone too — undo is then a no-op on the filesystem but still
        # marks the row as undone for audit clarity.
        sidecar = current_abs.with_name(current_abs.name + ".revision.json")
        if not current_abs.is_file():
            raise HTTPException(
                409,
                f"proposal file no longer at {d.current_path!r} — it may have "
                "been re-routed; cannot undo dismissal",
            )
        if sidecar.is_file():
            try:
                sidecar.unlink()
            except OSError as e:
                raise HTTPException(500, f"failed to remove revision sidecar: {e}") from e
        restored_to = d.original_path

    elif d.action == "reject":
        # File is currently in rejected dir; move it back to its original
        # location + remove the rejected sidecar.
        if not current_abs.is_file():
            raise HTTPException(
                409,
                f"rejected file no longer at {d.current_path!r}; cannot undo",
            )
        if original_abs.exists():
            raise HTTPException(
                409,
                f"original location {d.original_path!r} occupied; cannot undo "
                "without overwrite",
            )

        original_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(current_abs), str(original_abs))
        except OSError as e:
            raise HTTPException(500, f"failed to restore rejected file: {e}") from e

        # Sidecar lives in the rejected dir, next to the file that just moved.
        rejected_sidecar = current_abs.with_name(current_abs.name + ".rejected.json")
        if rejected_sidecar.is_file():
            try:
                rejected_sidecar.unlink()
            except OSError as e:
                log.warning(
                    "undo reject: failed to remove sidecar %s: %s",
                    rejected_sidecar, e,
                )
        restored_to = d.original_path

    else:
        raise HTTPException(422, f"unsupported dismissal action {d.action!r}")

    try:
        mark_undone(dismissal_id)
    except DismissalAlreadyUndone as e:
        # Race: someone else undid this between our get + mark.
        raise HTTPException(409, str(e)) from e
    except DismissalNotFound as e:
        raise HTTPException(404, str(e)) from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=d.proposal_id,
        action="undo",
        routine="dismissals.undo",
        run_id=run_id,
        status="ok",
        audit_dir=RUNS_DIR,
        inputs={
            "dismissal_id": dismissal_id,
            "action": d.action,
            "proposal_id": d.proposal_id,
            "proposal_kind": d.proposal_kind,
        },
        outputs={"restored_to": restored_to},
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return UndoResponse(
        ok=True,
        restored_proposal_id=d.proposal_id,
        restored_to=restored_to,
    )


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _parse_iso_param(value: str, field: str) -> datetime:
    """Parse an ISO datetime query param; 422 on garbage."""
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(422, f"invalid {field}={value!r}: {e}") from e


__all__ = ["router"]
