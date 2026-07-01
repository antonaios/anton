"""Crew audit mirror — parent + per-role rows (#31).

Canonical audit lives on the ANTON side; MetaGPT's ``env.history`` is
in-process to the crew subprocess and dies with it, so the crew serializes a
``roles_log`` into its final result line and the bridge mirrors it here
(METAGPT-INTEGRATION-SPEC.md §4).

File shapes (same per-routine JSONL pattern as skills/tools/composites):

  * parent row  → ``runs/crew.<verb>.jsonl``
  * role rows   → ``runs/crew.<verb>.roles.jsonl`` (one per role, keyed by
    the parent ``run_id``)

ADAPTED from the staged spec: the sketch called the legacy ``audit.write()``;
the current substrate's canonical API is ``audit.write_structured(routine=…,
audit_dir=…)`` which co-writes the same per-routine JSONL line (preserving
every glob-consumer of ``runs/*.jsonl``) AND the structured activity stream /
SQLite index — with the proper ``entity_type="crew_run"`` (already in the #60
closed enum) instead of the legacy bridge's generic ``"session"``.

No reconciler, by design: rows are written synchronously after the result
line is parsed, so there is no checkpoint to drift from (spec §4.3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from routines.shared import audit

logger = logging.getLogger(__name__)


def write_parent_started(
    *,
    verb: str,
    run_id: str,
    audit_dir: Path,
    workspace_type: str,
    workspace_name: str,
    sensitivity: str,
    lane: str,
    args: dict[str, Any],
    cost_cap_tokens: int,
    session_id: str | None = None,
) -> None:
    """Parent row in ``started`` state — written before the subprocess
    launches so a bridge crash mid-crew still leaves a trace."""
    audit.write_structured(
        actor={"type": "agent", "id": f"crew.{verb}"},
        entity_type="crew_run",
        entity_id=run_id,
        action="start",
        routine=f"crew.{verb}",
        run_id=run_id,
        status="started",
        audit_dir=audit_dir,
        inputs={
            "verb": verb,
            "workspace_type": workspace_type,
            "workspace_name": workspace_name,
            "sensitivity": sensitivity,
            "lane": lane,
            "session_id": session_id,
            # Redacted arg SURFACE, never the payload (codex-5.5 xhigh,
            # 2026-06-10): future crews (#32 /triage) take document text /
            # MNPI content in args — the audit row must record shape, not
            # the very content the sensitivity tiers protect. Mirrors the
            # no-args rule the refusal rows already follow.
            "args_keys": sorted(args.keys()),
            "args_chars": sum(len(str(v)) for v in args.values()),
            "cost_cap_tokens": cost_cap_tokens,
        },
    )


def write_parent_completion(
    *,
    verb: str,
    run_id: str,
    audit_dir: Path,
    status: str,
    workspace_type: str,
    workspace_name: str,
    sensitivity: str,
    duration_ms: int | None,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    """Parent completion row (``ok`` / ``error`` / ``cancelled`` / ``timeout``)."""
    outputs: dict[str, Any] | None = None
    if result is not None:
        outputs = {
            "summary": result.get("summary"),
            "token_count": result.get("token_count", 0),
            "roles_count": len(result.get("roles_log") or []),
            "artefacts": result.get("artefacts", []),
        }
    audit.write_structured(
        actor={"type": "agent", "id": f"crew.{verb}"},
        entity_type="crew_run",
        entity_id=run_id,
        action="complete",
        routine=f"crew.{verb}",
        run_id=run_id,
        status=status,
        audit_dir=audit_dir,
        inputs={
            "verb": verb,
            "workspace_type": workspace_type,
            "workspace_name": workspace_name,
            "sensitivity": sensitivity,
        },
        outputs=outputs,
        duration_ms=duration_ms,
        error=error,
    )


def write_role_rows(
    *,
    verb: str,
    run_id: str,
    audit_dir: Path,
    roles_log: list[dict[str, Any]],
) -> None:
    """One child row per role, mirrored from the crew's serialized
    ``env.history`` walk. Malformed entries are logged + skipped — the audit
    mirror must never lose the parent row over one bad role entry."""
    for entry in roles_log:
        if not isinstance(entry, dict):
            logger.warning("crew %s/%s: non-dict roles_log entry skipped", verb, run_id)
            continue
        audit.write_structured(
            actor={"type": "agent", "id": f"crew.{verb}"},
            entity_type="crew_run",
            entity_id=run_id,
            action="role_complete",
            routine=f"crew.{verb}.roles",
            run_id=run_id,
            status=str(entry.get("status", "ok")),
            audit_dir=audit_dir,
            inputs={
                "parent_run_id": run_id,
                "role": entry.get("role"),
                "action": entry.get("action"),
                "sensitivity": entry.get("sensitivity"),
                # The role's OWN start time (stamped inside the crew) — the
                # row's ts is the audit WRITE time, which post-dates it by
                # the whole crew run (codex-5.5 SEV-3).
                "ts_start": entry.get("ts_start"),
            },
            outputs={
                "output_summary": entry.get("output_summary"),
                "token_count": entry.get("token_count", 0),
            },
            duration_ms=int(entry["duration_ms"]) if str(entry.get("duration_ms", "")).lstrip("-").isdigit() else None,
        )


def write_refusal(
    *,
    verb: str,
    run_id: str,
    audit_dir: Path,
    workspace_type: str,
    workspace_name: str,
    reason: str,
) -> None:
    """A pre-launch sensitivity refusal — the platform's most safety-critical
    crew gate leaves an explicit trail (mirrors the #anton-skill-refusal-audit
    pattern). Never logs the request args — they may carry the very content
    the gate refused."""
    audit.write_structured(
        actor={"type": "agent", "id": f"crew.{verb}"},
        entity_type="crew_run",
        entity_id=run_id,
        action="refuse",
        routine=f"crew.{verb}",
        run_id=run_id,
        status="refused",
        audit_dir=audit_dir,
        inputs={
            "verb": verb,
            "workspace_type": workspace_type,
            "workspace_name": workspace_name,
        },
        error=reason,
    )


__all__ = [
    "write_parent_started",
    "write_parent_completion",
    "write_role_rows",
    "write_refusal",
]
