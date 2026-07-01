"""HiNotes → issue-candidate proposals (#issues-register v1.5).

After a transcript is processed AND routed to a project, any issue-shaped
statements the extractor surfaced (risks / blockers / items to monitor through
the deal — see ``extract.SCHEMA_HINT`` ``issues``) become operator-gated
``kind: issue-candidate`` proposals under ``Routines/issue-candidates/``.

This module NEVER writes the issues register itself. The proposal surfaces in
``GET /api/proposals/pending`` (approval tier); only on operator **Route** does
``routines.api.routes.proposals._route_issue_candidate`` append a ``## ISS-NN``
section to ``Projects/<deal>/14 Issues & Outstanding.md`` — append-only,
consistent with CLAUDE.md §3 rule 9 (never auto-write the vault).

Design mirrors the #76 capture loop (``routines/skills/_runtime/capture.py``):
best-effort from the caller's perspective (an emission miss must never fail the
HiNotes pipeline), idempotent against operator-triaged re-runs, one proposal
per candidate issue so the operator can cherry-pick.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter

from routines.hinotes.extract import Extraction

logger = logging.getLogger(__name__)

PROPOSAL_DIR_REL = "Routines/issue-candidates"

# Mirrors capture.SKIP_STATUSES — a proposal the operator already triaged must
# not be stomped by reprocessing the same transcript.
SKIP_STATUSES = {"applied", "rejected", "routed", "revision-requested"}

_MIN_TITLE_LEN = 5
_VALID_PRIORITIES = {"P1", "P2", "P3"}


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text).strip().lower())
    return s.strip("-") or "issue"


def proposal_path_for(
    vault_root: Path, *, project: str, title: str, now: datetime,
) -> Path:
    """``<vault>/Routines/issue-candidates/<date>-<project>-<title-slug>.md``."""
    filename = f"{now.date().isoformat()}-{_slug(project)}-{_slug(title)[:48]}.md"
    return vault_root / PROPOSAL_DIR_REL / filename


def _render_body(
    *, title: str, why: str, project: str, source_note_link: str, now: datetime,
) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    why_block = f"\n{why}\n" if why else ""
    return (
        f"# Issue candidate — {title}\n\n"
        f"## Why it matters\n{why_block}\n"
        f"Surfaced from {source_note_link}.\n\n"
        f"On **Route**, this candidate is appended as a `## ISS-NN` section to "
        f"`Projects/{project}/14 Issues & Outstanding.md` (status `open`; gating "
        f"items as `[issue:ISS-NN]`-tagged checkboxes) — append-only, never "
        f"overwriting prior issues (CLAUDE.md §3 rule 9).\n\n"
        f"*Emitted by the HiNotes issue-candidate loop (#issues-register v1.5) "
        f"on {ts}. Routes through the standard proposals lifecycle (#8 + #58).*\n"
    )


def emit_issue_candidates(
    extraction: Extraction,
    *,
    project_matched: str | None,
    source_note_link: str,
    vault_root: Path,
    now: Optional[datetime] = None,
) -> list[Path]:
    """Write one ``issue-candidate`` proposal per extracted issue.

    Returns the paths written. No-ops (``[]``) when the transcript didn't route
    to a project — an unrouted transcript has no register to target; the
    operator routes the note first via the ``hinotes-unrouted`` proposal, and
    issues ride the next processing pass or get raised manually.

    Pure I/O: writes proposal files only; never touches the register. Callers
    treat failures as non-fatal (the structured note already landed).
    """
    if not project_matched or not extraction.issues:
        return []
    now = now or datetime.now(timezone.utc)

    written: list[Path] = []
    for raw in extraction.issues:
        title = str(raw.get("title") or "").strip()
        if len(title) < _MIN_TITLE_LEN:
            continue
        why = str(raw.get("why") or "").strip()
        affects = str(raw.get("affects") or "").strip()
        priority = str(raw.get("suggested_priority") or "").strip().upper()
        priority = priority if priority in _VALID_PRIORITIES else ""
        gating_raw = raw.get("gating_items")
        gating = (
            [str(g).strip() for g in gating_raw if str(g).strip()]
            if isinstance(gating_raw, list) else []
        )

        path = proposal_path_for(vault_root, project=project_matched, title=title, now=now)
        if path.is_file():
            try:
                existing = frontmatter.load(path)
                status = str(existing.metadata.get("status") or "").strip().lower()
                if status in SKIP_STATUSES:
                    logger.info(
                        "issue-candidates: skipping %s — operator-triaged status=%r",
                        path, status,
                    )
                    continue
            except Exception as e:  # noqa: BLE001 — unreadable existing file → overwrite
                logger.warning("issue-candidates: failed to parse existing %s (%s) — overwriting", path, e)

        post = frontmatter.Post(_render_body(
            title=title, why=why, project=project_matched,
            source_note_link=source_note_link, now=now,
        ))
        post.metadata["type"] = "issue-candidate"
        post.metadata["kind"] = "issue-candidate"
        post.metadata["status"] = "pending-review"
        post.metadata["date"] = now.date().isoformat()
        post.metadata["project"] = project_matched
        post.metadata["target"] = f"Projects/{project_matched}/14 Issues & Outstanding.md"
        post.metadata["title"] = title
        if why:
            post.metadata["why"] = why
        if affects:
            post.metadata["affects"] = affects
        if priority:
            post.metadata["suggested_priority"] = priority
        post.metadata["gating"] = gating
        post.metadata["provenance"] = source_note_link
        post.metadata["sensitivity"] = extraction.sensitivity_classification
        post.metadata["tldr"] = title

        path.parent.mkdir(parents=True, exist_ok=True)
        serialised = frontmatter.dumps(post) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
        logger.info("issue-candidates: wrote %s", path)
        written.append(path)

    return written


__all__ = [
    "PROPOSAL_DIR_REL",
    "SKIP_STATUSES",
    "proposal_path_for",
    "emit_issue_candidates",
]
