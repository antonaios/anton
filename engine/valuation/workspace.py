"""Workspace bootstrap + path resolution — three-tier workspace model (#18).

Supersedes the project-only ``project.py``. The operator works across three
workspace types, all rooted under ``<workspace-root>/`` (operator profile
``_claude/profile.md``):

    project → <workspace-root>/1. Projects             live deals; has a vault counterpart
    bd      → <workspace-root>/2. Business development  pitches; NO workspace vault
                                                            (BD watch-state lives on Companies/<X>.md frontmatter)
    general → <workspace-root>/3. General               ad-hoc scratch; NO vault

DECOUPLING (engine stays a pure calculator). The engine never reads the vault
or the operator profile. The CALLER supplies the filesystem root:

  * the bridge's workspace layer already resolves these roots from the profile
    (``routines/api/routes/workspaces.py::_roots_for``) and passes the resolved
    root to ``engine run ... --workspace-root <path>``;
  * the operator on the CLI may pass ``--workspace-root`` explicitly, or rely on
    the :data:`DEFAULT_ROOTS` below (kept in sync with the profile for ergonomic
    CLI use — the bridge path is authoritative).

This keeps a stable CLI contract (see ``docs/workspace-redesign-handoff.md`` §8
gotcha 3) and a single source of truth for roots (the profile, owned by the
bridge), with no engine→vault coupling.

Output-path conventions per type:

    project / bd : <root>/<name>/3. Financials & analysis/2. Valuation/
                   Project_<name>_<SKILL>_<YYYY-MM-DD>_vN.xlsx     (archive: 00. OLD/)

    general      : <root>/<name>/<SKILL>/
                   <name>_<SKILL>_<YYYY-MM-DD>_vN.xlsx             (archive: <SKILL>/00. OLD/)
                   — flat, no ``Project_`` prefix, no M&A folder hierarchy
                   (operator's words: "general>new>client 'ASOS'>LBO then ASOS
                   folder is created and within it LBO folder where the excel
                   output will be saved").

Vault counterpart: ONLY ``project`` has one (``<vault>/Projects/<name>/``).
``new_workspace`` for a project is atomic (file-system first; vault copy failure
rolls the file-system side back). ``bd``/``general`` are file-system only.

NOTE: the bridge's ``POST /api/workspaces`` (``create_workspace``) is the primary
workspace creator in the running platform; the engine's ``new_workspace`` here is
for CLI-only / no-bridge use and for the back-compat ``new-project`` path. Both
land the same folder shapes.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from valuation.exceptions import ProjectBootstrapError

# Re-export under a workspace-neutral name; ``project.py`` keeps the old name.
WorkspaceBootstrapError = ProjectBootstrapError

WorkspaceType = Literal["project", "bd", "general"]
WORKSPACE_TYPES: tuple[WorkspaceType, ...] = ("project", "bd", "general")


# --------------------------------------------------------------------- roots
# Defaults mirror the operator profile (_claude/profile.md, 2026-06-04). The
# bridge passes the resolved root explicitly; these are the CLI fallback so a
# bare ``engine run lbo --workspace project:DemoDeal`` still works locally.
DEFAULT_ROOTS: dict[WorkspaceType, Path] = {
    "project": Path(r"<workspace-root>/1. Projects"),
    "bd":      Path(r"<workspace-root>/2. Business development"),
    "general": Path(r"<workspace-root>/3. General"),
}

# Templates copied on bootstrap. project + bd share the M&A folder hierarchy;
# general has no template (flat). Only project gets a vault counterpart.
FS_TEMPLATE = Path(r"<fs-template>")
VAULT_PROJECTS_ROOT = Path(r"<vault>/Projects")
VAULT_TEMPLATE = VAULT_PROJECTS_ROOT / "_template"

# Repo-bundled portable default (ships with a clone). workspace.py is at
# ``<repo>/engine/valuation/workspace.py`` → parents[2] is the repo root.
_BUNDLED_FS_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "corporate-finance-deal-structure"


def _resolve_fs_template() -> Path:
    """Default M&A folder-structure template — first existing dir wins:
    ``AGENTIC_FS_TEMPLATE`` env → the operator's external template location
    (:data:`FS_TEMPLATE`) → the repo-bundled template that ships with a clone.
    Returns the bundled path as the last resort so a not-found error names a
    real candidate."""
    candidates: list[Path] = []
    env = os.environ.get("AGENTIC_FS_TEMPLATE")
    if env:
        candidates.append(Path(env))
    candidates.append(FS_TEMPLATE)
    candidates.append(_BUNDLED_FS_TEMPLATE)
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return _BUNDLED_FS_TEMPLATE

# Output sub-paths.
_VALUATION_SUBPATH = Path("3. Financials & analysis") / "2. Valuation"
_ARCHIVE_DIRNAME = "00. OLD"


def default_root(workspace_type: WorkspaceType) -> Path:
    """The profile-mirrored default filesystem root for a workspace type."""
    try:
        return DEFAULT_ROOTS[workspace_type]
    except KeyError:
        raise WorkspaceBootstrapError(
            f"Unknown workspace type {workspace_type!r}; expected one of {WORKSPACE_TYPES}"
        )


@dataclass(frozen=True)
class Workspace:
    """A resolved workspace: type + name + the filesystem root it lives under.

    ``fs_root`` is supplied by the caller (bridge → profile, or CLI default).
    ``vault_root`` is only meaningful for ``project`` (defaults to the canonical
    vault Projects root); it is ignored for ``bd``/``general``.
    """

    type: WorkspaceType
    name: str
    fs_root: Path
    vault_root: Optional[Path] = None

    # --------------------------------------------------------------- dirs
    @property
    def fs_dir(self) -> Path:
        return self.fs_root / self.name

    @property
    def vault_dir(self) -> Optional[Path]:
        """The vault counterpart — only projects have one."""
        if self.type != "project":
            return None
        root = self.vault_root if self.vault_root is not None else VAULT_PROJECTS_ROOT
        return root / self.name

    @property
    def has_vault(self) -> bool:
        return self.type == "project"

    @property
    def deal(self) -> str:
        """DEPRECATED back-compat alias for ``name`` — preserves the old
        project-only ``ProjectPaths.deal`` attribute for callers on the
        ``valuation.project`` shim during the compatibility window."""
        return self.name

    # ------------------------------------------------------------- exists
    @property
    def exists(self) -> bool:
        """Project: BOTH file-system and vault folders. bd/general: fs only."""
        if not self.fs_dir.exists():
            return False
        if self.has_vault:
            vd = self.vault_dir
            return vd is not None and vd.exists()
        return True

    @property
    def status(self) -> dict[str, bool]:
        s = {"fs": self.fs_dir.exists()}
        if self.has_vault:
            vd = self.vault_dir
            s["vault"] = vd is not None and vd.exists()
        return s

    # ------------------------------------------------------- output paths
    def output_dir(self, skill: str) -> Path:
        """Directory the engine writes a ``skill`` run into.

        project/bd → ``<name>/3. Financials & analysis/2. Valuation``
        general    → ``<name>/<SKILL>`` (flat)
        """
        if self.type == "general":
            return self.fs_dir / skill.upper()
        return self.fs_dir / _VALUATION_SUBPATH

    def archive_dir(self, skill: str) -> Path:
        """Where prior versions of ``skill`` move to (always ``00. OLD/`` under
        the skill's output dir — same convention for all three types)."""
        return self.output_dir(skill) / _ARCHIVE_DIRNAME

    def output_filename(self, skill: str, date_iso: str, version: int) -> str:
        """Filename for a run. project/bd carry the ``Project_`` prefix; general
        is flat (``<name>_<SKILL>_<date>_vN.xlsx``)."""
        skill_u = skill.upper()
        if self.type == "general":
            return f"{self.name}_{skill_u}_{date_iso}_v{version}.xlsx"
        return f"Project_{self.name}_{skill_u}_{date_iso}_v{version}.xlsx"

    def filename_prefix(self, skill: str) -> str:
        """The stable leading token used to match this workspace's runs of
        ``skill`` (everything before the ``<date>_vN.xlsx`` tail)."""
        skill_u = skill.upper()
        if self.type == "general":
            return f"{self.name}_{skill_u}_"
        return f"Project_{self.name}_{skill_u}_"


# ----------------------------------------------------------------- resolve

def parse_workspace_ref(ref: str) -> tuple[WorkspaceType, str]:
    """Parse a ``<type>:<name>`` CLI ref. ``name`` may contain colons (only the
    first ``:`` splits). Raises on an unknown / missing type."""
    if ":" not in ref:
        raise WorkspaceBootstrapError(
            f"Workspace ref {ref!r} must be '<type>:<name>' "
            f"(e.g. 'project:DemoDeal'); type is one of {WORKSPACE_TYPES}"
        )
    type_str, _, name = ref.partition(":")
    type_str = type_str.strip().lower()
    if type_str not in WORKSPACE_TYPES:
        raise WorkspaceBootstrapError(
            f"Unknown workspace type {type_str!r} in {ref!r}; expected {WORKSPACE_TYPES}"
        )
    return type_str, _sanitise(name)  # type: ignore[return-value]


def paths_for(workspace_type: WorkspaceType,
              name: str,
              *,
              fs_root: Optional[Path] = None,
              vault_root: Optional[Path] = None) -> Workspace:
    """Build a :class:`Workspace` without touching disk. ``fs_root`` defaults to
    the profile-mirrored root for the type."""
    if workspace_type not in WORKSPACE_TYPES:
        raise WorkspaceBootstrapError(
            f"Unknown workspace type {workspace_type!r}; expected {WORKSPACE_TYPES}"
        )
    name = _sanitise(name)
    root = fs_root if fs_root is not None else default_root(workspace_type)
    return Workspace(type=workspace_type, name=name, fs_root=root, vault_root=vault_root)


def exists(workspace_type: WorkspaceType, name: str, *,
           fs_root: Optional[Path] = None, vault_root: Optional[Path] = None) -> bool:
    return paths_for(workspace_type, name, fs_root=fs_root, vault_root=vault_root).exists


def status(workspace_type: WorkspaceType, name: str, *,
           fs_root: Optional[Path] = None, vault_root: Optional[Path] = None) -> dict[str, bool]:
    return paths_for(workspace_type, name, fs_root=fs_root, vault_root=vault_root).status


def list_workspaces(workspace_type: WorkspaceType, *,
                    fs_root: Optional[Path] = None) -> list[str]:
    """List existing workspace names under a type's root (immediate subdirs).
    Returns ``[]`` if the root doesn't exist yet."""
    root = fs_root if fs_root is not None else default_root(workspace_type)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# --------------------------------------------------------------- bootstrap

def new_workspace(workspace_type: WorkspaceType,
                  name: str,
                  *,
                  fs_root: Optional[Path] = None,
                  vault_root: Optional[Path] = None,
                  fs_template: Optional[Path] = None,
                  vault_template: Path = VAULT_TEMPLATE) -> Workspace:
    """Create a workspace's folder(s).

    * ``project`` — file-system (from the M&A template) + vault (from
      ``Projects/_template``), atomic: a vault-copy failure rolls back the
      file-system side.
    * ``bd`` — file-system only, from the same M&A template.
    * ``general`` — file-system only, a bare ``<root>/<name>/`` (no template).

    Raises :class:`WorkspaceBootstrapError` on any failure or pre-existing folder.
    """
    if fs_template is None:
        fs_template = _resolve_fs_template()
    ws = paths_for(workspace_type, name, fs_root=fs_root, vault_root=vault_root)

    # Pre-flight.
    if ws.fs_dir.exists():
        raise WorkspaceBootstrapError(f"File-system folder already exists: {ws.fs_dir}")
    if ws.has_vault and ws.vault_dir is not None and ws.vault_dir.exists():
        raise WorkspaceBootstrapError(f"Vault folder already exists: {ws.vault_dir}")

    uses_fs_template = workspace_type in ("project", "bd")
    if uses_fs_template and not fs_template.exists():
        raise WorkspaceBootstrapError(f"File-system template not found: {fs_template}")
    if ws.has_vault and not vault_template.exists():
        raise WorkspaceBootstrapError(f"Vault template not found: {vault_template}")

    # 1. File system.
    try:
        if uses_fs_template:
            shutil.copytree(fs_template, ws.fs_dir)
        else:  # general — bare folder, no hierarchy.
            ws.fs_dir.mkdir(parents=True)
    except Exception as e:  # noqa: BLE001
        raise WorkspaceBootstrapError(f"Failed to create file-system folder: {e}") from e

    # 2. Vault (project only) — with rollback of the file-system side on failure.
    if ws.has_vault:
        try:
            shutil.copytree(vault_template, ws.vault_dir)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            try:
                shutil.rmtree(ws.fs_dir)
            except Exception as cleanup_err:  # noqa: BLE001
                raise WorkspaceBootstrapError(
                    f"Vault copy failed AND file-system rollback failed.\n"
                    f"  vault error: {e}\n"
                    f"  cleanup error: {cleanup_err}\n"
                    f"  orphan folder: {ws.fs_dir}"
                ) from e
            raise WorkspaceBootstrapError(
                f"Failed to copy vault template; file-system rolled back: {e}"
            ) from e

    return ws


# ------------------------------------------------------------------ internals

_FORBIDDEN = set(r'/\<>:"|?*')


def _sanitise(name: str) -> str:
    """Light hygiene on a workspace name. We do NOT auto-lowercase or replace
    spaces — operator deal names mix conventions (DemoDeal, FALCON, "Acme Telco")."""
    name = (name or "").strip()
    if not name:
        raise WorkspaceBootstrapError("Workspace name cannot be empty")
    bad = [c for c in name if c in _FORBIDDEN or ord(c) < 32]
    if bad:
        raise WorkspaceBootstrapError(f"Workspace name contains forbidden characters: {bad!r}")
    if name in {".", ".."} or name.endswith((" ", ".")):
        raise WorkspaceBootstrapError(f"Workspace name not allowed: {name!r}")
    return name
