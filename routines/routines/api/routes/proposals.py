"""Proposals API — pending list + route / reject / skip actions.

The pending list (``GET /api/proposals/pending``) powers the dashboard's
TopHeader "REVIEW · N" chip and the Inbox tab. Three mutating actions land
proposals into final workspace destinations, reject them (with audit
trail preserved under ``VAULT/_processing/rejected/``), or skip them for
a deferral window (sidecar-driven; transparent reappearance).

Proposals are file-system artefacts. Identity is the 12-hex sha1 of the
vault-relative POSIX path — stable, deterministic, surfaces no PII. See
``OUTSTANDING.md`` ## CONTRACTS · inbox/proposals routing for the locked
shapes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import frontmatter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from routines.api.deps import RUNS_DIR, VAULT
from routines.api.middleware.session_lock import (
    SessionLockBusy,
    acquire_session_lock,
    release_session_lock,
)
from routines.dismissals import query_dismissals, record_dismissal
from routines.projects.issues import next_issue_id
from routines.shared import audit
from routines.shared.vault_writer import atomic_write

router = APIRouter()
log = logging.getLogger(__name__)


def _with_proposal_lock(fn):
    """F-31: serialise the route/reject/skip FS state machine per proposal.

    The lifecycle handlers are find-then-move sequences with no lock —
    concurrent POSTs for the same id race ``_find_proposal_by_id`` against
    ``shutil.move`` (double-append to the target note in the worst case, an
    OSError-mapped 4xx/5xx in the best). Reuses the F-13 coalescing lock
    under a namespaced ``proposal:<id>`` key (same registry as the sessions
    routes — keys are namespaced exactly so they can share it); contention
    maps to HTTP 409, the idempotent-retry contract the dashboard already
    speaks. Lock holder is a per-request nonce: every HTTP call is its own
    attempt, stale holders age out via the registry's stale_after."""
    @functools.wraps(fn)
    def wrapper(proposal_id: str, *args: Any, **kwargs: Any) -> Any:
        key = f"proposal:{proposal_id}"
        holder = audit.new_run_id()
        try:
            acquire_session_lock(key, holder)
        except SessionLockBusy as e:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"proposal {proposal_id!r} has an in-flight lifecycle "
                    f"action (started {e.acquired_age_sec:.1f}s ago) — retry "
                    "shortly"
                ),
            )
        try:
            return fn(proposal_id, *args, **kwargs)
        finally:
            release_session_lock(key, holder)
    return wrapper


# ────────────────────────────────────────────────────────────────────────────
# Scan config
# ────────────────────────────────────────────────────────────────────────────


# Directory -> proposal-kind label. The kind drives the chip's breakdown
# tooltip + the route action's destination logic.
#
# Frontmatter-based proposals (status: pending-review or legacy "pending"):
#     Routines/learning, Routines/memory-promotion, Routines/lessons-learned,
#     Routines/sector-extraction, Routines/sector-synthesis
#
# Flat-file proposals (any .md file in the dir is a proposal — no status
# field required because the file's *presence* IS the signal):
#     Inbox/hinotes-unrouted
_PROPOSAL_DIRS: dict[str, str] = {
    "Routines/learning": "learning",
    "Routines/memory-promotion": "memory-promotion",
    "Routines/lessons-learned": "lessons-learned",
    "Routines/sector-extraction": "sector-extraction",   # Plan v3 §6.9 Phase 3
    "Routines/sector-synthesis": "sector-synthesis",     # Plan v3 §6.9 Phase 4
    "Routines/system-insights": "system-insight",        # #73 — Dream Cycle Phase 5
    "Routines/deliverable-outcomes": "deliverable-outcome",  # #76 — skill conclusion capture
    "Routines/earnings-variance": "earnings-variance",   # #44 — earnings material-variance alert (own chip)
    "Routines/issue-candidates": "issue-candidate",      # #issues-register v1.5 — HiNotes → deal issues register
    "Inbox/hinotes-unrouted": "hinotes-unrouted",        # #8 — HiNotes routing
}

# Kinds for which presence-alone is the signal (no frontmatter status check).
_FLAT_KINDS = {"hinotes-unrouted"}

_PENDING_STATUSES = {"pending-review", "pending"}


# #58 — Two-tier classification. APPROVAL kinds write to the canonical
# vault layer (Companies/Sectors/People + decision register) and warrant
# audit-critical UI treatment; CONFIRMATION kinds are lightweight routing
# (where does this file live?). Unknown kinds default to confirmation —
# fail-safe to the lighter UI so a typo never silently promotes noise to
# the approval tier.
_APPROVAL_KINDS: frozenset[str] = frozenset({
    "learning",
    "memory-promotion",
    "lessons-learned",
    "sector-extraction",
    "sector-synthesis",
    "system-insight",  # #73 — Dream Cycle Phase 5 weekly self-reflection
    "deliverable-outcome",  # #76 — skill conclusion → semantic fact on a Company/Deal note
    "earnings-variance",  # #44 — earnings material-variance alert → fact on a Company note
    "issue-candidate",  # #issues-register v1.5 — appends an ISS section to the deal's issues register
})

_CONFIRMATION_KINDS: frozenset[str] = frozenset({
    "hinotes-unrouted",
    "email-unrouted",
})

ProposalTier = Literal["approval", "confirmation"]


def _tier_for(kind: str) -> ProposalTier:
    if kind in _APPROVAL_KINDS:
        return "approval"
    return "confirmation"


# ────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ────────────────────────────────────────────────────────────────────────────


WorkspaceType = Literal["project", "bd", "general"]


class PendingProposal(BaseModel):
    id: str             # 12-hex sha1 of vault-relative POSIX path
    kind: str           # see _PROPOSAL_DIRS
    tier: ProposalTier  # #58 — derived from kind; drives Inbox UI grouping
    path: str           # vault-relative POSIX
    title: str          # first H1, or filename stem if no H1
    date: str           # ISO from frontmatter, else from filename, else ""


class PendingProposalsResponse(BaseModel):
    total: int
    byKind: dict[str, int]
    items: list[PendingProposal]


class RouteRequest(BaseModel):
    workspace_type: WorkspaceType
    workspace_name: str = Field(..., min_length=1, max_length=128)


class RouteResponse(BaseModel):
    moved_to: str


# #58 — `reason` is REQUIRED on reject. Empty / whitespace-only → 422.
# The load-bearing audit-discipline change: operator cannot dismiss an
# audit-critical promotion without leaving a footprint.
class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="REQUIRED — audit trail enforced")

    @field_validator("reason")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("reason must be non-empty after stripping whitespace")
        return s


class RejectResponse(BaseModel):
    ok: bool = True


class SkipRequest(BaseModel):
    defer_days: int = Field(7, ge=1, le=365)


class SkipResponse(BaseModel):
    reappears_at: str


# #58 — revision-request: kick a proposal back to its source routine
# with operator feedback. Writes a `.revision.json` sidecar; the pending
# scanner hides the proposal until the source routine re-fires.
class RevisionRequest(BaseModel):
    feedback: str = Field(..., min_length=1, description="REQUIRED — what the operator wants reworked")

    @field_validator("feedback")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("feedback must be non-empty after stripping whitespace")
        return s


class RevisionResponse(BaseModel):
    ok: bool = True
    revision_sidecar_path: str


# #inbox-proposal-detail — read-only full write-up for the Inbox card.
class ProposalContentResponse(BaseModel):
    id: str
    kind: str
    path: str           # vault-relative POSIX
    title: str
    date: str
    body: str           # the markdown body (frontmatter stripped)


# #inbox-resolved-feed — one recently-resolved proposal for the Inbox rail.
ResolvedVerdict = Literal["routed", "rejected", "skipped", "revision"]


class ResolvedProposal(BaseModel):
    proposal_id: str
    kind: str
    verdict: ResolvedVerdict
    at: str             # ISO-8601 timestamp of the resolution
    title: str          # derived from the (now-moved) filename stem


class ResolvedProposalsResponse(BaseModel):
    total: int
    items: list[ResolvedProposal]   # newest-first, capped at `limit`


# ────────────────────────────────────────────────────────────────────────────
# Identity helpers
# ────────────────────────────────────────────────────────────────────────────


def _proposal_id(vault_relative_posix: str) -> str:
    """12-hex sha1 of the vault-relative POSIX path. Stable across processes
    so the dashboard can round-trip an id back through route/reject/skip."""
    return hashlib.sha1(vault_relative_posix.encode("utf-8")).hexdigest()[:12]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ────────────────────────────────────────────────────────────────────────────
# Sidecars (skip / reject)
# ────────────────────────────────────────────────────────────────────────────


def _skip_sidecar_for(path: Path) -> Path:
    """``proposal.md`` → ``proposal.md.skip.json`` (lives next to the file)."""
    return path.with_name(path.name + ".skip.json")


def _revision_sidecar_for(path: Path) -> Path:
    """``proposal.md`` → ``proposal.md.revision.json`` (lives next to the file).

    Presence alone hides the proposal from the pending scanner — the source
    routine is expected to re-fire and replace the file (which clears the
    sidecar too)."""
    return path.with_name(path.name + ".revision.json")


def _is_currently_skipped(path: Path) -> bool:
    """True when a future-dated `.skip.json` sidecar exists next to ``path``.

    Stale sidecars (``skipped_until`` already in the past) are ignored — the
    proposal reappears naturally without needing manual cleanup."""
    sidecar = _skip_sidecar_for(path)
    if not sidecar.is_file():
        return False
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(str(data.get("skipped_until")))
    except (ValueError, OSError, json.JSONDecodeError) as e:
        log.warning("skip sidecar unreadable at %s: %s — treating as not skipped", sidecar, e)
        return False
    # Compare as UTC-aware. If the parsed value is naive, treat as UTC.
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def _has_revision_pending(path: Path) -> bool:
    """True when a `.revision.json` sidecar exists next to ``path``.

    Unlike skip, revision-request has no expiry — the source routine is
    supposed to re-fire and replace the file (deleting the sidecar in the
    process). While the sidecar lives, the proposal stays hidden."""
    return _revision_sidecar_for(path).is_file()


# ────────────────────────────────────────────────────────────────────────────
# GET /api/proposals/pending — list
# ────────────────────────────────────────────────────────────────────────────


@router.get("/proposals/pending", response_model=PendingProposalsResponse)
def proposals_pending() -> PendingProposalsResponse:
    """Walk the proposal dirs and surface anything pending review.

    Filters out proposals whose ``.skip.json`` sidecar marks them as
    skipped past today — they reappear automatically when their
    deferral window expires."""
    try:
        items = list(_walk_pending(VAULT))
    except Exception as e:  # noqa: BLE001
        log.exception("proposals_pending: walk failed")
        raise HTTPException(status_code=500, detail=f"Walk failed: {e}") from e

    by_kind: dict[str, int] = {label: 0 for label in _PROPOSAL_DIRS.values()}
    for it in items:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1

    return PendingProposalsResponse(
        total=len(items),
        byKind=by_kind,
        items=items,
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /api/proposals/{id}/content — full write-up (#inbox-proposal-detail)
# ────────────────────────────────────────────────────────────────────────────


@router.get("/proposals/{proposal_id}/content", response_model=ProposalContentResponse)
def proposal_content(proposal_id: str) -> ProposalContentResponse:
    """Read-only: the full markdown body of a pending proposal, for the Inbox
    card's inline write-up.

    ``id`` resolves to a file via :func:`_find_proposal_by_id` (12-hex validated
    + walks only the known proposal dirs, so no path traversal). Title/date mirror
    the ``/pending`` list so the card stays consistent. No sensitivity gate: this
    is an operator-local read of the operator's own vault note — the same body the
    route/reject handlers already touch — and nothing leaves the machine."""
    found = _find_proposal_by_id(proposal_id)
    if found is None:
        raise HTTPException(404, f"proposal {proposal_id!r} not found")
    path, kind = found
    rel = path.relative_to(VAULT).as_posix()
    try:
        post = frontmatter.load(str(path))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"proposal unreadable: {e}") from e

    if kind in _FLAT_KINDS:
        title = _read_title(path)
        date = _date_from_filename(path) or ""
    else:
        title = _extract_title(post.content, fallback=path.stem)
        date = str(post.metadata.get("date") or _date_from_filename(path) or "")

    return ProposalContentResponse(
        id=proposal_id, kind=kind, path=rel, title=title, date=date, body=post.content,
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /api/proposals/resolved — recently-resolved feed (#inbox-resolved-feed)
# ────────────────────────────────────────────────────────────────────────────

# Terminal-resolution dismissals actions → the rail's verdict vocabulary. (No
# `auto-expire` handling — that action is never written by any code path; a
# lapsed skip is dropped below by its reappears_at instead.)
_RESOLVED_VERDICT: dict[str, str] = {
    "reject": "rejected",
    "skip": "skipped",
    "revision-request": "revision",
}
_TERMINAL_ACTIONS = ("reject", "skip", "revision-request")
_ROUTE_LOG_TAIL = 2000   # cap the route-log parse (bounded "recently resolved")


def _title_from_stem(path_str: str) -> str:
    """Humanise a proposal filename stem into a rail title. Best-effort — the
    real H1 lives inside the (now-moved) file, so we derive from the name:
    drop a leading ``YYYY-MM-DD`` prefix, swap separators for spaces, capitalise.
    Returns "" only for empty input (callers fall back to the kind)."""
    if not path_str:
        return ""
    name = path_str.replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.md$", "", name, flags=re.IGNORECASE)
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ ]+", "", stem)
    words = stem.replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else name


def _parse_at(s: Optional[str]) -> Optional[datetime]:
    """ISO-8601 → aware UTC datetime, or None. Accepts a trailing ``Z`` and
    treats naive values as UTC, so ordering/filtering are correct regardless of
    the writer's offset shape (not a fragile lexicographic string compare)."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.get("/proposals/resolved", response_model=ResolvedProposalsResponse)
def proposals_resolved(
    since: Optional[str] = None,
    limit: int = 30,
) -> ResolvedProposalsResponse:
    """Read-only feed of recently-RESOLVED proposals for the Inbox "Recently
    resolved" rail. Unions two EXISTING persistent surfaces — no new writes:

      * the dismissals index (reject / skip / revision-request; undone rows and
        already-reappeared skips excluded), and
      * routed proposals from the ``proposals.route`` audit log.

    Newest-first by real instant, capped at ``limit`` (1..200). Best-effort per
    source: a failure in one source is logged and skipped, never 500s the rail."""
    limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    # (parsed instant, item) pairs so sort + `since` use real datetimes.
    rows: list[tuple[datetime, ResolvedProposal]] = []

    # 1) dismissals — query each terminal action SEPARATELY so a burst of one
    #    action can't starve the others out of the limit window.
    for action in _TERMINAL_ACTIONS:
        try:
            dismissed = query_dismissals(
                since=None, until=None, action=action, kind=None,
                include_undone=False, limit=limit,
            )
        except Exception:  # noqa: BLE001
            log.warning("proposals/resolved: dismissals query (%s) failed", action, exc_info=True)
            continue
        for d in dismissed:
            verdict = _RESOLVED_VERDICT.get(d.action)
            if verdict is None:
                continue
            # A skip whose window has LAPSED has reappeared in /pending — it is no
            # longer "resolved", so drop it from the feed.
            if d.action == "skip" and d.reappears_at is not None and d.reappears_at <= now:
                continue
            at = d.dismissed_at.isoformat(timespec="seconds")
            rows.append((_parse_at(at) or now, ResolvedProposal(
                proposal_id=d.proposal_id,
                kind=d.proposal_kind,
                verdict=verdict,  # type: ignore[arg-type]
                at=at,
                title=_title_from_stem(d.original_path) or d.proposal_kind,
            )))

    # 2) routed — the per-routine audit jsonl (best-effort; skip bad lines).
    route_log = RUNS_DIR / "proposals.route.jsonl"
    if route_log.is_file():
        try:
            lines = route_log.read_text(encoding="utf-8").splitlines()[-_ROUTE_LOG_TAIL:]
        except OSError:
            lines = []
            log.warning("proposals/resolved: route log read failed", exc_info=True)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict) or rec.get("status") != "ok":
                continue
            inp = rec.get("inputs")
            inp = inp if isinstance(inp, dict) else {}
            out = rec.get("outputs")
            out = out if isinstance(out, dict) else {}
            pid = inp.get("id")
            ts = rec.get("ts")
            if not isinstance(pid, str):
                continue
            at_dt = _parse_at(ts if isinstance(ts, str) else None)
            if at_dt is None:
                continue
            # `from` is written only by the generic move handler; the special-case
            # handlers (deliverable-outcome / issue-candidate) carry target/project/
            # moved_to instead — fall back so the title is never blank; kind is the
            # last resort (always present).
            src = (inp.get("from") or inp.get("target") or inp.get("project")
                   or out.get("moved_to") or out.get("appended_to") or "")
            kind = str(inp.get("kind") or "routed")
            rows.append((at_dt, ResolvedProposal(
                proposal_id=pid,
                kind=kind,
                verdict="routed",
                at=str(ts),
                title=_title_from_stem(str(src)) or kind,
            )))

    # newest-first by real instant; `since` lower bound; cap.
    rows.sort(key=lambda t: t[0], reverse=True)
    since_dt = _parse_at(since)
    if since_dt is not None:
        rows = [t for t in rows if t[0] >= since_dt]
    items = [rp for _, rp in rows[:limit]]
    return ResolvedProposalsResponse(total=len(items), items=items)


# ────────────────────────────────────────────────────────────────────────────
# POST /api/proposals/{id}/route
# ────────────────────────────────────────────────────────────────────────────


@router.post("/proposals/{proposal_id}/route", response_model=RouteResponse)
@_with_proposal_lock
def route_proposal(proposal_id: str, req: RouteRequest) -> RouteResponse:
    """Move a proposal to its workspace destination per the kind+workspace
    matrix in OUTSTANDING ## CONTRACTS · inbox/proposals routing."""
    started = time.monotonic()
    run_id = audit.new_run_id()

    found = _find_proposal_by_id(proposal_id)
    if found is None:
        raise HTTPException(404, f"proposal {proposal_id!r} not found")
    src_path, kind = found

    # #76 — deliverable-outcome routes differently from the move-to-workspace
    # path below: it APPENDS the captured conclusion as a dated, sourced fact to
    # the target note named in the proposal's OWN frontmatter (never overwrites
    # — §3 rule 9), then retires the proposal file. The request's workspace_*
    # fields are not consulted for this kind.
    #
    # #44 — earnings-variance shares the deliverable-outcome FRONTMATTER shape
    # (type/target/headline/section) and only differs by its own Inbox chip
    # (own directory → own kind). It appends the variance headline to the
    # Company note via the same handler — no separate routing logic needed.
    if kind in ("deliverable-outcome", "earnings-variance"):
        return _route_deliverable_outcome(src_path, proposal_id, run_id, started)

    # #issues-register v1.5 — issue-candidate also routes from its OWN
    # frontmatter (project/title/gating), appending a `## ISS-NN` section to
    # `Projects/<deal>/14 Issues & Outstanding.md` — append-only (§3 rule 9).
    if kind == "issue-candidate":
        return _route_issue_candidate(src_path, proposal_id, run_id, started)

    # Reject path-injection in workspace_name belt-and-braces. The destination
    # builder concatenates it raw, so we sanitise here.
    name = req.workspace_name.strip()
    if not name or any(bad in name for bad in ("..", "/", "\\", ":", "\x00")):
        raise HTTPException(422, f"invalid workspace_name {req.workspace_name!r}")

    dest_dir = _destination_for(req.workspace_type, name, kind)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / src_path.name

    if dest_path.exists():
        raise HTTPException(409, f"destination already exists: {dest_path}")

    tier = _tier_for(kind)
    try:
        shutil.move(str(src_path), str(dest_path))
        # If a skip or revision sidecar travelled with the proposal, move
        # them too so the final destination doesn't carry stale state (and
        # so the audit trail is preserved alongside the file).
        for src_side, dst_builder in (
            (_skip_sidecar_for(src_path), _skip_sidecar_for),
            (_revision_sidecar_for(src_path), _revision_sidecar_for),
        ):
            if src_side.is_file():
                shutil.move(str(src_side), str(dst_builder(dest_path)))
    except OSError as e:
        audit.write_structured_safe(
            actor={"type": "user", "id": "operator"},
            entity_type="proposal",
            entity_id=proposal_id,
            action="route",
            routine="proposals.route",
            audit_dir=RUNS_DIR,
            run_id=run_id,
            status="error",
            inputs={
                "id": proposal_id, "from": str(src_path), "to": str(dest_path),
                "workspace_type": req.workspace_type, "workspace_name": name,
                "kind": kind, "tier": tier,
            },
            error=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
            details={
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "from": str(src_path), "to": str(dest_path),
                "workspace_type": req.workspace_type, "workspace_name": name,
                "kind": kind, "tier": tier,
            },
        )
        raise HTTPException(500, f"move failed: {e}") from e

    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="route",
        routine="proposals.route",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "from": str(src_path),
            "workspace_type": req.workspace_type, "workspace_name": name,
            "kind": kind, "tier": tier,
        },
        outputs={"moved_to": str(dest_path)},
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "from": str(src_path), "to": str(dest_path),
            "workspace_type": req.workspace_type, "workspace_name": name,
            "kind": kind, "tier": tier,
        },
    )

    return RouteResponse(moved_to=str(dest_path))


# ────────────────────────────────────────────────────────────────────────────
# POST /api/proposals/{id}/reject
# ────────────────────────────────────────────────────────────────────────────


@router.post("/proposals/{proposal_id}/reject", response_model=RejectResponse)
@_with_proposal_lock
def reject_proposal(
    proposal_id: str,
    req: RejectRequest,
) -> RejectResponse:
    """Move the proposal file to ``VAULT/_processing/rejected/`` and write
    a sidecar capturing the rejection reason. Never deletes.

    #58 — ``reason`` is REQUIRED (422 if absent / empty / whitespace-only).
    Audit-discipline change: operator cannot dismiss without leaving a
    footprint."""
    started = time.monotonic()
    run_id = audit.new_run_id()

    found = _find_proposal_by_id(proposal_id)
    if found is None:
        raise HTTPException(404, f"proposal {proposal_id!r} not found")
    src_path, kind = found
    tier = _tier_for(kind)

    rejected_dir = VAULT / "_processing" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_in(rejected_dir, src_path.name)

    try:
        shutil.move(str(src_path), str(dest))
        # Carry the skip + revision sidecars over if present (audit trail).
        for src_side, dst_builder in (
            (_skip_sidecar_for(src_path), _skip_sidecar_for),
            (_revision_sidecar_for(src_path), _revision_sidecar_for),
        ):
            if src_side.is_file():
                shutil.move(str(src_side), str(dst_builder(dest)))
    except OSError as e:
        audit.write_structured_safe(
            actor={"type": "user", "id": "operator"},
            entity_type="proposal",
            entity_id=proposal_id,
            action="reject",
            routine="proposals.reject",
            audit_dir=RUNS_DIR,
            run_id=run_id,
            status="error",
            inputs={"id": proposal_id, "path": str(src_path), "kind": kind, "tier": tier},
            error=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
            details={
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "path": str(src_path), "kind": kind, "tier": tier,
            },
        )
        raise HTTPException(500, f"move-to-rejected failed: {e}") from e

    sidecar = dest.with_name(dest.name + ".rejected.json")
    sidecar.write_text(
        json.dumps(
            {
                "rejected_at": _now_utc_iso(),
                "reason": req.reason,
                "original_kind": kind,
                "tier": tier,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # #62 — dual-write to the dismissals SQLite index. Sidecar above
    # remains source-of-truth for the reject audit-trail file; the row
    # below powers GET /api/dismissals + POST /api/dismissals/{id}/undo.
    original_rel = src_path.relative_to(VAULT).as_posix()
    current_rel = dest.relative_to(VAULT).as_posix()
    dismissal = record_dismissal(
        proposal_id=proposal_id,
        proposal_kind=kind,
        original_path=original_rel,
        current_path=current_rel,
        action="reject",
        reason=req.reason,
    )

    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="reject",
        routine="proposals.reject",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "path": str(src_path),
            "kind": kind, "tier": tier, "reason": req.reason,
        },
        outputs={"moved_to": str(dest), "dismissal_id": dismissal.id},
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "reason": req.reason,
            "kind": kind, "tier": tier,
            "moved_to": str(dest),
        },
    )

    return RejectResponse(ok=True)


# ────────────────────────────────────────────────────────────────────────────
# POST /api/proposals/{id}/skip
# ────────────────────────────────────────────────────────────────────────────


@router.post("/proposals/{proposal_id}/skip", response_model=SkipResponse)
@_with_proposal_lock
def skip_proposal(
    proposal_id: str,
    req: Optional[SkipRequest] = None,
) -> SkipResponse:
    """Write a ``.skip.json`` sidecar deferring the proposal for ``defer_days``
    (default 7). The pending scanner filters it out until the date passes."""
    started = time.monotonic()
    run_id = audit.new_run_id()
    if req is None:
        req = SkipRequest()

    found = _find_proposal_by_id(proposal_id)
    if found is None:
        raise HTTPException(404, f"proposal {proposal_id!r} not found")
    src_path, kind = found
    tier = _tier_for(kind)

    reappears_at = datetime.now(timezone.utc) + timedelta(days=req.defer_days)
    reappears_iso = reappears_at.isoformat(timespec="seconds")
    sidecar = _skip_sidecar_for(src_path)
    sidecar.write_text(
        json.dumps(
            {
                "skipped_until": reappears_iso,
                "original_kind": kind,
                "tier": tier,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # #62 — dual-write to the dismissals SQLite index. Sidecar above
    # remains source-of-truth for skip-expiry semantics (the pending
    # scanner only consults sidecars); the row below powers
    # GET /api/dismissals + POST /api/dismissals/{id}/undo.
    rel_path = src_path.relative_to(VAULT).as_posix()
    dismissal = record_dismissal(
        proposal_id=proposal_id,
        proposal_kind=kind,
        original_path=rel_path,
        current_path=rel_path,
        action="skip",
        reappears_at=reappears_at,
    )

    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="skip",
        routine="proposals.skip",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "path": str(src_path),
            "kind": kind, "tier": tier, "defer_days": req.defer_days,
        },
        outputs={
            "reappears_at": reappears_iso,
            "sidecar": str(sidecar),
            "dismissal_id": dismissal.id,
        },
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "defer_days": req.defer_days,
            "reappears_at": reappears_iso,
            "kind": kind, "tier": tier,
        },
    )

    return SkipResponse(reappears_at=reappears_iso)


# ────────────────────────────────────────────────────────────────────────────
# POST /api/proposals/{id}/request-revision   — #58
# ────────────────────────────────────────────────────────────────────────────


@router.post(
    "/proposals/{proposal_id}/request-revision",
    response_model=RevisionResponse,
)
def request_proposal_revision(
    proposal_id: str,
    req: RevisionRequest,
) -> RevisionResponse:
    """Kick a pending proposal back to its source routine with feedback.

    Writes a ``<file>.revision.json`` sidecar adjacent to the proposal.
    The pending scanner treats the sidecar as a hide signal (no expiry —
    the source routine is supposed to re-fire, replace the file, and
    delete the sidecar in the process).

    Errors:
      * 404 — proposal id doesn't resolve to any active file (already
        routed / rejected, or never existed)
      * 409 — proposal already has a revision pending (avoid stomping
        the prior feedback)
    """
    started = time.monotonic()
    run_id = audit.new_run_id()

    found = _find_proposal_by_id(proposal_id)
    if found is None:
        raise HTTPException(404, f"proposal {proposal_id!r} not found")
    src_path, kind = found
    tier = _tier_for(kind)

    sidecar = _revision_sidecar_for(src_path)
    if sidecar.is_file():
        raise HTTPException(
            409,
            f"proposal {proposal_id!r} already has a revision pending — "
            "wait for the source routine to re-fire, or delete the existing "
            f".revision.json sidecar to overwrite",
        )

    requested_at = _now_utc_iso()
    sidecar.write_text(
        json.dumps(
            {
                "requested_at": requested_at,
                "feedback": req.feedback,
                "requested_by": "operator",
                "source_routine_kind": kind,
                "tier": tier,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # #58 — dual-write to dismissals so "show me everything I bumped back
    # this week" works the same way as rejects + skips. Source-of-truth
    # remains the sidecar; the row is the queryable index.
    rel_path = src_path.relative_to(VAULT).as_posix()
    dismissal = record_dismissal(
        proposal_id=proposal_id,
        proposal_kind=kind,
        original_path=rel_path,
        current_path=rel_path,
        action="revision-request",
        reason=req.feedback,
    )

    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="request-revision",
        routine="proposals.request-revision",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "path": str(src_path),
            "kind": kind, "tier": tier, "feedback": req.feedback,
        },
        outputs={
            "revision_sidecar_path": str(sidecar),
            "requested_at": requested_at,
            "dismissal_id": dismissal.id,
        },
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "feedback": req.feedback,
            "kind": kind, "tier": tier,
        },
    )

    return RevisionResponse(
        ok=True,
        revision_sidecar_path=str(sidecar),
    )


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _walk_pending(vault_root: Path):
    """Yield ``PendingProposal`` for every file the scanner considers active.

    Hides proposals with a future-dated ``.skip.json`` sidecar OR any
    ``.revision.json`` sidecar (#58 — the source routine is expected to
    re-fire and replace the file; sidecar is the kicker)."""
    for rel_dir, kind in _PROPOSAL_DIRS.items():
        full = vault_root / rel_dir
        if not full.is_dir():
            continue
        for path in sorted(full.glob("*.md"), reverse=True):
            # Skip the sidecars themselves and any non-files.
            if not path.is_file():
                continue
            if _is_currently_skipped(path):
                continue
            if _has_revision_pending(path):
                continue

            if kind in _FLAT_KINDS:
                # Presence is the signal — no frontmatter status required.
                title = _read_title(path)
                date = _date_from_filename(path) or ""
            else:
                try:
                    post = frontmatter.load(path)
                except Exception as e:  # noqa: BLE001
                    log.warning("proposals: parse %s failed: %s", path, e)
                    continue
                status = str(post.metadata.get("status") or "").lower().strip()
                if status not in _PENDING_STATUSES:
                    continue
                title = _extract_title(post.content, fallback=path.stem)
                date = str(post.metadata.get("date") or _date_from_filename(path) or "")

            rel = path.relative_to(vault_root).as_posix()
            yield PendingProposal(
                id=_proposal_id(rel),
                kind=kind,
                tier=_tier_for(kind),
                path=rel,
                title=title,
                date=date,
            )


def _find_proposal_by_id(proposal_id: str) -> Optional[tuple[Path, str]]:
    """Reverse-lookup an id → (absolute path, kind).

    Walks the same scan space as ``_walk_pending`` (including skipped ones,
    so skip-then-route works) and matches on the same sha1[:12] derivation.
    Returns None when the id doesn't resolve to any file we can see today."""
    # Validate shape so we don't iterate the whole vault for garbage.
    if not isinstance(proposal_id, str) or len(proposal_id) != 12:
        return None
    if any(c not in "0123456789abcdef" for c in proposal_id.lower()):
        return None
    proposal_id = proposal_id.lower()

    for rel_dir, kind in _PROPOSAL_DIRS.items():
        full = VAULT / rel_dir
        if not full.is_dir():
            continue
        for path in full.glob("*.md"):
            if not path.is_file():
                continue
            rel = path.relative_to(VAULT).as_posix()
            if _proposal_id(rel) == proposal_id:
                return (path, kind)
    return None


# ────────────────────────────────────────────────────────────────────────────
# #76 — deliverable-outcome routing (append conclusion to target note)
# ────────────────────────────────────────────────────────────────────────────

_DEFAULT_CAPTURE_SECTION = "Valuation history"


def _safe_vault_target(target_rel: str) -> Path:
    """Resolve a proposal's ``target:`` frontmatter to an absolute vault path,
    rejecting anything that escapes the vault (path injection). Vault-relative,
    forward-slash, no ``..`` segments, no drive separators."""
    norm = str(target_rel or "").replace("\\", "/").strip().lstrip("/")
    if not norm or ":" in norm or any(seg in ("..", "") for seg in norm.split("/")):
        raise HTTPException(422, f"invalid deliverable-outcome target {target_rel!r}")
    if not norm.lower().endswith(".md"):
        raise HTTPException(422, f"deliverable-outcome target must be a .md note, got {target_rel!r}")
    candidate = (VAULT / norm).resolve()
    vault_root = VAULT.resolve()
    if vault_root != candidate and vault_root not in candidate.parents:
        raise HTTPException(422, f"deliverable-outcome target escapes the vault: {target_rel!r}")
    return VAULT / norm


def _is_sector_comps_target(target_path: Path) -> bool:
    """True when the target is a ``Sectors/<sector>/**/Comps.md`` note — a
    ``sector-claim`` comps note (observed trading-multiple snapshots), which
    must be seeded from the sector-comps template rather than the Company
    schema (#43-sector-template-align §4.3).

    Requires a sector segment BETWEEN ``Sectors/`` and ``Comps.md`` (``len >=
    3``): a bare ``Sectors/Comps.md`` has no sector to name and must NOT match
    (it would otherwise seed ``sector: Sectors``) — it falls through to the
    company default instead. Match is case-exact, mirroring the canonical vault
    layout and the producer's emitted ``Sectors/<slug>/Comps.md`` target."""
    try:
        parts = target_path.relative_to(VAULT).parts
    except ValueError:
        parts = target_path.parts  # best-effort if somehow outside the vault
    return len(parts) >= 3 and parts[0] == "Sectors" and parts[-1] == "Comps.md"


def _seed_sector_comps(target_path: Path) -> str:
    """Seed a new ``Sectors/<sector>/Comps.md`` from ``Templates/sector-comps.md``
    (the canonical ``sector-claim`` shape). The sector slug is the parent
    directory name; it fills the template's blank ``sector:`` field + the
    ``{Sector}`` title placeholder. Falls back to a minimal sector-claim note
    when the template file is absent (e.g. in tests) so routing never fails."""
    sector = target_path.parent.name
    title = sector.replace("-", " ").strip().title() or sector
    tmpl = VAULT / "Templates" / "sector-comps.md"
    if tmpl.is_file():
        try:
            raw = tmpl.read_text(encoding="utf-8")
            raw = re.sub(r"(?m)^sector:\s*$", f"sector: {sector}", raw, count=1)
            return raw.replace("{Sector}", title)
        except OSError:
            pass
    return (
        "---\n"
        "type: sector-claim\n"
        f"sector: {sector}\n"
        "claim_type: comps\n"
        "memory_kind: semantic\n"
        "sensitivity: internal\n"
        "tags: [sector-claim, comps, semantic-memory]\n"
        "---\n\n"
        f"# {title} — comps\n\n"
        "## Precedent transactions\n\n"
        "## Comps runs\n\n"
        "## Multiples range summary\n\n"
    )


def _new_note_from_template(target_path: Path) -> str:
    """Seed text for a target note that doesn't exist yet.

    The template is chosen by the target's vault-relative PATH — a *path-prefix
    map*, the simpler of the two options floated in #43-sector-template-align §4
    (the other was a proposal-declared ``template:`` field). Path-prefix keeps
    the create-from-template policy in ONE place (this route, which governs the
    destructive create), needs no producer-side change, and can't be steered by
    a malformed proposal:

      * ``Sectors/**/Comps.md``  → ``Templates/sector-comps.md`` (a
        ``sector-claim`` comps note — observed trading multiples, NOT a company
        profile).
      * everything else          → ``Templates/company.md`` (the canonical
        Company schema — unchanged).

    Each branch falls back to a minimal note of the correct ``type:`` when its
    template file is absent (e.g. in tests), so routing never fails just because
    a template is missing."""
    if _is_sector_comps_target(target_path):
        return _seed_sector_comps(target_path)
    name = target_path.stem
    tmpl = VAULT / "Templates" / "company.md"
    if tmpl.is_file():
        try:
            raw = tmpl.read_text(encoding="utf-8")
            raw = re.sub(r"(?m)^name:\s*$", f"name: {name}", raw, count=1)
            return raw.replace("{Company Name}", name)
        except OSError:
            pass
    return (
        "---\n"
        "type: company\n"
        f"name: {name}\n"
        "sensitivity: internal\n"
        "tags: [company]\n"
        "---\n\n"
        f"# {name}\n\n"
    )


def _format_fact_bullet(*, headline: str, date: str, provenance: str, artefact: str) -> str:
    """Render the dated, sourced semantic-fact bullet (#54a-style inline
    provenance + a §3 rule 7 certainty marker)."""
    lead = f"- **{date}** — {headline}" if date else f"- {headline}"
    marker = f"`(confirmed, {date})`" if date else "`(confirmed)`"
    extras: list[str] = []
    if provenance:
        extras.append(f"provenance: `{provenance}`")
    if artefact:
        extras.append(f"artefact: `{artefact}`")
    tail = (" — " + " · ".join(extras)) if extras else ""
    return f"{lead} {marker}{tail}"


def _append_under_section(text: str, section: str, bullet: str) -> str:
    """Append ``bullet`` under the ``## <section>`` heading (creating the
    section at end-of-file if absent). APPEND-ONLY: existing content is never
    rewritten, only added to (§3 rule 9)."""
    heading = f"## {section}"
    lines = text.splitlines()

    head_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            head_idx = i
            break

    if head_idx is None:
        # No such section yet — append a fresh one at the end of the note.
        body = text.rstrip("\n")
        return f"{body}\n\n{heading}\n\n{bullet}\n"

    # Find the section's end (next ## heading, else EOF), then back up over any
    # trailing blank lines so the bullet sits flush with existing entries.
    end = len(lines)
    for j in range(head_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    insert_at = end
    while insert_at - 1 > head_idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    new_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
    return "\n".join(new_lines) + "\n"


_YEAR_HEADING_RE = re.compile(r"^##\s+(\d{4})\s*$")


def _year_insert_index(lines: list[str], year: int) -> int:
    """Where to insert a brand-new ``## <year>`` block to keep year headings in
    DESCENDING order (matching the canonical ``Sectors/<x>/Comps.md`` shape).

    Before the first existing year heading smaller than ``year``; failing that,
    after the last (larger) year block but before its trailing non-year section
    (e.g. ``## Multiples range summary``); failing that, before the first
    section heading; else end-of-file."""
    year_headings = [
        (i, int(m.group(1)))
        for i, ln in enumerate(lines)
        if (m := _YEAR_HEADING_RE.match(ln.strip()))
    ]
    for i, yy in year_headings:
        if yy < year:
            return i
    if year_headings:
        last_year_idx = year_headings[-1][0]
        for j in range(last_year_idx + 1, len(lines)):
            if lines[j].startswith("## "):
                return j
        return len(lines)
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            return i
    return len(lines)


def _append_block_under_year(text: str, year: str, block: str) -> str:
    """Append a pre-rendered ``### comp-<id>`` block under the ``## <year>``
    heading (creating the heading in descending position if absent).

    APPEND-ONLY and idempotent: if a ``### <anchor>`` heading identical to the
    block's first ``### `` line already exists anywhere in the note, the text is
    returned unchanged (re-routing the same deal is a no-op — §3 rule 9)."""
    block = block.strip("\n")
    anchor = next((ln.strip() for ln in block.splitlines() if ln.startswith("### ")), "")
    lines = text.splitlines()

    if anchor and any(ln.strip() == anchor for ln in lines):
        return text  # comp-id already present — idempotent

    heading = f"## {year}"
    head_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if ln.strip() == heading:
            head_idx = i
            break

    if head_idx is not None:
        # Append to the end of the existing year section (before the next ## ),
        # trimming trailing blanks so the block sits flush under prior comps.
        end = len(lines)
        for j in range(head_idx + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        insert_at = end
        while insert_at - 1 > head_idx and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + ["", *block.splitlines()] + lines[insert_at:]
        return "\n".join(new_lines) + "\n"

    # Year heading absent — create it in descending position.
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        idx = len(lines)
    else:
        idx = _year_insert_index(lines, year_int)
    new_section = [heading, "", *block.splitlines(), ""]
    new_lines = lines[:idx] + new_section + lines[idx:]
    return "\n".join(new_lines).rstrip("\n") + "\n"


def _route_deliverable_outcome(
    src_path: Path, proposal_id: str, run_id: str, started: float,
) -> RouteResponse:
    """Route a ``deliverable-outcome`` proposal: append its captured conclusion
    to the target note, then retire the proposal file to ``_processing/applied/``.

    The target note + headline + provenance all come from the proposal's own
    frontmatter (written by the capture loop, #76). Never overwrites the target
    — appends a dated bullet under the declared section, creating the note from
    template if it doesn't exist yet."""
    try:
        post = frontmatter.load(src_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"deliverable-outcome proposal unreadable: {e}") from e

    meta = post.metadata
    target_rel = str(meta.get("target") or "").strip()
    headline = str(meta.get("headline") or "").strip()
    if not target_rel or not headline:
        raise HTTPException(
            422,
            "deliverable-outcome proposal missing required 'target'/'headline' frontmatter",
        )
    section = str(meta.get("section") or _DEFAULT_CAPTURE_SECTION).strip() or _DEFAULT_CAPTURE_SECTION
    provenance = str(meta.get("provenance") or "").strip()
    artefact = str(meta.get("workspace_artefact") or "").strip()
    date = str(meta.get("date") or "").strip()
    # #43 (Option B): an optional pre-rendered structured block. When present,
    # it is appended heading-aware under the ``## <section>`` (a year) and is
    # idempotent on its own ``### <comp-id>`` anchor. When absent — every
    # existing caller (LBO, comps, the deal-tracker company bullets) — the
    # flat-bullet path below runs exactly as before (back-compat).
    body_md = str(meta.get("body_md") or "").strip()

    target_path = _safe_vault_target(target_rel)

    # Append the fact (create from template if the note is new). Append-only.
    base = target_path.read_text(encoding="utf-8") if target_path.is_file() else _new_note_from_template(target_path)
    created = not target_path.is_file()
    if body_md:
        new_text = _append_block_under_year(base, section, body_md)
    else:
        bullet = _format_fact_bullet(headline=headline, date=date, provenance=provenance, artefact=artefact)
        new_text = _append_under_section(base, section, bullet)
    try:
        atomic_write(target_path, new_text, vault_root=VAULT)
    except OSError as e:
        audit.write_structured_safe(
            actor={"type": "user", "id": "operator"},
            entity_type="proposal",
            entity_id=proposal_id,
            action="route",
            routine="proposals.route",
            audit_dir=RUNS_DIR,
            run_id=run_id,
            status="error",
            inputs={"id": proposal_id, "kind": "deliverable-outcome", "target": target_rel},
            error=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise HTTPException(500, f"append-to-note failed: {e}") from e

    # Retire the proposal file so it leaves the pending queue (audit trail
    # preserved under _processing/applied/, never deleted). Carry sidecars.
    applied_dir = VAULT / "_processing" / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_in(applied_dir, src_path.name)
    shutil.move(str(src_path), str(dest))
    for src_side, dst_builder in (
        (_skip_sidecar_for(src_path), _skip_sidecar_for),
        (_revision_sidecar_for(src_path), _revision_sidecar_for),
    ):
        if src_side.is_file():
            shutil.move(str(src_side), str(dst_builder(dest)))

    target_str = str(target_path)
    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="route",
        routine="proposals.route",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "kind": "deliverable-outcome", "tier": "approval",
            "target": target_rel, "provenance": provenance,
        },
        outputs={"appended_to": target_str, "note_created": created, "retired_to": str(dest)},
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "kind": "deliverable-outcome", "tier": "approval",
            "appended_to": target_str, "note_created": created,
            "provenance": provenance,
        },
    )
    return RouteResponse(moved_to=target_str)


_ISSUES_REGISTER_FILENAME = "14 Issues & Outstanding.md"
_ISSUES_PLACEHOLDER = "*(none yet)*"

# Serialises the read-allocate-append-write cycle on issue registers so two
# concurrent Route clicks can't allocate the same ISS-NN or lose an append
# (codex finding 2). One process-wide lock — register routing is operator-
# initiated and rare; per-path granularity isn't worth the bookkeeping.
_ISSUE_ROUTE_LOCK = threading.Lock()


def _one_line(value: Any) -> str:
    """Collapse a frontmatter value to single-line text before it is
    interpolated into register Markdown — a newline in a title / gating item
    could otherwise inject a fake heading, metadata bullet, or checkbox
    (codex finding 4)."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _seed_issues_register(project: str) -> str:
    """Seed text for a project whose register predates the v1 template — copy
    ``Projects/_template/14 Issues & Outstanding.md`` with the project name
    filled in, falling back to a minimal correct-`type:` note (mirrors
    ``_new_note_from_template``)."""
    tmpl = VAULT / "Projects" / "_template" / _ISSUES_REGISTER_FILENAME
    if tmpl.is_file():
        try:
            raw = tmpl.read_text(encoding="utf-8")
            raw = re.sub(r'(?m)^project:\s*"\[\[\]\]"\s*$', f"project: {project}", raw, count=1)
            return raw.replace("{Project Name}", project)
        except OSError:
            pass
    return (
        "---\n"
        "type: issues-register\n"
        "memory_kind: episodic\n"
        f"project: {project}\n"
        "sensitivity: confidential\n"
        "tags: [register, issues]\n"
        "---\n\n"
        f"# Issues & Outstanding — {project}\n\n"
        "## Issues\n"
    )


def _next_issue_id(text: str) -> str:
    nums = [int(n) for n in _ISSUE_ID_RE.findall(text)]
    return f"ISS-{(max(nums) + 1 if nums else 1):02d}"


def _append_issue_block(text: str, block: str) -> str:
    """Append a ``## ISS-NN`` block at the end of the ``## Issues`` section
    (end-of-file in the canonical register shape), dropping the lone
    ``*(none yet)*`` placeholder on first append. APPEND-ONLY — no existing
    issue section is ever rewritten (§3 rule 9)."""
    block = block.strip("\n")
    lines = text.splitlines()

    head_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if ln.strip() == "## Issues":
            head_idx = i
            break

    if head_idx is None:
        body = text.rstrip("\n")
        return f"{body}\n\n## Issues\n\n{block}\n"

    # The placeholder only counts when it's still the section's sole content.
    section_tail = [ln for ln in lines[head_idx + 1:] if ln.strip()]
    if section_tail and all(ln.strip() == _ISSUES_PLACEHOLDER for ln in section_tail):
        lines = [
            ln for i, ln in enumerate(lines)
            if not (i > head_idx and ln.strip() == _ISSUES_PLACEHOLDER)
        ]

    return "\n".join(lines).rstrip("\n") + f"\n\n{block}\n"


def _route_issue_candidate(
    src_path: Path, proposal_id: str, run_id: str, started: float,
) -> RouteResponse:
    """Route an ``issue-candidate`` proposal (#issues-register v1.5): append a
    ``## ISS-NN`` section to the named project's issues register, then retire
    the proposal to ``_processing/applied/``.

    Everything comes from the proposal's OWN frontmatter (written by the
    HiNotes emitter — ``routines.hinotes.issue_candidates``): ``project``,
    ``title``, optional ``suggested_priority`` / ``affects`` / ``owner`` /
    ``gating`` / ``provenance`` / ``date``. The register is created from
    ``Projects/_template`` when an older deal predates the v1 template. The
    next free ISS-NN is computed at route time (not emit time) so concurrent
    candidates never collide on an id."""
    try:
        post = frontmatter.load(src_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"issue-candidate proposal unreadable: {e}") from e

    meta = post.metadata
    project = str(meta.get("project") or "").strip()
    title = str(meta.get("title") or "").strip()
    if not project or not title:
        raise HTTPException(
            422, "issue-candidate proposal missing required 'project'/'title' frontmatter",
        )
    if any(bad in project for bad in ("..", "/", "\\", ":", "\x00")):
        raise HTTPException(422, f"invalid project in proposal frontmatter: {project!r}")

    project_dir = VAULT / "Projects" / project
    if not project_dir.is_dir():
        raise HTTPException(422, f"project folder not found in vault: Projects/{project}")
    register = project_dir / _ISSUES_REGISTER_FILENAME

    # Single-line everything that lands in register Markdown (codex finding 4
    # — a CR/LF in an extracted title could inject a fake heading/checkbox).
    title = _one_line(title)
    priority = _one_line(meta.get("suggested_priority")).upper()
    priority = priority if priority in ("P1", "P2", "P3") else ""
    owner = _one_line(meta.get("owner"))
    affects = _one_line(meta.get("affects"))
    provenance = _one_line(meta.get("provenance"))
    date = _one_line(meta.get("date"))
    why = _one_line(meta.get("why"))
    gating_raw = meta.get("gating")
    gating = [_one_line(g) for g in gating_raw if _one_line(g)] if isinstance(gating_raw, list) else []

    # Idempotency marker: re-routing the same proposal (e.g. the bridge died
    # between register write and proposal retire — codex finding 3) must not
    # append a duplicate section. The marker carries the proposal id, which is
    # stable (sha1 of the proposal's vault-relative path).
    marker = f"<!-- issue-candidate:{proposal_id} -->"

    # Read-allocate-append-write under the lock (codex finding 2): two
    # concurrent Route clicks must not allocate the same ISS-NN.
    with _ISSUE_ROUTE_LOCK:
        base = register.read_text(encoding="utf-8") if register.is_file() else _seed_issues_register(project)
        created = not register.is_file()
        already_applied = marker in base

        if already_applied:
            issue_id = ""  # nothing appended this pass
        else:
            issue_id = next_issue_id(base)
            block_lines = [f"## {issue_id} — {title}", "- **status:** open"]
            if priority:
                block_lines.append(f"- **priority:** {priority}")
            if owner:
                block_lines.append(f"- **owner:** {owner}")
            raised = f"- **raised:** {date}" if date else "- **raised:**"
            if provenance:
                raised += f" — {provenance}"
            block_lines.append(raised)
            if affects:
                block_lines.append(f"- **affects:** {affects}")
            if why:
                block_lines.append(f"- **why:** {why}")
            if gating:
                block_lines.append("- **gating items:**")
                block_lines += [f"  - [ ] {g} [issue:{issue_id}]" for g in gating]
            block_lines.append(marker)
            block = "\n".join(block_lines)

            new_text = _append_issue_block(base, block)
            try:
                atomic_write(register, new_text, vault_root=VAULT)
            except OSError as e:
                audit.write_structured_safe(
                    actor={"type": "user", "id": "operator"},
                    entity_type="proposal",
                    entity_id=proposal_id,
                    action="route",
                    routine="proposals.route",
                    audit_dir=RUNS_DIR,
                    run_id=run_id,
                    status="error",
                    inputs={"id": proposal_id, "kind": "issue-candidate", "project": project},
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise HTTPException(500, f"append-to-register failed: {e}") from e

    applied_dir = VAULT / "_processing" / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_in(applied_dir, src_path.name)
    shutil.move(str(src_path), str(dest))
    for src_side, dst_builder in (
        (_skip_sidecar_for(src_path), _skip_sidecar_for),
        (_revision_sidecar_for(src_path), _revision_sidecar_for),
    ):
        if src_side.is_file():
            shutil.move(str(src_side), str(dst_builder(dest)))

    register_str = str(register)
    audit.write_structured_safe(
        actor={"type": "user", "id": "operator"},
        entity_type="proposal",
        entity_id=proposal_id,
        action="route",
        routine="proposals.route",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "id": proposal_id, "kind": "issue-candidate", "tier": "approval",
            "project": project, "provenance": provenance,
        },
        outputs={
            "appended_to": register_str, "issue_id": issue_id,
            "register_created": created, "retired_to": str(dest),
            "already_applied": already_applied,
        },
        duration_ms=int((time.monotonic() - started) * 1000),
        details={
            "status": "ok",
            "kind": "issue-candidate", "tier": "approval",
            "appended_to": register_str, "issue_id": issue_id,
            "register_created": created, "already_applied": already_applied,
        },
    )
    return RouteResponse(moved_to=register_str)


def _destination_for(workspace_type: WorkspaceType, workspace_name: str, kind: str) -> Path:
    """Map (kind, workspace_type) → destination directory in the vault.

    Raises 422 for combinations we haven't wired yet (so the failure is
    explicit + auditable rather than silently routing to a wrong path)."""
    # General workspace is a catch-all landing zone for any kind — Inbox/Captures.
    if workspace_type == "general":
        return VAULT / "Inbox" / "Captures" / workspace_name

    # Sector extraction goes to the topic tree regardless of project/bd.
    if kind == "sector-extraction":
        return VAULT / "Topics" / "Sectors" / workspace_name

    # Project + BD kinds with explicit subfolder mappings.
    if workspace_type in ("project", "bd"):
        if kind == "hinotes-unrouted":
            return VAULT / "Projects" / workspace_name / "02 Meeting Notes"
        if kind == "email-unrouted":
            return VAULT / "Projects" / workspace_name / "03 Emails"

    raise HTTPException(
        422,
        f"routing not supported for (kind={kind!r}, workspace_type={workspace_type!r}); "
        f"reject or skip the proposal instead, or extend _destination_for in "
        f"routines/api/routes/proposals.py",
    )


def _unique_in(directory: Path, filename: str) -> Path:
    """Resolve a collision-free destination inside ``directory``. Appends
    ``-1`` / ``-2`` / ... before the suffix until a free name is found.

    Used by reject so a second rejection of an identically-named file
    doesn't clobber the first audit trail."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        cand = directory / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def _extract_title(body: str, *, fallback: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()
    return fallback


def _read_title(path: Path) -> str:
    """Read just the first H1 from a flat-file proposal (no frontmatter)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem
    return _extract_title(text, fallback=path.stem)


def _date_from_filename(path: Path) -> Optional[str]:
    """Extract a leading YYYY-MM-DD from the filename, if present."""
    stem = path.stem
    if len(stem) >= 10 and stem[4] == "-" and stem[7] == "-":
        candidate = stem[:10]
        try:
            from datetime import date
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            return None
    return None
