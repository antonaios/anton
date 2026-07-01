"""Projects API routes.

  * GET  /api/projects/{project}/actions            — aggregated open / overdue / stale / done lists
  * POST /api/projects/{project}/actions/toggle     — flip a checkbox between [ ] and [x]
  * GET  /api/projects/{project}/overview           — brief-frontmatter reader for the right-rail tile

The deprecated ``GET /api/projects`` list endpoint (a thin wrapper around
``GET /api/workspaces?type=project&filter=vault``) was removed in #6c-routines
(Session 15, 2026-05-28) after Session 12 (dashboard ``d147936``) migrated the
last dashboard callers to ``api.workspaces({type: "project"})``. The wrapper
had carried ``Deprecation: true`` since 2026-05-26 (#6c, routines ``760096e``).

Action endpoints follow the 2026-05-23 inline-tag convention; see
``routines/projects/actions.py`` for the parser + the workspace-write-policy
note in the vault for the convention spec.

Overview endpoint implements OUTSTANDING.md ## CONTRACTS · project overview
(#11, locked 2026-05-24). Reads ``Projects/<name>/00 Brief.md`` frontmatter
per the canonical template at ``Projects/_template/00 Brief.md``.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import frontmatter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT, ROUTINES_REPO, vault_paths
from routines.projects import actions as actions_mod
from routines.projects import issues as issues_mod
from routines.shared import audit, profile as profile_mod

router = APIRouter()
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/projects/{project}/actions
# ──────────────────────────────────────────────────────────────────────────

class ActionItem(BaseModel):
    title: str
    status: Literal["open", "overdue", "stale", "done"]
    due: str | None = None        # ISO date YYYY-MM-DD
    owner: str = ""
    urgent: bool = False
    flag: bool = False
    done: str | None = None
    source_file: str              # absolute path; carry back unchanged for toggle
    source_line: int              # 1-indexed; hint only
    task_hash: str                # 8-char sha1 of normalised title
    issue: str | None = None      # [issue:ISS-NN] — issue grouping (#issues-register v2)


class ActionsCounts(BaseModel):
    overdue: int
    open: int
    stale: int
    done: int
    total_open: int               # overdue + open + stale (everything not done)


class ActionsResponse(BaseModel):
    project: str
    overdue: list[ActionItem]
    open: list[ActionItem]
    stale: list[ActionItem]
    done: list[ActionItem]
    counts: ActionsCounts


def _to_pydantic(a: actions_mod.Action) -> ActionItem:
    return ActionItem(
        title=a.title,
        status=a.status,  # type: ignore[arg-type]
        due=a.due,
        owner=a.owner,
        urgent=a.urgent,
        flag=a.flag,
        done=a.done,
        source_file=a.source_file,
        source_line=a.source_line,
        task_hash=a.task_hash,
        issue=a.issue,
    )


@router.get("/projects/{project}/actions", response_model=ActionsResponse)
def project_actions(project: str) -> ActionsResponse:
    """Return aggregated actions for ``project``, grouped by status."""
    try:
        items = actions_mod.aggregate(VAULT, project)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Action aggregation failed: {e}") from e

    overdue: list[ActionItem] = []
    open_: list[ActionItem] = []
    stale: list[ActionItem] = []
    done: list[ActionItem] = []
    for a in items:
        item = _to_pydantic(a)
        if a.status == "overdue":
            overdue.append(item)
        elif a.status == "stale":
            stale.append(item)
        elif a.status == "done":
            done.append(item)
        else:
            open_.append(item)

    return ActionsResponse(
        project=project,
        overdue=overdue,
        open=open_,
        stale=stale,
        done=done,
        counts=ActionsCounts(
            overdue=len(overdue),
            open=len(open_),
            stale=len(stale),
            done=len(done),
            total_open=len(overdue) + len(open_) + len(stale),
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# GET /api/projects/{project}/issues — issues-register reader (#issues-register v2)
# ──────────────────────────────────────────────────────────────────────────

class IssueGatingItem(BaseModel):
    title: str
    checked: bool
    due: str | None = None
    owner: str | None = None
    urgent: bool = False
    line: int                     # 1-indexed in the register file


class IssueItem(BaseModel):
    id: str                       # "ISS-03"
    title: str
    status: Literal["open", "monitoring", "blocked", "closed"]
    priority: str | None = None   # "P1" | "P2" | "P3"
    owner: str | None = None
    raised: str | None = None
    affects: str | None = None
    resolution: str | None = None
    line: int                     # heading line in the register file
    gating: list[IssueGatingItem]
    gating_open: int
    gating_total: int


class IssuesCounts(BaseModel):
    open: int
    monitoring: int
    blocked: int
    closed: int
    non_closed: int               # the /agenda (v3) contract: everything not closed


class IssuesResponse(BaseModel):
    project: str
    exists: bool                  # False = project predates the v1 register template
    register_path: str | None = None  # vault-relative POSIX, when the project resolves
    issues: list[IssueItem]
    counts: IssuesCounts


@router.get("/projects/{project}/issues", response_model=IssuesResponse)
def project_issues(project: str) -> IssuesResponse:
    """Parse ``Projects/<project>/14 Issues & Outstanding.md`` into typed issue
    records (#issues-register v2). Read-only; no audit row on GET (matches the
    actions endpoint — dashboard polls must not fabricate operator-attributed
    audit rows). 404 on unsafe names / missing project; a missing register file
    on an existing project returns ``exists: false`` with empty issues."""
    register = issues_mod.resolve_register_path(VAULT, project)
    if register is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project}")

    _, parsed = issues_mod.load_register(VAULT, project)

    items = [
        IssueItem(
            id=i.id,
            title=i.title,
            status=i.status,  # type: ignore[arg-type]
            priority=i.priority,
            owner=i.owner,
            raised=i.raised,
            affects=i.affects,
            resolution=i.resolution,
            line=i.line,
            gating=[
                IssueGatingItem(
                    title=g.title, checked=g.checked, due=g.due,
                    owner=g.owner, urgent=g.urgent, line=g.line,
                )
                for g in i.gating
            ],
            gating_open=i.gating_open,
            gating_total=i.gating_total,
        )
        for i in parsed
    ]

    by_status = {s: 0 for s in issues_mod.KNOWN_STATUSES}
    for i in items:
        by_status[i.status] += 1

    try:
        register_rel = register.relative_to(Path(VAULT).resolve()).as_posix()
    except ValueError:
        register_rel = register.as_posix()

    return IssuesResponse(
        project=project,
        exists=register.is_file(),
        register_path=register_rel,
        issues=items,
        counts=IssuesCounts(
            open=by_status["open"],
            monitoring=by_status["monitoring"],
            blocked=by_status["blocked"],
            closed=by_status["closed"],
            non_closed=by_status["open"] + by_status["monitoring"] + by_status["blocked"],
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# POST /api/projects/{project}/actions/toggle
# ──────────────────────────────────────────────────────────────────────────

class ToggleRequest(BaseModel):
    source_file: str = Field(..., description="Absolute path returned by the GET endpoint.")
    task_hash: str = Field(..., description="8-char hash from the Action record.")
    line_hint: int | None = Field(
        None, ge=1,
        description="1-indexed line number from the Action record. Fast-path lookup; "
                    "falls back to whole-file hash scan if hint misses.",
    )
    to: Literal["open", "done"]


class ToggleCandidate(BaseModel):
    line: int
    snippet: str


class ToggleResponse(BaseModel):
    success: bool
    line: int | None = None
    snippet: str | None = None
    candidates: list[ToggleCandidate] | None = None


def _is_safe_source(path: Path, profile: profile_mod.OperatorProfile) -> bool:
    """Bridge security: refuse to write outside the vault + external project paths.

    Prevents a malicious source_file in the toggle request from rewriting
    arbitrary files on the host. Only paths under VAULT or any of the
    operator's ``external_project_paths`` are writable.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    allowed_roots: list[Path] = [VAULT.resolve()]
    for ext in profile.external_project_paths:
        try:
            allowed_roots.append(Path(ext).resolve())
        except OSError:
            continue
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


@router.post("/projects/{project}/actions/toggle", response_model=ToggleResponse)
def toggle_action(project: str, req: ToggleRequest) -> ToggleResponse:
    """Flip a task's checkbox state. Writes the source file in place + audit-logs."""
    profile = profile_mod.load(VAULT)
    src = Path(req.source_file)

    if not _is_safe_source(src, profile):
        raise HTTPException(
            status_code=403,
            detail=(
                f"refused: source_file is outside vault and external_project_paths "
                f"(file: {src})"
            ),
        )

    run_id = audit.new_run_id()
    t0 = time.monotonic()
    result = actions_mod.toggle(
        source_file=src,
        task_hash=req.task_hash,
        to=req.to,
        line_hint=req.line_hint,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    if not result.success:
        # 409 on ambiguous-hash collisions, 404 on not-found, 400 otherwise.
        status_code = 409 if result.candidates else (
            404 if (result.error or "").startswith("task not found") else 400
        )
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="workspace",
            entity_id=project,
            action="toggle",
            routine="projects.actions.toggle",
            run_id=run_id,
            status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={
                "project": project,
                "source_file": str(src),
                "task_hash": req.task_hash,
                "to": req.to,
                "line_hint": req.line_hint,
            },
            error=result.error,
            duration_ms=duration_ms,
            extra={"candidates": result.candidates} if result.candidates else None,
        )
        if result.candidates is not None:
            # 409: surface candidates for re-prompting
            raise HTTPException(
                status_code=status_code,
                detail={
                    "error": result.error or "ambiguous match",
                    "candidates": result.candidates,
                },
            )
        raise HTTPException(status_code=status_code, detail=result.error or "toggle failed")

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="workspace",
        entity_id=project,
        action="toggle",
        routine="projects.actions.toggle",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={
            "project": project,
            "source_file": str(src),
            "task_hash": req.task_hash,
            "to": req.to,
            "line_hint": req.line_hint,
        },
        outputs={
            "line": result.line,
            "snippet": result.snippet,
        },
        duration_ms=duration_ms,
        semantic_target=str(src),
    )

    return ToggleResponse(
        success=True,
        line=result.line,
        snippet=result.snippet,
    )


# ──────────────────────────────────────────────────────────────────────────
# GET /api/projects/{name}/overview — brief frontmatter reader
# ──────────────────────────────────────────────────────────────────────────
#
# Powers ProjectPanel right-rail (HARNESS #12). Contract: see OUTSTANDING.md
# ## CONTRACTS · project overview (#11). Field names match the canonical
# brief template at Projects/_template/00 Brief.md verbatim — wikilinked
# entity fields are returned as bare display strings.

# Allowed enumerations (mirror frontmatter values in the template).
ClientSide = Literal["buy", "sell", "advisory"]
Stage = Literal["pitch", "kick-off", "DD", "bid-1", "bid-2", "signing", "close"]
Status = Literal["live", "paused", "won", "lost", "archived"]
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]
KeyDateState = Literal["done", "next", "future"]


class KeyDate(BaseModel):
    label: str
    date: str | None = None    # ISO YYYY-MM-DD passthrough; missing → None
    state: KeyDateState = "future"


class ProjectOverview(BaseModel):
    name: str
    client_side: ClientSide | None = None
    sector: str | None = None
    subsector: str | None = None
    industry: str | None = None
    stage: Stage | None = None
    owner: str | None = None
    status: Status | None = None
    sensitivity: Sensitivity | None = None
    target: str | None = None
    counterparty: str | None = None
    client: str | None = None
    tldr: str | None = None
    opened: str | None = None
    closed: str | None = None
    # Best-effort project-start anchor: the ``opened`` frontmatter date when
    # present, else the project folder's creation time (Windows ctime). Always
    # populated so a lifetime-spend "since project opened" query has an anchor
    # even when the operator left ``opened`` blank in the brief.
    created: str | None = None
    key_dates: list[KeyDate] = Field(default_factory=list)
    last_touched: str          # ISO-8601 UTC from brief mtime


# Bare-name extractor for wikilinks. We use string ops rather than a regex so
# that the path prefix is stripped GREEDILY — "[[Sectors/telecoms/_Index]]"
# resolves to "_Index", not "telecoms/_Index". Real briefs nest Sector pages
# under sub-folders (see Projects/DemoTarget/00 Brief.md).
def _strip_wikilink(value: Any) -> str | None:
    """Return the display name from a wikilinked frontmatter value.

    Examples:
        "[[Companies/DemoTelco Group plc]]"  → "DemoTelco Group plc"
        "[[Sectors/telecoms/_Index]]"        → "_Index"
        "[[Bare]]"                            → "Bare"
        "DemoTelco Group plc"                 → "DemoTelco Group plc"
        ""                                   → None
        None                                 → None
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[[") and text.endswith("]]"):
        inner = text[2:-2].strip()
        if "/" in inner:
            inner = inner.rsplit("/", 1)[-1]
        return inner.strip() or None
    return text


def _coerce_iso_date(value: Any) -> str | None:
    """Frontmatter dates may arrive as ``date``/``datetime``/``str``.
    Normalise to ``YYYY-MM-DD``; return None on placeholder / empty values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    # `date` is a subclass of nothing else here, but ``datetime.date`` works:
    from datetime import date as _date
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.upper() in {"YYYY-MM-DD", "TBD", "N/A"}:
        return None
    return text


def _coerce_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    """Lowercase-strip and check against ``allowed``. Returns None on miss —
    we silently drop unrecognised values rather than 500ing on brief drift."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"buy | sell | advisory", "pitch | kick-off | DD | bid-1 | bid-2 | signing | close"}:
        # Template placeholders left in by the operator.
        return None
    return text if text in allowed else None


def _parse_key_dates(raw: Any) -> list[KeyDate]:
    """Parse the ``key-dates`` frontmatter list. Skips malformed entries
    (non-dict, missing label, unknown state) rather than failing the whole
    response — partial briefs are common during the project-bootstrap phase."""
    if not isinstance(raw, list):
        return []
    out: list[KeyDate] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not label or not str(label).strip():
            continue
        state = entry.get("state", "future")
        if state not in ("done", "next", "future"):
            state = "future"
        out.append(KeyDate(
            label=str(label).strip(),
            date=_coerce_iso_date(entry.get("date")),
            state=state,  # type: ignore[arg-type]
        ))
    return out


def _resolve_brief_path(name: str) -> Path | None:
    """Locate ``Projects/<name>/00 Brief.md`` within VAULT — fail-closed on
    path traversal.

    Returns the resolved path if it sits under ``VAULT/Projects/`` AND the
    brief exists. Returns None on:
      - name containing path separators (``/`` ``\\``) or relative segments (``..``)
      - resolved location outside ``VAULT/Projects/``
      - brief file missing on disk
    """
    if not name or any(sep in name for sep in ("/", "\\")) or ".." in name.split():
        return None
    # Extra belt-and-braces: even a single-segment name like ``..`` shouldn't slip through.
    if name in (".", ".."):
        return None

    projects_root = (VAULT / "Projects").resolve()
    candidate = (projects_root / name / "00 Brief.md").resolve()
    try:
        candidate.relative_to(projects_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _build_project_overview(name: str) -> ProjectOverview | None:
    """Pure builder: read ``Projects/<name>/00 Brief.md`` and return the
    ``ProjectOverview`` shape, or ``None`` if the brief is missing /
    path-traversal-refused. NEVER writes audit rows — callers that need
    audit attribution (e.g. the HTTP wrapper) do their own write.

    Split out from :func:`project_overview` (FIX 2) so the dashboard
    rollup's per-project fan-out doesn't fabricate ~N audit rows every
    5 minutes attributed to ``operator`` without an operator initiating
    the call."""
    brief = _resolve_brief_path(name)
    if brief is None:
        return None

    try:
        post = frontmatter.load(str(brief))
        meta: dict[str, Any] = dict(post.metadata or {})
    except Exception:  # noqa: BLE001
        log.exception("projects.overview: frontmatter parse failed for %s", brief)
        # Treat unreadable frontmatter as "present but empty" — return the
        # shape with all optionals as None rather than 500ing on a YAML wart.
        meta = {}

    last_touched = datetime.fromtimestamp(brief.stat().st_mtime, tz=timezone.utc).isoformat()

    # created: prefer the explicit ``opened`` frontmatter date; else fall back to
    # the project folder's creation time (ctime) so a "since project opened"
    # lifetime figure always has an anchor. ``opened`` is validated as a CANONICAL
    # YYYY-MM-DD first — _coerce_iso_date passes free-form strings through verbatim
    # (e.g. "Q1 2026", "15/03/2026"), and a non-canonical anchor would 422 the
    # downstream llm-burn ``since`` query (which appends "T00:00:00Z"). An
    # unparseable/malformed ``opened`` therefore falls through to the ctime anchor.
    opened_iso = _coerce_iso_date(meta.get("opened"))
    created_iso: str | None = None
    if opened_iso is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", opened_iso):
        try:
            date.fromisoformat(opened_iso)
            created_iso = opened_iso
        except ValueError:
            created_iso = None
    if created_iso is None:
        try:
            created_iso = datetime.fromtimestamp(
                brief.parent.stat().st_ctime, tz=timezone.utc,
            ).date().isoformat()
        except OSError:
            created_iso = None

    return ProjectOverview(
        name=name,
        client_side=_coerce_enum(meta.get("client-side"), ("buy", "sell", "advisory")),  # type: ignore[arg-type]
        sector=_strip_wikilink(meta.get("sector")),
        subsector=(str(meta["subsector"]).strip() or None) if meta.get("subsector") else None,
        industry=(str(meta["industry"]).strip() or None) if meta.get("industry") else None,
        stage=_coerce_enum(  # type: ignore[arg-type]
            meta.get("stage"),
            ("pitch", "kick-off", "DD", "bid-1", "bid-2", "signing", "close"),
        ),
        owner=(str(meta["owner"]).strip() or None) if meta.get("owner") else None,
        status=_coerce_enum(  # type: ignore[arg-type]
            meta.get("status"),
            ("live", "paused", "won", "lost", "archived"),
        ),
        sensitivity=_coerce_enum(  # type: ignore[arg-type]
            meta.get("sensitivity"),
            ("public", "internal", "confidential", "MNPI"),
        ),
        target=_strip_wikilink(meta.get("target")),
        counterparty=_strip_wikilink(meta.get("counterparty")),
        client=_strip_wikilink(meta.get("client")),
        tldr=(str(meta["tldr"]).strip() or None) if meta.get("tldr") else None,
        opened=opened_iso,
        closed=_coerce_iso_date(meta.get("closed")),
        created=created_iso,
        key_dates=_parse_key_dates(meta.get("key-dates")),
        last_touched=last_touched,
    )


@router.get("/projects/{name}/overview", response_model=ProjectOverview)
def project_overview(name: str) -> ProjectOverview:
    """Read the project's ``00 Brief.md`` frontmatter and return the
    ProjectOverview shape per OUTSTANDING ## CONTRACTS · project overview.

    Missing optional fields return ``null``. Malformed ``key-dates`` entries
    are skipped (logged); the rest of the response is unaffected. Path
    traversal attempts return 404, never 200.

    HTTP wrapper around :func:`_build_project_overview`. Writes the
    ``projects.overview`` audit row attributed to ``operator`` — the
    rollup code path calls the pure builder directly so dashboard polls
    don't fabricate audit rows the operator can't trace back.
    """
    run_id = audit.new_run_id()
    t0 = time.monotonic()

    overview = _build_project_overview(name)
    if overview is None:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="workspace",
            entity_id=name,
            action="overview",
            routine="projects.overview",
            run_id=run_id,
            status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"name": name},
            error="project not found or path-traversal refused",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(status_code=404, detail=f"project not found: {name}")

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="workspace",
        entity_id=name,
        action="overview",
        routine="projects.overview",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"name": name},
        outputs={
            "stage": overview.stage,
            "status": overview.status,
            "sensitivity": overview.sensitivity,
            "key_dates_count": len(overview.key_dates),
        },
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

    return overview
