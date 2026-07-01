"""Actions-decay skill bridge route (#21 — eighth SKILL.md migration).

``POST /api/workflows/actions-decay`` — fires a deterministic cross-project
sweep over vault ``Projects/`` AND every ``external_project_paths`` root
declared in ``_claude/profile.md``, runs the per-project actions aggregator
on each discovered project, and returns the set of overdue + stale rows as
JSON. The handler:

  1. Reads the skill registry for governance metadata (sensitivity, scope,
     cost caps) — no inlined constants.
  2. Wraps the in-process scan call in the real ``tool_call_hooks`` context
     manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope: any``,
     ``sensitivity: internal``) the guard is a structural NO-OP for the
     common case; the only firing path is the cross-skill MNPI gate.
  3. Resolves the profile's ``external_project_paths`` BEFORE calling
     ``decay.scan`` so the response can surface ``roots_resolved`` +
     ``roots_unresolved`` — the Iron Law's "roots surfaced" clause is a
     route-layer responsibility (the routine itself doesn't track this).
  4. Calls ``routines.projects.decay.scan`` directly (no subprocess — the
     routine is pure file-walk + frontmatter parse, sub-second to ~5s on a
     typical few-dozen-project tree).
  5. Surfaces ``projects_failed`` per the routine's existing skip-on-error
     contract by re-running the aggregator with try/except so the route
     captures the failures (the routine logs them but doesn't return them).

The existing per-project routes at ``/api/projects/{project}/actions``
(GET) and ``/api/projects/{project}/actions/toggle`` (POST) — in
``routines/api/routes/projects.py`` — are UNTOUCHED. This is the
cross-project sweep, a separate surface. The 06:45 daily cron continues
to call ``routines.projects.decay.scan`` DIRECTLY (no skill dispatch);
this route is the on-demand operator surface.
"""

from __future__ import annotations

import logging
import time
from datetime import date as date_cls
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.projects import actions as _actions
from routines.projects import decay as _decay
from routines.shared import audit
from routines.shared import profile as _profile_mod
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# Constant — the routine's stale threshold. Surfaced in the response so the
# operator can sanity-check what "stale" means right now. Read directly
# from the actions module so a future change to the constant flows through
# without the route needing a manual update.
STALE_AFTER_DAYS = _actions.STALE_AFTER_DAYS

# Vault sentinel used in roots_resolved to distinguish the vault Projects/
# root from external filesystem roots (both could be absolute paths on
# Windows; the vault:// scheme is unambiguous).
_VAULT_PROJECTS_ROOT = "vault://Projects/"


OutputFormat = Literal["summary", "json", "brief"]


# ── request / response models ───────────────────────────────────────────────


class ActionsDecayRequest(BaseModel):
    """On-demand actions-decay request from the dashboard or Cmd-K.

    Defaults mirror the cron job's behaviour (today=today). ``today`` is
    an optional ISO override for testing / deterministic replay.
    ``format`` is accepted for parity with the CLI's
    ``actions-decay scan --format`` flag, but the JSON response carries
    the full structured payload regardless — ``brief`` adds a rendered
    markdown snippet (via ``decay.format_for_morning_brief``), ``json``
    is the default structured shape, ``summary`` returns counts only."""

    today: Optional[str] = None
    format: OutputFormat = "json"
    # workspace fields are conventional across all skill routes (#61) — for
    # this any-scope, internal skill they pass through the central guard
    # without effect (except for MNPI inputs, which the guard refuses).
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class StaleActionOut(BaseModel):
    """One overdue or stale action surfaced in the response. Mirrors the
    routine's :class:`StaleAction` dataclass; every field is required so
    the Iron Law's provenance-complete clause cannot be silently violated."""

    project: str
    title: str
    status: str
    due: Optional[str] = None
    owner: str
    urgent: bool
    flag: bool
    source_file: str
    source_line: int
    task_hash: str


class UnresolvedRoot(BaseModel):
    """A profile-declared external_project_paths entry that did not resolve."""

    profile_path: str
    reason: str


class FailedProject(BaseModel):
    """A project whose per-project aggregator raised (logged + skipped)."""

    project: str
    reason: str


class ActionsDecayCounts(BaseModel):
    """Headline counts — operator sanity-check shape."""

    overdue: int
    stale: int
    projects_scanned: int
    projects_failed: int


class ActionsDecayResult(BaseModel):
    """Structured actions-decay sweep result. Pure-return data — no file
    write (cousin shape to bd-decay). ``roots_resolved`` is the Iron
    Law's "roots surfaced" disclosure: vault://Projects/ + each existing
    external_project_paths entry. ``roots_unresolved`` carries any
    profile entry that didn't resolve to an existing directory."""

    status: Literal["ok", "error"]
    run_id: str
    today: str
    thresholds_applied: dict[str, int] = Field(default_factory=dict)
    roots_resolved: list[str] = Field(default_factory=list)
    roots_unresolved: list[UnresolvedRoot] = Field(default_factory=list)
    projects_scanned: list[str] = Field(default_factory=list)
    projects_failed: list[FailedProject] = Field(default_factory=list)
    overdue: list[StaleActionOut] = Field(default_factory=list)
    stale: list[StaleActionOut] = Field(default_factory=list)
    counts: ActionsDecayCounts
    rendered_markdown: str = ""
    duration_ms: int = 0
    error: Optional[str] = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _resolve_roots(
    vault_root: Path,
    profile: _profile_mod.OperatorProfile,
) -> tuple[list[str], list[UnresolvedRoot]]:
    """Enumerate the paths the walker WILL visit + the ones it can't.

    Iron Law clause 2 — the response MUST surface every path the walker
    visited AND every profile entry that didn't resolve, so the operator
    can sanity-check multi-root resolution. Always lists ``vault_root /
    Projects/`` first (as ``vault://Projects/``) if it exists; then each
    external entry that resolves to an existing directory; then the
    unresolved entries with reason."""
    resolved: list[str] = []
    unresolved: list[UnresolvedRoot] = []

    vault_projects = vault_root / "Projects"
    if vault_projects.is_dir():
        resolved.append(_VAULT_PROJECTS_ROOT)
    else:
        unresolved.append(UnresolvedRoot(
            profile_path=str(vault_projects),
            reason="vault Projects/ directory does not exist",
        ))

    for root_str in profile.external_project_paths:
        root = Path(root_str)
        if root.is_dir():
            # Forward-slash, no trailing dedupe — the path is the
            # operator's declared value, surfaced verbatim so it matches
            # what they see in profile.md.
            resolved.append(str(root).replace("\\", "/"))
        else:
            unresolved.append(UnresolvedRoot(
                profile_path=root_str,
                reason="directory does not exist",
            ))

    return resolved, unresolved


def _action_out(project: str, a: _actions.Action) -> StaleActionOut:
    """Map a routine ``Action`` (per-project aggregator output) to the
    response's ``StaleActionOut`` shape. Used both for routine-side
    overdue/stale rows AND for re-running the aggregator with project-
    level error capture."""
    return StaleActionOut(
        project=project,
        title=a.title,
        status=a.status,
        due=a.due,
        owner=a.owner,
        urgent=a.urgent,
        flag=a.flag,
        source_file=a.source_file,
        source_line=a.source_line,
        task_hash=a.task_hash,
    )


def _stale_out(s: _decay.StaleAction) -> StaleActionOut:
    """Map the routine's ``StaleAction`` to the response shape. Mirrors
    the routine's dataclass field-for-field — Iron Law clause 1's
    provenance-complete check passes mechanically because each field is
    pydantic-required."""
    return StaleActionOut(
        project=s.project,
        title=s.title,
        status=s.status,
        due=s.due,
        owner=s.owner,
        urgent=s.urgent,
        flag=s.flag,
        source_file=s.source_file,
        source_line=s.source_line,
        task_hash=s.task_hash,
    )


def _collect_failed_projects(
    vault_root: Path,
    profile: _profile_mod.OperatorProfile,
    today: date_cls,
    projects: list[str],
) -> list[FailedProject]:
    """Re-run the aggregator per project under try/except to capture
    which projects raised.

    The routine's ``decay.scan`` calls ``actions_mod.aggregate`` under a
    bare except + log.warning (line 113-115 of decay.py); it doesn't
    return the failing project names. To honour the Iron Law (no silent
    swallow), the route re-runs the aggregator on each scanned project
    and collects which ones raise. This duplicates the file-walk work
    (~2x cost) but keeps `decay.py` untouched + delivers the
    `projects_failed` field operator-visibly. The duplication is
    acceptable because the routine is sub-second to ~5s typical."""
    failed: list[FailedProject] = []
    for project in projects:
        try:
            _actions.aggregate(vault_root, project, profile=profile, today=today)
        except Exception as e:  # noqa: BLE001 — capture every failure mode
            failed.append(FailedProject(
                project=project,
                reason=f"{type(e).__name__}: {e}",
            ))
    return failed


# ── route ───────────────────────────────────────────────────────────────────


@router.post("/actions-decay", response_model=ActionsDecayResult)
@anton_skill("actions-decay")
def run_workflow_actions_decay(req: ActionsDecayRequest) -> ActionsDecayResult:
    """Run an actions-decay sweep on demand. See module docstring.

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

    # Resolve the profile BEFORE calling decay.scan so we can
    # both pass it in (avoid the routine re-loading it) AND use
    # it for the roots_resolved + roots_unresolved enumeration.
    profile = _profile_mod.load(VAULT)
    roots_resolved, roots_unresolved = _resolve_roots(VAULT, profile)

    # Stage 1-3 — scan. The routine walks projects + aggregates +
    # buckets by status. Sub-second to ~5s typical. The Iron Law
    # applies at the route boundary: an exception is NOT a clean
    # pass — surface verbatim.
    try:
        sweep = _decay.scan(VAULT, profile=profile, today=today)
    except Exception as e:  # noqa: BLE001 — scan errors map to 500
        log.error("actions-decay scan failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"actions-decay scan failed: {e}",
        )

    # Stage 4 — surface with roots_resolved + projects_failed.
    # The routine logs + skips per-project failures; re-run the
    # aggregator under try/except to capture which projects
    # raised (Iron Law: no silent swallow on aggregator
    # exceptions).
    projects_failed = _collect_failed_projects(
        VAULT, profile, today, sweep.projects_scanned,
    )

    # Map StaleAction rows to response shape. The routine's
    # status-mapping layer (lines 312-319 of actions.py) already
    # excludes 'done' rows from both buckets; a `done` row
    # appearing here would be a routine drift — filter
    # defensively at the route boundary to honour Iron Law
    # clause 3, and log if any was seen so the drift surfaces.
    overdue_out: list[StaleActionOut] = []
    for s in sweep.overdue:
        if s.status == "done":
            log.warning(
                "actions-decay: done row in overdue bucket "
                "(routine drift): project=%s title=%s",
                s.project, s.title,
            )
            continue
        overdue_out.append(_stale_out(s))

    stale_out: list[StaleActionOut] = []
    for s in sweep.stale:
        if s.status == "done":
            log.warning(
                "actions-decay: done row in stale bucket "
                "(routine drift): project=%s title=%s",
                s.project, s.title,
            )
            continue
        stale_out.append(_stale_out(s))

    rendered_markdown = ""
    if req.format == "brief":
        rendered_markdown = _decay.format_for_morning_brief(sweep)

    counts = ActionsDecayCounts(
        overdue=len(overdue_out),
        stale=len(stale_out),
        projects_scanned=len(sweep.projects_scanned),
        projects_failed=len(projects_failed),
    )

    return ActionsDecayResult(
        status="ok",
        run_id=run_id,
        today=today.isoformat(),
        thresholds_applied={"stale_after_days": STALE_AFTER_DAYS},
        roots_resolved=roots_resolved,
        roots_unresolved=roots_unresolved,
        projects_scanned=list(sweep.projects_scanned),
        projects_failed=projects_failed,
        overdue=overdue_out,
        stale=stale_out,
        counts=counts,
        rendered_markdown=rendered_markdown,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
