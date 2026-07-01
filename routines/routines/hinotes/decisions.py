"""Promote decisions extracted from transcripts to vault registers.

Two destinations:
    1. Projects/<deal>/09 Decision Log.md  (per-project, if project matched)
    2. Registers/Decisions.md              (cross-project, always)

Idempotent: if an entry with the same source-note link + decision text
already exists, skip. Append-only — never edits or removes existing
entries (per CLAUDE.md §5 rule 9: safe deletion only, append-only registers).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from routines.shared.vault_writer import (
    VaultPaths,
    atomic_write,
    parse_note,
    serialise_note,
)

logger = logging.getLogger(__name__)


def promote_decisions(
    decisions: list[dict[str, Any]],
    *,
    paths: VaultPaths,
    project_matched: str | None,
    source_note_link: str,
    source_date: str | None,
) -> dict[str, list[Path]]:
    """Append decisions to per-project + cross-project decision logs.

    Returns dict with keys:
        - "project_log_appended": [Path] if any entries appended to per-project log
        - "cross_register_appended": [Path] if any entries appended to cross-cutting register
    """
    result: dict[str, list[Path]] = {
        "project_log_appended": [],
        "cross_register_appended": [],
    }
    if not decisions:
        return result

    # Per-project log
    if project_matched:
        project_log = paths.projects / project_matched / "09 Decision Log.md"
        if _append_to_log(
            project_log,
            decisions=decisions,
            source_note_link=source_note_link,
            source_date=source_date,
            project_link=f"[[Projects/{project_matched}]]",
            scope="project",
            vault_root=paths.root,
        ):
            result["project_log_appended"].append(project_log)

    # Cross-project register
    cross_register = paths.registers / "Decisions.md"
    if _append_to_log(
        cross_register,
        decisions=decisions,
        source_note_link=source_note_link,
        source_date=source_date,
        project_link=f"[[Projects/{project_matched}]]" if project_matched else "(no project)",
        scope="cross",
        vault_root=paths.root,
    ):
        result["cross_register_appended"].append(cross_register)

    return result


# ============================================================ internals


def _append_to_log(
    log_path: Path,
    *,
    decisions: list[dict[str, Any]],
    source_note_link: str,
    source_date: str | None,
    project_link: str,
    scope: str,
    vault_root: Path,
) -> bool:
    """Append decisions to log_path. Idempotent: skip entries already present
    (matched on source-note + decision text). Returns True if any appended."""
    today = source_date or date.today().isoformat()

    # Read existing log if it exists; create from blank if not
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
        parsed = parse_note(text)
        metadata = parsed.metadata
        body = parsed.body
    else:
        metadata = _new_log_frontmatter(scope, project_link)
        body = _new_log_body(scope)

    # Build new entries, skipping duplicates
    new_entries: list[str] = []
    for d in decisions:
        decision_text = str(d.get("decision", "")).strip()
        if not decision_text:
            continue
        # Idempotency check: if this exact source-note + decision-text combo
        # is already in the log, skip
        marker = f"{source_note_link}"
        if marker in body and decision_text in body:
            logger.debug("decision already in %s, skipping", log_path.name)
            continue

        owner = d.get("owner")
        d_date = d.get("date") or today
        owner_link = f"[[People/{owner}]]" if owner else "*(unattributed)*"

        entry = (
            f"\n## {d_date} — {decision_text[:80]}\n"
            f"- **Project:** {project_link}\n"
            f"- **Decision:** {decision_text}\n"
            f"- **Decided by:** {owner_link}\n"
            f"- **Source:** {source_note_link}\n"
        )
        new_entries.append(entry)

    if not new_entries:
        return False

    body = body.rstrip() + "\n" + "".join(new_entries)
    atomic_write(log_path, serialise_note(metadata, body), vault_root=vault_root)
    logger.info(
        "promoted %d decision(s) to %s",
        len(new_entries), log_path,
    )
    return True


def _new_log_frontmatter(scope: str, project_link: str) -> dict[str, Any]:
    if scope == "project":
        return {
            "type": "decision-log",
            "project": project_link,
            "sensitivity": "confidential",
            "tags": ["register", "decisions"],
        }
    return {
        "type": "register",
        "register": "decisions",
        "sensitivity": "internal",
        "tags": ["register", "decisions"],
    }


def _new_log_body(scope: str) -> str:
    if scope == "project":
        return (
            "# Decision Log\n\n"
            "> Append-only. Each decision: what, who decided, when, the alternatives "
            "that were considered and rejected, and the rationale. Do not edit prior entries.\n"
        )
    return (
        "# Decisions — Cross-Project Register\n\n"
        "> Append-only. Decisions promoted from per-project `09 Decision Log.md` files when they "
        "have implications beyond the originating deal.\n"
    )
