"""Workspaces API — #5 (create) + #6 (list).

Two endpoints over `<workspace-root>\\<type>\\<name>\\` (filesystem) and
`<vault>\\Projects\\<name>\\` (vault side, Project + BD only):

  * ``POST /api/workspaces``   — create a new workspace (atomic, two-tree)
  * ``GET  /api/workspaces``   — list existing workspaces, optionally
                                 filtered by ``type``

See [[OUTSTANDING]] ## CONTRACTS · workspaces for the locked schemas, and
[[workspace-write-policy]] §2 for the rule that drove these paths.

**Plural roots.** ``external_project_paths`` is a YAML list in
``profile.md`` so the operator can split active vs archive mandates across
multiple roots. The endpoints accommodate this by:

  * LIST — walks every configured root, tagging each result with
    ``source_root``; dashboard can group / filter later.
  * CREATE — defaults to ``paths[0]`` so single-root profiles behave
    unchanged; optional ``root_index`` lets a caller opt into a non-default
    root. Conflict check spans **every** configured root + the vault scaffold
    target, so a name clash in any tree returns 409.

**Atomic two-tree create.** When Project / BD creates need both filesystem
and vault scaffolds, we:

  1. Stage filesystem at ``<fs_root>/.<name>.tmp-<8hex>``
  2. Stage vault at ``<vault>/Projects/.<name>.tmp-<8hex>``
  3. Rename vault staging → final (vault commits first — smaller blast radius
     if FS fails afterwards)
  4. Rename fs staging → final
  5. On any failure: rmtree both staging dirs AND, if vault already
     committed, rmtree the vault final too — post-failure state is always
     "both present" or "both absent", never partial.

**General workspaces** skip the vault scaffold entirely — just an FS
``mkdir``. Per [[workspace-write-policy]] §2: General is flat, no M&A
subfolder set.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import frontmatter
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routines.api.deps import ROUTINES_REPO, VAULT
from routines.shared import audit, profile as profile_mod

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Types
# ────────────────────────────────────────────────────────────────────────────


WorkspaceType = Literal["project", "bd", "general"]


class WorkspacePaths(BaseModel):
    filesystem: str
    vault: str | None = None
    source_root: str


class CreatedWorkspace(BaseModel):
    type: WorkspaceType
    name: str
    paths: WorkspacePaths


class CreateWorkspaceRequest(BaseModel):
    type: WorkspaceType
    name: str = Field(..., min_length=1, max_length=64)
    root_index: int | None = Field(
        None,
        description=(
            "Project workspaces only: which entry in ``external_project_paths`` "
            "to write into. Defaults to 0. 422 if out of range."
        ),
    )


class CreateWorkspaceResponse(BaseModel):
    workspace: CreatedWorkspace


class WorkspaceListItem(BaseModel):
    type: WorkspaceType
    name: str
    last_touched: str             # ISO-8601 UTC
    source_root: str              # primary source — first of source_roots
    # #6c — dual-scan tagging. Only meaningful for type="project" today;
    # bd + general retain single-source semantics so the existing fields
    # are sufficient. Defaults keep the response backwards-compatible for
    # any consumer that doesn't read the new fields yet.
    in_vault: bool = False
    in_corporate_finance: bool = False
    source_roots: list[str] = Field(default_factory=list)


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceListItem]


# ────────────────────────────────────────────────────────────────────────────
# Name validation
# ────────────────────────────────────────────────────────────────────────────


# Letters, digits, space, underscore, hyphen. Must start with alnum (so leading
# `-`, ` `, `_`, `.` are all rejected). 1-64 chars total.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


def _validate_name(raw: str) -> str:
    """Normalise + validate a workspace name. Raises HTTPException(422) on reject.

    Strips whitespace; rejects path-traversal-shaped tokens and any character
    outside the safe-filename allow-list."""
    if not isinstance(raw, str):
        raise HTTPException(422, "name must be a string")
    name = raw.strip()
    if not name:
        raise HTTPException(422, "name must not be empty")
    if not _NAME_RE.match(name):
        raise HTTPException(
            422,
            (
                f"invalid workspace name {name!r}: must start with a letter or digit "
                "and contain only letters, digits, space, underscore, or hyphen "
                "(1-64 chars). No slashes, dots, colons, or other punctuation."
            ),
        )
    # Defensive belt-and-braces — _NAME_RE already excludes these, but a
    # future regex tweak shouldn't quietly open a path-traversal hole.
    for forbidden in ("..", "/", "\\", ":"):
        if forbidden in name:
            raise HTTPException(422, f"invalid workspace name: contains {forbidden!r}")
    return name


# ────────────────────────────────────────────────────────────────────────────
# Profile → roots
# ────────────────────────────────────────────────────────────────────────────


def _roots_for(t: WorkspaceType, prof: profile_mod.OperatorProfile) -> list[Path]:
    """Return every configured filesystem root for workspace type ``t``.

    BD and General use single-string config; we wrap them in a single-element
    list so every downstream code path treats roots symmetrically."""
    if t == "project":
        return [Path(p) for p in prof.external_project_paths if p]
    if t == "bd":
        return [Path(prof.external_bd_path)] if prof.external_bd_path else []
    if t == "general":
        return [Path(prof.external_general_path)] if prof.external_general_path else []
    raise HTTPException(422, f"unknown workspace type {t!r}")


# ────────────────────────────────────────────────────────────────────────────
# Scaffold templates
# ────────────────────────────────────────────────────────────────────────────


# M&A folder structure template — filesystem side. Per
# workspace-write-policy §2: applies to Project + BD only.
_FS_TEMPLATE_CANDIDATES = (
    Path("<fs-template>"),
    Path("os-templates/M&A folder structure template"),
)

# Repo-bundled portable default (ships with a clone) so a fresh deployment can
# scaffold workspaces out-of-the-box. ROUTINES_REPO is ``<repo>/routines``, so
# its parent is the repo root that carries ``templates/``.
_BUNDLED_FS_TEMPLATE = ROUTINES_REPO.parent / "templates" / "corporate-finance-deal-structure"


def _fs_template_candidates() -> tuple[Path, ...]:
    """Template-source resolution order — first existing dir wins:

      1. ``AGENTIC_FS_TEMPLATE`` env var — explicit operator/deployment override
      2. the operator's external template locations (their local install)
      3. the repo-bundled template — the portable default that ships with a
         clone, so a fresh deployment can create workspaces with no extra setup
    """
    cands: list[Path] = []
    env = os.environ.get("AGENTIC_FS_TEMPLATE")
    if env:
        cands.append(Path(env))
    cands.extend(_FS_TEMPLATE_CANDIDATES)
    cands.append(_BUNDLED_FS_TEMPLATE)
    return tuple(cands)


def _fs_scaffold_template() -> Path:
    """Locate the M&A folder structure template on disk. Raises 500 if no
    candidate exists — we refuse to invent a template path."""
    candidates = _fs_template_candidates()
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    raise HTTPException(
        500,
        f"M&A folder structure template not found; checked: "
        f"{[str(c) for c in candidates]}",
    )


def _vault_scaffold_template() -> Path:
    """Locate the vault Projects/_template/ directory. Raises 500 if missing —
    we refuse to bootstrap a project without the canonical scaffold."""
    template = VAULT / "Projects" / "_template"
    if not template.is_dir():
        raise HTTPException(
            500,
            f"vault Projects/_template/ scaffold missing at {template}",
        )
    return template


def _vault_path_for(name: str) -> Path:
    """Where the vault scaffold for ``name`` lands. Project + BD only — both
    share the same vault tree per [[workspace-write-policy]] §10."""
    return VAULT / "Projects" / name


# ────────────────────────────────────────────────────────────────────────────
# #6e brief frontmatter sync — workspace-type field + leading tag
# ────────────────────────────────────────────────────────────────────────────


def _sync_brief_frontmatter(brief_path: Path, workspace_type: WorkspaceType) -> None:
    """Inject/overwrite `workspace-type: <t>` in the brief frontmatter AND
    swap the leading element of `tags:` to match (project ↔ bd).

    #6e step 2 (scheduled 2026-05-26): the vault `_template/00 Brief.md` declares
    ``workspace-type: project`` plus a leading tag of ``project``. Pure copytree
    on a BD scaffold create would mis-tag the workspace as project. This helper
    runs AFTER the rename commits and rewrites the brief in place via
    ``python-frontmatter`` so both the field + the leading tag match the request type.

    Behaviour:
      * If the brief is missing, log a warning and return (don't fail the create —
        scaffold might legitimately not include a brief in the future).
      * If `tags` is missing or not a list, only the field is set (don't synthesise tags).
      * Other tags + other frontmatter fields are left untouched.
    """
    if not brief_path.is_file():
        log.warning("workspaces._sync_brief_frontmatter: brief not found at %s", brief_path)
        return
    try:
        post = frontmatter.load(brief_path)
    except Exception as e:  # noqa: BLE001
        log.warning("workspaces._sync_brief_frontmatter: failed to parse %s: %s", brief_path, e)
        return

    # 1. Set the canonical machine-read field. Pure overwrite — last writer
    #    wins by design; the template's default is benign.
    post.metadata["workspace-type"] = workspace_type

    # 2. Swap the leading tag to mirror. Only touch `tags` if it's already a
    #    list — never invent a tags field where the operator hasn't declared one.
    tags = post.metadata.get("tags")
    if isinstance(tags, list) and tags:
        # The first element is the workspace-type tag per the template
        # convention (commit 094e2fd). Replace it; other tags untouched.
        new_tags = list(tags)
        new_tags[0] = workspace_type
        post.metadata["tags"] = new_tags

    try:
        brief_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    except OSError as e:
        log.warning("workspaces._sync_brief_frontmatter: failed to write %s: %s", brief_path, e)


# ────────────────────────────────────────────────────────────────────────────
# POST /api/workspaces — create
# ────────────────────────────────────────────────────────────────────────────


@router.post(
    "/workspaces",
    response_model=CreateWorkspaceResponse,
    status_code=201,
)
def create_workspace(req: CreateWorkspaceRequest) -> CreateWorkspaceResponse:
    """Create a workspace. Atomic across filesystem + vault scaffolds for
    Project / BD; flat ``mkdir`` for General."""
    name = _validate_name(req.name)
    prof = profile_mod.load(VAULT)
    roots = _roots_for(req.type, prof)
    if not roots:
        raise HTTPException(
            500,
            f"no filesystem root configured for workspace type {req.type!r}; "
            f"check profile.md `external_{req.type}_path(s)`",
        )

    # Pick the target root. root_index only meaningful for project (the only
    # type with a plural-list config today) — but we accept it for any type
    # for forward-compat. Out-of-range → 422.
    root_index = req.root_index if req.root_index is not None else 0
    if root_index < 0 or root_index >= len(roots):
        raise HTTPException(
            422,
            f"root_index {root_index} out of range; "
            f"{len(roots)} root(s) configured for type {req.type!r}",
        )
    fs_root = roots[root_index]
    fs_final = fs_root / name

    # Vault scaffold target — Project + BD only.
    vault_final: Path | None = _vault_path_for(name) if req.type in ("project", "bd") else None

    # ── 409 conflict check, spanning EVERY configured root + vault tree ──
    conflicts: list[str] = []
    for r in roots:
        candidate = r / name
        if candidate.exists():
            conflicts.append(str(candidate))
    if vault_final is not None and vault_final.exists():
        conflicts.append(str(vault_final))
    if conflicts:
        raise HTTPException(
            409,
            {"error": "already exists", "existing_paths": conflicts},
        )

    # ── Make sure roots exist before staging ──
    if not fs_root.exists():
        fs_root.mkdir(parents=True, exist_ok=True)
    if vault_final is not None:
        vault_final.parent.mkdir(parents=True, exist_ok=True)

    # ── Stage ──
    stamp = uuid.uuid4().hex[:8]
    fs_staging = fs_root / f".{name}.tmp-{stamp}"
    vault_staging: Path | None = (
        vault_final.parent / f".{vault_final.name}.tmp-{stamp}"
        if vault_final is not None else None
    )

    run_id = audit.new_run_id()
    t0 = time.monotonic()

    # Track what's committed so rollback knows what to undo.
    vault_committed = False
    fs_committed = False

    try:
        # 1. Filesystem staging — copytree the M&A scaffold for Project/BD;
        #    flat mkdir for General.
        if req.type == "general":
            fs_staging.mkdir(parents=True, exist_ok=False)
        else:
            shutil.copytree(_fs_scaffold_template(), fs_staging)

        # 2. Vault staging — full _template/ copytree for Project + BD.
        if vault_staging is not None:
            shutil.copytree(_vault_scaffold_template(), vault_staging)

        # 3. Commit vault FIRST (smaller blast radius if step 4 fails).
        if vault_staging is not None and vault_final is not None:
            vault_staging.rename(vault_final)
            vault_committed = True
            vault_staging = None

        # 4. Commit filesystem.
        fs_staging.rename(fs_final)
        fs_committed = True
        fs_staging = None

        # 5. #6e step 2 — sync `workspace-type` + leading tag on the brief.
        #    Runs AFTER both renames committed so the file we touch is the
        #    final on-disk location. Failure inside the helper is logged but
        #    does not roll back the create (the workspace already exists +
        #    behaves correctly; field-injection is icing).
        if vault_final is not None and req.type in ("project", "bd"):
            brief_path = vault_final / "00 Brief.md"
            _sync_brief_frontmatter(brief_path, req.type)

    except Exception as e:  # noqa: BLE001 — we want every failure mode to roll back
        # Roll back any staging dirs.
        if fs_staging is not None and fs_staging.exists():
            shutil.rmtree(fs_staging, ignore_errors=True)
        if vault_staging is not None and vault_staging.exists():
            shutil.rmtree(vault_staging, ignore_errors=True)
        # If vault committed but fs didn't, undo the vault to preserve atomicity.
        if vault_committed and not fs_committed and vault_final is not None and vault_final.exists():
            shutil.rmtree(vault_final, ignore_errors=True)

        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="workspace",
            entity_id=name,
            action="create",
            routine="workspaces.create",
            run_id=run_id,
            status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"type": req.type, "name": name, "root_index": root_index},
            error=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        # Surface HTTPException unchanged so callers see 409 / 500 cleanly;
        # everything else becomes 500.
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(500, f"workspace create failed: {type(e).__name__}: {e}") from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="workspace",
        entity_id=name,
        action="create",
        routine="workspaces.create",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"type": req.type, "name": name, "root_index": root_index},
        outputs={
            "filesystem": str(fs_final),
            "vault": str(vault_final) if vault_final is not None else None,
            "source_root": str(fs_root),
        },
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

    return CreateWorkspaceResponse(
        workspace=CreatedWorkspace(
            type=req.type,
            name=name,
            paths=WorkspacePaths(
                filesystem=str(fs_final),
                vault=str(vault_final) if vault_final is not None else None,
                source_root=str(fs_root),
            ),
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /api/workspaces — list
# ────────────────────────────────────────────────────────────────────────────


def _iso_utc(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# Raw row before dedup — internal type
_RawRow = tuple[str, float, str, bool, bool]  # (name, mtime, root_str, in_vault, in_cf)


def _vault_project_roots() -> list[Path]:
    """Vault-side project roots — currently just ``<vault>/Projects``.

    #6c dual-scan: returns the vault Projects dir if it exists so we can
    surface legacy vault-only deals (DemoTarget) alongside the
    Corporate Finance ones. Returns an empty list if vault is unreachable
    — tests + bare-bones installs continue to work."""
    candidate = VAULT / "Projects"
    return [candidate] if candidate.exists() and candidate.is_dir() else []


def _scan_dir_for_workspaces(root: Path) -> list[tuple[str, float]]:
    """Walk ``root`` and yield ``(name, mtime)`` for each project-like dir.

    Skip rules match the rest of the workspaces logic:
      * hidden dirs (``.foo``)
      * underscore-prefixed sentinels (``_template``, ``_Trackers``)
      * non-directories
      * unreadable entries
    """
    out: list[tuple[str, float]] = []
    if not root.exists() or not root.is_dir():
        return out
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        try:
            mt = entry.stat().st_mtime
        except OSError:
            continue
        out.append((entry.name, mt))
    return out


def _read_workspace_type_from_brief(vault_workspace_dir: Path) -> WorkspaceType:
    """Read ``<vault_workspace_dir>/00 Brief.md`` frontmatter's
    ``workspace-type`` field. Defaults to ``project`` on any failure path
    (#6e step 3 backward-compat).

    Returns:
        ``project`` | ``bd`` — only the two values declared by the
        template. Any other / missing value collapses to ``project`` with
        a debug log so future audit can spot the drift.
    """
    brief = vault_workspace_dir / "00 Brief.md"
    if not brief.is_file():
        log.debug(
            "workspaces._read_workspace_type_from_brief: no 00 Brief.md at %s — defaulting to project",
            vault_workspace_dir,
        )
        return "project"
    try:
        post = frontmatter.load(brief)
    except Exception as e:  # noqa: BLE001
        log.debug(
            "workspaces._read_workspace_type_from_brief: failed to parse %s: %s — defaulting to project",
            brief, e,
        )
        return "project"
    raw = post.metadata.get("workspace-type")
    if raw == "bd":
        return "bd"
    if raw == "project":
        return "project"
    log.debug(
        "workspaces._read_workspace_type_from_brief: %s has workspace-type=%r — defaulting to project",
        brief, raw,
    )
    return "project"


FilterKind = Literal["both", "vault", "corporate_finance"]


def _list_projects_dual_scan(
    prof: profile_mod.OperatorProfile,
    *,
    filter_: FilterKind = "both",
    requested_type: WorkspaceType = "project",
) -> list[WorkspaceListItem]:
    """Walk BOTH the Corporate Finance roots (for ``requested_type``) AND
    the vault Projects tree. Dedupe by name, tag each item with which
    source(s) it came from.

    #6c rationale: pre-existing vault projects (DemoTarget) and
    Corporate Finance projects (DemoDeal-Test) coexist without 1:1
    parity. ``filter_`` lets a caller narrow to one source.

    #6e step 3 (scheduled 2026-05-26): for each vault entry, read
    ``workspace-type`` from ``00 Brief.md`` frontmatter. Default to
    ``project`` when absent (backward compat) — emits a debug log.
    Entries whose detected type does NOT match ``requested_type`` are
    filtered out of the result.

    Args:
      * prof — for the Corporate Finance roots
      * filter_ — ``both`` (default) / ``vault`` / ``corporate_finance``
      * requested_type — ``project`` (default) | ``bd``. Drives both
        which CF root to walk AND which vault entries pass the filter.

    Returns one ``WorkspaceListItem`` per distinct name. ``source_root``
    is the FIRST root that yielded the name; ``source_roots`` is the full
    list. ``last_touched`` is the MAX mtime across all sources.
    """
    cf_roots = _roots_for(requested_type, prof)
    vault_roots = _vault_project_roots()

    # Build per-source raw rows.
    cf_rows: list[tuple[str, float, Path]] = []
    if filter_ in ("both", "corporate_finance"):
        for root in cf_roots:
            for name, mt in _scan_dir_for_workspaces(root):
                cf_rows.append((name, mt, root))

    # Vault entries — each needs its workspace-type resolved from frontmatter.
    # Items whose detected type doesn't match `requested_type` are dropped.
    vault_rows: list[tuple[str, float, Path]] = []
    if filter_ in ("both", "vault"):
        for root in vault_roots:
            for name, mt in _scan_dir_for_workspaces(root):
                detected = _read_workspace_type_from_brief(root / name)
                if detected != requested_type:
                    continue
                vault_rows.append((name, mt, root))

    # Merge by name. For each name, compute:
    #   * in_corporate_finance / in_vault flags
    #   * source_roots list (CF first when present, then vault)
    #   * source_root (first of source_roots)
    #   * last_touched = MAX(mtime across sources)
    merged: dict[str, dict] = {}

    def _accumulate(name: str, mt: float, root: Path, source: str) -> None:
        bucket = merged.setdefault(name, {
            "mtime": mt,
            "roots_in_order": [],
            "in_vault": False,
            "in_corporate_finance": False,
        })
        bucket["mtime"] = max(bucket["mtime"], mt)
        root_str = str(root)
        if root_str not in bucket["roots_in_order"]:
            bucket["roots_in_order"].append(root_str)
        if source == "vault":
            bucket["in_vault"] = True
        elif source == "corporate_finance":
            bucket["in_corporate_finance"] = True

    # Order matters for ``source_root`` (first listed wins). CF first when
    # both present so the canonical surface is Corporate Finance; vault is
    # the fallback when CF doesn't have it yet.
    for name, mt, root in cf_rows:
        _accumulate(name, mt, root, "corporate_finance")
    for name, mt, root in vault_rows:
        _accumulate(name, mt, root, "vault")

    out: list[WorkspaceListItem] = []
    for name, bucket in merged.items():
        roots = bucket["roots_in_order"]
        out.append(WorkspaceListItem(
            type=requested_type,
            name=name,
            last_touched=_iso_utc(bucket["mtime"]),
            source_root=roots[0],
            in_vault=bucket["in_vault"],
            in_corporate_finance=bucket["in_corporate_finance"],
            source_roots=list(roots),
        ))
    return out


def _list_one_type(t: WorkspaceType, prof: profile_mod.OperatorProfile) -> list[WorkspaceListItem]:
    """Walk every configured root for type ``t`` and emit one item per dir.

    For ``project`` and ``bd``: dual-scan via ``_list_projects_dual_scan``
    (filter defaults to ``both``). #6e step 3: BD scanner also reads vault
    briefs' ``workspace-type`` field so BD-vault-only entries surface.

    For ``general``: single-source walk; legacy semantics (vault doesn't
    carry General workspaces by [[workspace-write-policy]] §2).

    Skip rules:
      * hidden dirs (name starts with ``.``)
      * underscore-prefixed sentinels (``_template``, ``_Trackers``)
      * non-directories
      * missing roots (no error — operator may have configured a path that
        doesn't exist yet)
    """
    if t in ("project", "bd"):
        return _list_projects_dual_scan(prof, filter_="both", requested_type=t)

    out: list[WorkspaceListItem] = []
    for root in _roots_for(t, prof):
        for name, mt in _scan_dir_for_workspaces(root):
            out.append(WorkspaceListItem(
                type=t,
                name=name,
                last_touched=_iso_utc(mt),
                source_root=str(root),
                in_vault=False,
                in_corporate_finance=False,
                source_roots=[str(root)],
            ))
    return out


@router.get("/workspaces", response_model=ListWorkspacesResponse)
def list_workspaces(
    type: WorkspaceType | None = Query(  # noqa: A002 — matches contract param name
        None,
        description="Filter by workspace type; omitted → returns all three types merged.",
    ),
    filter: FilterKind = Query(  # noqa: A002 — matches contract param name
        "both",
        description=(
            "For type=project only: which source(s) to scan. "
            "``both`` (default) merges vault + Corporate Finance; "
            "``vault`` returns vault Projects/* only; "
            "``corporate_finance`` returns Corporate Finance/1. Projects/* only. "
            "Ignored for type=bd / type=general."
        ),
    ),
) -> ListWorkspacesResponse:
    """List workspaces across every configured root. Sorted by
    ``last_touched`` descending so the dashboard sidebar lands on the most
    recently active deal first.

    #6c dual-scan: when ``type=project`` (or omitted), this endpoint scans
    BOTH the Corporate Finance ``1. Projects/`` roots AND the vault
    ``Projects/`` tree, deduplicating by name. Each item carries
    ``in_vault`` / ``in_corporate_finance`` flags so the dashboard can
    decide where to read overview / actions from.
    """
    prof = profile_mod.load(VAULT)
    types_to_list: list[WorkspaceType] = (
        [type] if type is not None else ["project", "bd", "general"]
    )

    items: list[WorkspaceListItem] = []
    for t in types_to_list:
        if t in ("project", "bd"):
            # #6e step 3 — BD also uses dual-scan so vault-only orphans
            # with ``workspace-type: bd`` in their brief frontmatter surface.
            items.extend(_list_projects_dual_scan(prof, filter_=filter, requested_type=t))
        else:
            items.extend(_list_one_type(t, prof))

    # Most recent first. Stable secondary sort by name for determinism when
    # mtimes collide (e.g. fresh test fixtures created in the same second).
    items.sort(key=lambda w: (w.last_touched, w.name), reverse=True)
    return ListWorkspacesResponse(workspaces=items)
