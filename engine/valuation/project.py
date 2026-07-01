"""DEPRECATED — project-only bootstrap. Superseded by ``workspace.py`` (#18).

Thin back-compat shim: every call delegates to the three-tier workspace model
with ``type="project"``. The engine CLI now uses ``valuation.workspace`` directly;
this module remains only for any external caller still importing
``valuation.project``. Remove after one release cycle.

NOTE: ``ProjectPaths`` is now an alias for :class:`valuation.workspace.Workspace`
(attribute ``.name`` replaces the old ``.deal``). The legacy ``*_ROOT`` constants
now point at the canonical ``<workspace-root>/`` roots (was the stale
``<workspace-root>/``).
"""
from __future__ import annotations

from pathlib import Path

from valuation import workspace as _ws
from valuation.exceptions import ProjectBootstrapError  # noqa: F401 — re-export
from valuation.workspace import Workspace as ProjectPaths  # back-compat alias

# Legacy constants kept for import compatibility — now the Corporate Finance roots.
FS_TEMPLATE = _ws.FS_TEMPLATE
FS_PROJECTS_ROOT = _ws.DEFAULT_ROOTS["project"]
VAULT_TEMPLATE = _ws.VAULT_TEMPLATE
VAULT_PROJECTS_ROOT = _ws.VAULT_PROJECTS_ROOT

__all__ = [
    "ProjectPaths", "ProjectBootstrapError",
    "paths_for", "exists", "status", "new_project",
    "FS_TEMPLATE", "FS_PROJECTS_ROOT", "VAULT_TEMPLATE", "VAULT_PROJECTS_ROOT",
]


def paths_for(deal: str, *,
              fs_root: Path = FS_PROJECTS_ROOT,
              vault_root: Path = VAULT_PROJECTS_ROOT) -> ProjectPaths:
    return _ws.paths_for("project", deal, fs_root=Path(fs_root), vault_root=Path(vault_root))


def exists(deal: str) -> bool:
    return paths_for(deal).exists


def status(deal: str) -> dict[str, bool]:
    return paths_for(deal).status


def new_project(deal: str, *,
                fs_template: Path = FS_TEMPLATE,
                vault_template: Path = VAULT_TEMPLATE,
                fs_root: Path = FS_PROJECTS_ROOT,
                vault_root: Path = VAULT_PROJECTS_ROOT) -> ProjectPaths:
    return _ws.new_workspace(
        "project", deal,
        fs_root=Path(fs_root), vault_root=Path(vault_root),
        fs_template=Path(fs_template), vault_template=Path(vault_template),
    )
