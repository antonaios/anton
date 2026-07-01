"""Vault-health skill bridge route (#21 — third SKILL.md migration).

``POST /api/workflows/vault-health`` — runs a freshness / links / speculation
sweep (or all three) and returns a structured :class:`VaultHealthResult`. The
handler:

  1. Reads the skill registry for governance metadata (sensitivity, scope,
     cost caps) — no inlined constants.
  2. Wraps the in-process sweep call in the real ``tool_call_hooks`` context
     manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope: any``,
     ``sensitivity: internal``) the guard is a structural NO-OP — nothing to
     refuse — but the wiring is the audit-recognition surface the operator
     can grep in ``runs/tool.vault-health.jsonl``.
  3. Calls the sweep functions directly (``freshness.scan`` / ``links.scan``
     / ``speculation.scan``) rather than spawning a CLI subprocess. The
     sweeps are deterministic file-walks that complete in seconds on a
     ~1k-note vault — sub-second to a few seconds — well under the 60s
     ceiling. Direct call avoids the subprocess overhead and gives the route
     structured access to the result + report path.
  4. Writes the report via the routine's own ``render_report`` + the shared
     ``atomic_write``, mirroring the CLI's behaviour. Same-day rerun
     overwrites in place (no archive).

The existing CLI + cron jobs (``vault-health-freshness`` Mon 08:00,
``vault-health-links`` Mon 08:30 — see ``routines/scheduler/jobs.py``) are
untouched and continue to fire the CLI directly; this route is the on-demand
operator surface (dashboard tile + Cmd-K).
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.shared.vault_writer import atomic_write
from routines.skills._runtime.anton_skill import anton_skill
from routines.vault_health import freshness, links, speculation

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ── request / response models ────────────────────────────────────────────────


SweepKind = Literal["freshness", "links", "speculation", "all"]


class VaultHealthRequest(BaseModel):
    """On-demand sweep request from the dashboard or Cmd-K.

    ``kind`` selects which sweep(s) to run; ``all`` runs freshness + links +
    speculation independently and returns the combined counts.

    F-18 (HR S-13): ``write`` defaults to FALSE on the HTTP surface. An empty-
    body request used to default to ``kind=all, write=True`` → a full-vault
    sweep that ALSO wrote reports on demand. The on-demand operator surface
    must be read-only unless the caller explicitly asks for a report
    (``write: true``); the scheduled cron jobs fire the CLI directly and keep
    writing as before (this default change is HTTP-surface-only)."""

    kind: SweepKind = "all"
    write: bool = False
    # workspace fields are conventional across all skill routes (#61) — for
    # this any-scope, internal skill they pass through the central guard
    # without effect, but the route accepts them so the dashboard caller is
    # symmetric with /api/workflows/lbo.
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class VaultHealthResult(BaseModel):
    """Structured sweep result. Mirrors the LBO/sector-news shape: status +
    output_path + counts + duration."""

    status: Literal["ok", "error"]
    kind: SweepKind
    output_path: Optional[str] = None  # vault-relative POSIX; None when no findings or kind=all
    output_paths: dict[str, str] = Field(default_factory=dict)  # populated for kind=all
    counts: dict[str, int] = Field(default_factory=dict)
    duration_ms: int = 0
    error: Optional[str] = None


# ── helpers — run one sweep, render + write report when findings exist ──────


def _run_freshness(vault: Path, write: bool) -> tuple[dict[str, int], Optional[str]]:
    """Returns (counts, vault-relative report path or None)."""
    today = date_cls.today()
    stale = freshness.scan(vault, today=today)
    counts = {
        "stale": len(stale),
        "auto_bump": sum(1 for s in stale if s.severity == "auto-bump"),
        "warning": sum(1 for s in stale if s.severity == "warning"),
    }
    rel_path: Optional[str] = None
    if write and stale:
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-freshness.md"
        atomic_write(report_path, freshness.render_report(stale, today=today), vault_root=vault)
        rel_path = str(report_path.relative_to(vault)).replace("\\", "/")
    return counts, rel_path


def _run_links(vault: Path, write: bool) -> tuple[dict[str, int], Optional[str]]:
    orphans = links.scan(vault)
    counts = {
        "orphans": len(orphans),
        "affected_files": len({o.source_path for o in orphans}),
    }
    rel_path: Optional[str] = None
    if write and orphans:
        today = date_cls.today()
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-orphan-links.md"
        atomic_write(report_path, links.render_report(orphans), vault_root=vault)
        rel_path = str(report_path.relative_to(vault)).replace("\\", "/")
    return counts, rel_path


def _run_speculation(vault: Path) -> tuple[dict[str, int], Optional[str]]:
    """Stub sweep — returns empty until #54a / `vault_health/speculation.py`
    full implementation lands. Surface the count (0) honestly; do NOT write a
    report for a stub."""
    markers = speculation.scan(vault)
    return {"markers": len(markers)}, None


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/vault-health", response_model=VaultHealthResult)
@anton_skill("vault-health")
def run_workflow_vault_health(req: VaultHealthRequest) -> VaultHealthResult:
    """Run a vault-health sweep on demand. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks``, lifecycle, dedup,
    ``SkillScopeRefused``→403). This body is just the sweep. Behaviour-identical."""
    import time
    t0 = time.monotonic()

    counts: dict[str, int] = {}
    output_path: Optional[str] = None
    output_paths: dict[str, str] = {}

    try:
        if req.kind == "freshness":
            counts, output_path = _run_freshness(VAULT, req.write)
        elif req.kind == "links":
            counts, output_path = _run_links(VAULT, req.write)
        elif req.kind == "speculation":
            counts, output_path = _run_speculation(VAULT)
        elif req.kind == "all":
            fresh_counts, fresh_path = _run_freshness(VAULT, req.write)
            links_counts, links_path = _run_links(VAULT, req.write)
            spec_counts, _ = _run_speculation(VAULT)
            counts = {
                "freshness_stale": fresh_counts["stale"],
                "freshness_auto_bump": fresh_counts["auto_bump"],
                "freshness_warning": fresh_counts["warning"],
                "links_orphans": links_counts["orphans"],
                "links_affected_files": links_counts["affected_files"],
                "speculation_markers": spec_counts["markers"],
            }
            if fresh_path:
                output_paths["freshness"] = fresh_path
            if links_path:
                output_paths["links"] = links_path
    except Exception as e:  # noqa: BLE001 — sweep errors map to 500
        # Iron Law: a sweep that errored is NOT a clean pass. Surface the
        # exception verbatim; do not paper over with partial counts.
        log.error("vault-health sweep %r failed: %s", req.kind, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"vault-health {req.kind} sweep failed: {e}")

    return VaultHealthResult(
        status="ok",
        kind=req.kind,
        output_path=output_path,
        output_paths=output_paths,
        counts=counts,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
