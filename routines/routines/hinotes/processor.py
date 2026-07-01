"""End-to-end processing of one transcript file.

Pipeline (per `routines/README.md`):
    1. Read raw transcript (TXT / DOCX / PDF / SRT / MD)
    2. Hash for idempotency
    3. Extract structured fields via local Ollama (qwen3:14b)
    4. Route structured note to Projects/<deal>/02 Meeting Notes/ or Inbox/Captures
    5. Auto-stub People / Companies on [[wikilink]] mentions
    6. Convert raw transcript to .md with frontmatter, move to Inbox/HiNotes/processed/
    7. Atomic writes throughout
    8. Audit log
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# ISO-date validator for inline [due:] tags (CLAUDE.md rule 11). Defensive
# guard against the extractor returning relative dates ("next week") that
# would corrupt the actions aggregator.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

from routines.hinotes.crossrefs import stub_companies, stub_people
from routines.hinotes.decisions import promote_decisions
from routines.hinotes.extract import Extraction, extract_from_transcript
from routines.hinotes.issue_candidates import emit_issue_candidates
from routines.shared import audit, profile as operator_profile
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import (
    VaultPaths,
    atomic_move,
    atomic_write,
    extract_wikilinks,
    serialise_note,
)

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    """Returned by process_one. Tells the caller what happened."""

    status: str                              # "ok" | "skipped" | "error"
    file_hash: str
    structured_note_path: Path | None = None
    transcript_md_path: Path | None = None
    people_stubbed: list[Path] = None         # type: ignore[assignment]
    companies_stubbed: list[Path] = None      # type: ignore[assignment]
    project_matched: str | None = None
    error: str | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.people_stubbed is None:
            self.people_stubbed = []
        if self.companies_stubbed is None:
            self.companies_stubbed = []


# ============================================================================ entrypoint


def process_one(
    transcript_path: Path,
    *,
    paths: VaultPaths,
    client: OllamaClient,
    audit_dir: Path,
    extract_model: str = "qwen3:14b",
) -> ProcessResult:
    """Process a single transcript file end-to-end."""
    started = time.monotonic()
    run_id = audit.new_run_id()
    file_hash = audit.hash_file(transcript_path)

    logger.info(
        "process_one start path=%s hash=%s run_id=%s",
        transcript_path.name, file_hash[:18], run_id,
    )

    try:
        # 1. Read transcript text
        transcript_text = _read_transcript(transcript_path)
        if not transcript_text.strip():
            raise ValueError(f"transcript empty after read: {transcript_path}")

        # 2. Idempotency check — if a transcript with this hash already lives
        #    in processed/, skip. Match on hash prefix in the filename.
        hash_short = file_hash.split(":")[1][:12]
        existing = list(paths.hinotes_processed.glob(f"{hash_short}-*.md"))
        if existing:
            logger.info("already processed (hash %s); skipping", hash_short)
            audit.write_structured(
                actor={"type": "system", "id": "routine:hinotes"},
                entity_type="vault_note",
                entity_id=str(transcript_path),
                action="process",
                routine="hinotes", run_id=run_id, status="skipped",
                audit_dir=audit_dir,
                inputs={"transcript_path": str(transcript_path), "file_hash": file_hash},
                outputs={"existing_processed": str(existing[0])},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return ProcessResult(status="skipped", file_hash=file_hash, duration_ms=0)

        # 3. Extract structured fields via Ollama
        extraction = extract_from_transcript(
            transcript_text, client=client, model=extract_model,
        )

        # 4. Route structured note → project or Captures
        target_dir, project_matched = _route_destination(extraction, paths)

        # 5. Build and write structured note
        meeting_md = _build_meeting_note(
            extraction=extraction,
            transcript_path=transcript_path,
            file_hash=file_hash,
            run_id=run_id,
            project_matched=project_matched,
        )
        note_filename = _structured_note_filename(extraction, transcript_path)
        note_path = target_dir / note_filename
        atomic_write(note_path, meeting_md, vault_root=paths.root)

        # 6. Auto-stub People / Companies cross-references
        source_note_link = _vault_link(note_path, paths)
        people_names = [p["name"] for p in extraction.people_mentions if p.get("name")]
        # Also include attendees as people
        attendee_names = [a["name"] for a in extraction.attendees if a.get("name")]
        all_people = list(dict.fromkeys(people_names + attendee_names))  # dedupe, preserve order
        # Plus any wikilinks the model put inline that didn't end up in the lists
        wikilinks_in_body = extract_wikilinks(meeting_md)
        people_dirs = _split_wikilinks_by_kind(wikilinks_in_body, "People", paths)
        company_dirs = _split_wikilinks_by_kind(wikilinks_in_body, "Companies", paths)

        people_to_stub = list(dict.fromkeys(all_people + people_dirs))
        companies_to_stub_dicts = list(extraction.company_mentions)
        companies_to_stub_dicts += [{"name": c, "context": ""} for c in company_dirs
                                     if c not in {d.get("name") for d in companies_to_stub_dicts}]

        # Load operator profile for self-exclusion (the operator shouldn't
        # be stubbed as a Person in their own vault when they show up as an
        # attendee in their own meetings).
        op = operator_profile.load(paths.root)
        operator_excludes = op.operator_name_variants()

        people_stubbed = stub_people(
            people_to_stub, paths=paths,
            source_note_link=source_note_link,
            source_date=extraction.meeting_date,
            exclude_names=operator_excludes,
        )
        companies_stubbed = stub_companies(
            companies_to_stub_dicts, paths=paths,
            source_note_link=source_note_link,
            source_date=extraction.meeting_date,
        )

        # 6.5. Promote decisions to per-project + cross-cutting register
        decisions_result = promote_decisions(
            extraction.decisions,
            paths=paths,
            project_matched=project_matched,
            source_note_link=source_note_link,
            source_date=extraction.meeting_date,
        )

        # 6.6. Emit operator-gated issue-candidate proposals (#issues-register
        # v1.5) for issue-shaped statements on project-routed transcripts.
        # BEST-EFFORT: an emission miss logs but never fails the pipeline —
        # the structured note already landed.
        issue_candidates: list[Path] = []
        try:
            issue_candidates = emit_issue_candidates(
                extraction,
                project_matched=project_matched,
                source_note_link=source_note_link,
                vault_root=paths.root,
            )
        except Exception:  # noqa: BLE001
            logger.exception("issue-candidate emission failed (best-effort; pipeline continues)")

        # 7. Convert raw transcript to .md with frontmatter; move to processed/
        transcript_md_path = _move_transcript_as_md(
            src=transcript_path,
            transcript_text=transcript_text,
            file_hash=file_hash,
            extraction=extraction,
            paths=paths,
            structured_note_link=source_note_link,
        )

        duration_ms = int((time.monotonic() - started) * 1000)

        audit.write_structured(
            actor={"type": "system", "id": "routine:hinotes"},
            entity_type="vault_note",
            entity_id=str(note_path),
            action="process",
            routine="hinotes", run_id=run_id, status="ok",
            audit_dir=audit_dir,
            semantic_target=(
                [str(p) for p in people_stubbed]
                + [str(c) for c in companies_stubbed]
            ) or None,
            inputs={
                "transcript_path": str(transcript_path),
                "file_hash": file_hash,
                "extract_model": extract_model,
            },
            outputs={
                "structured_note": str(note_path),
                "transcript_md": str(transcript_md_path),
                "project_matched": project_matched,
                "sensitivity": extraction.sensitivity_classification,
                "people_stubbed": [str(p) for p in people_stubbed],
                "companies_stubbed": [str(p) for p in companies_stubbed],
                "decisions_count": len(extraction.decisions),
                "decisions_promoted_to_project_log": [
                    str(p) for p in decisions_result["project_log_appended"]
                ],
                "decisions_promoted_to_cross_register": [
                    str(p) for p in decisions_result["cross_register_appended"]
                ],
                "actions_count": len(extraction.actions),
                "issue_candidates": [str(p) for p in issue_candidates],
            },
            duration_ms=duration_ms,
        )

        return ProcessResult(
            status="ok",
            file_hash=file_hash,
            structured_note_path=note_path,
            transcript_md_path=transcript_md_path,
            people_stubbed=people_stubbed,
            companies_stubbed=companies_stubbed,
            project_matched=project_matched,
            duration_ms=duration_ms,
        )

    except (OllamaError, ValueError, OSError) as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.exception("process_one failed for %s: %s", transcript_path, e)
        audit.write_structured(
            actor={"type": "system", "id": "routine:hinotes"},
            entity_type="vault_note",
            entity_id=str(transcript_path),
            action="process",
            routine="hinotes", run_id=run_id, status="error",
            audit_dir=audit_dir,
            inputs={"transcript_path": str(transcript_path), "file_hash": file_hash},
            duration_ms=duration_ms,
            error=str(e),
        )
        return ProcessResult(
            status="error", file_hash=file_hash, error=str(e), duration_ms=duration_ms,
        )


# ============================================================================ helpers


def _read_transcript(path: Path) -> str:
    """Read text from supported HiNotes export formats."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".srt"):
        return path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore[import-untyped]
        except ImportError as e:
            raise ValueError(f"python-docx required for .docx files: {e}") from e
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text)

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ValueError(f"pypdf required for .pdf files: {e}") from e
        reader = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)

    raise ValueError(f"unsupported transcript format: {suffix} ({path})")


def _route_destination(extraction: Extraction, paths: VaultPaths) -> tuple[Path, str | None]:
    """Pick where the structured note goes.

    Returns (directory, matched_project_name_or_None). If no project matches,
    routes to ``Inbox/hinotes-unrouted/`` so the proposals system can surface
    it for operator routing (see #8 — POST /api/proposals/{id}/route).
    """
    for candidate in extraction.project_mentions:
        matched = paths.match_project(candidate)
        if matched:
            return (paths.project_meeting_notes(matched), matched)
    # Also try company mentions as project candidates (some operators name
    # projects after the target company)
    for company_dict in extraction.company_mentions:
        candidate = company_dict.get("name", "")
        matched = paths.match_project(candidate)
        if matched:
            return (paths.project_meeting_notes(matched), matched)
    # No match — land in the unrouted inbox so the operator can route via
    # the dashboard's Inbox tab (proposal kind: hinotes-unrouted).
    return (paths.hinotes_unrouted, None)


def _build_meeting_note(
    *,
    extraction: Extraction,
    transcript_path: Path,
    file_hash: str,
    run_id: str,
    project_matched: str | None,
) -> str:
    """Produce the markdown content for the structured meeting note. Mirrors
    Templates/meeting-note.md schema."""
    metadata: dict[str, Any] = {
        "type": "meeting-note",
        "date": extraction.meeting_date or date.today().isoformat(),
        "duration": (
            f"{extraction.duration_minutes}m" if extraction.duration_minutes else None
        ),
        "attendees": [f"[[People/{a['name']}]]" for a in extraction.attendees if a.get("name")],
        "project": f"[[Projects/{project_matched}]]" if project_matched else None,
        "sensitivity": extraction.sensitivity_classification,
        "source-hash": file_hash.split(":")[1][:12],
        "source-file": transcript_path.name,
        "run-id": run_id,
        "tags": ["meeting", "hinotes-ingested"],
        "tldr": extraction.summary[:280] if extraction.summary else None,
    }

    # ----- body -----
    title = extraction.meeting_title or "Untitled meeting"
    parts: list[str] = [f"# {metadata['date']} — {title}", ""]

    if extraction.summary:
        parts += ["## Summary", extraction.summary, ""]

    if extraction.key_facts:
        parts += ["## Key facts"]
        for fact in extraction.key_facts:
            f = fact.get("fact", "").strip()
            t = fact.get("topic")
            line = f"- {f}"
            if t:
                line += f" *({t})*"
            parts.append(line)
        parts.append("")

    if extraction.decisions:
        parts += ["## Decisions"]
        for d in extraction.decisions:
            decision = d.get("decision", "").strip()
            owner = d.get("owner")
            d_date = d.get("date")
            line = f"- {decision}"
            extras = []
            if owner: extras.append(f"owner: {owner}")
            if d_date: extras.append(f"date: {d_date}")
            if extras:
                line += f" *({', '.join(extras)})*"
            parts.append(line)
        parts.append("")

    if extraction.actions:
        parts += ["## Actions"]
        for a in extraction.actions:
            action = a.get("action", "").strip()
            if not action:
                continue
            owner = a.get("owner")
            due = a.get("due")
            line = f"- [ ] {action}"
            # Inline-tag convention per CLAUDE.md rule 11 + workspace-write-policy §7.
            # Emit [due:YYYY-MM-DD] only when the value is ISO-shaped; the
            # extractor's prompt asks for ISO, but defensively drop "next week"
            # style relatives if they slip through.
            if due and _ISO_DATE_RE.match(str(due).strip()):
                line += f" [due:{str(due).strip()}]"
            if owner:
                # Slugify the owner name: lowercase + spaces→hyphens. Matches
                # the convention in profile.md operator_slug + downstream
                # aggregator (routines.projects.actions._normalize).
                slug = owner.strip().lower().replace(' ', '-')
                if slug:
                    line += f" [owner:{slug}]"
            parts.append(line)
        parts.append("")

    if extraction.open_questions:
        parts += ["## Open questions"]
        parts += [f"- {q}" for q in extraction.open_questions]
        parts.append("")

    parts += ["## Mentions"]
    if extraction.people_mentions or extraction.attendees:
        names = [a["name"] for a in extraction.attendees if a.get("name")]
        names += [p["name"] for p in extraction.people_mentions if p.get("name")]
        names = list(dict.fromkeys(names))
        parts.append("- **People:** " + ", ".join(f"[[People/{n}]]" for n in names))
    if extraction.company_mentions:
        cnames = [c["name"] for c in extraction.company_mentions if c.get("name")]
        parts.append("- **Companies:** " + ", ".join(f"[[Companies/{n}]]" for n in cnames))
    if extraction.sector_mentions:
        parts.append(
            "- **Sectors:** "
            + ", ".join(f"[[Sectors/{s}]]" for s in extraction.sector_mentions)
        )
    parts.append("")

    parts += ["## Source transcript",
              f"[[Inbox/HiNotes/processed/{file_hash.split(':')[1][:12]}-{transcript_path.stem}]]",
              ""]

    if extraction.sensitivity_rationale:
        parts += [
            "---",
            f"*Sensitivity classified as **{extraction.sensitivity_classification}** — "
            f"{extraction.sensitivity_rationale}*",
        ]

    body = "\n".join(parts)
    return serialise_note(metadata, body)


def _structured_note_filename(extraction: Extraction, transcript_path: Path) -> str:
    """Build a stable filename for the structured note: <date>-<title-slug>.md"""
    d = extraction.meeting_date or date.today().isoformat()
    slug = _slugify(extraction.meeting_title)[:60] or transcript_path.stem
    return f"{d}-{slug}.md"


def _slugify(s: str) -> str:
    """Turn 'DemoCo management call' into 'HB-Leisure-management-call'."""
    s = s.strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9\-]", "", s)
    return s.strip("-")


def _move_transcript_as_md(
    *,
    src: Path,
    transcript_text: str,
    file_hash: str,
    extraction: Extraction,
    paths: VaultPaths,
    structured_note_link: str,
) -> Path:
    """Convert the raw transcript to .md (with frontmatter), write to processed/,
    and DELETE the original from incoming/.

    The .md form is what makes Smart Connections index it.
    """
    hash_short = file_hash.split(":")[1][:12]
    target = paths.hinotes_processed / f"{hash_short}-{src.stem}.md"

    metadata: dict[str, Any] = {
        "type": "transcript-verbatim",
        "date": extraction.meeting_date or date.today().isoformat(),
        "title": extraction.meeting_title or src.stem,
        "sensitivity": extraction.sensitivity_classification,
        "source-hash": hash_short,
        "source-file": src.name,
        "structured-note": structured_note_link,
        "tags": ["transcript", "verbatim", "hinotes"],
    }

    body = (
        "# Verbatim transcript — " + (extraction.meeting_title or src.stem) + "\n\n"
        f"> Verbatim source for {structured_note_link}. "
        "Preserved here so semantic search can find off-hand mentions that the "
        "structured note didn't capture. Per CLAUDE.md §3.4: paraphrase in "
        "structured notes, verbatim stays here.\n\n"
        "## Transcript\n\n"
        f"```\n{transcript_text}\n```\n"
    )

    atomic_write(target, serialise_note(metadata, body), vault_root=paths.root)
    # Delete the original from incoming/
    src.unlink()
    logger.info("transcript moved as .md: %s", target)
    return target


def _vault_link(note_path: Path, paths: VaultPaths) -> str:
    """Convert an absolute path inside the vault to a `[[wikilink]]` form."""
    rel = note_path.relative_to(paths.root).with_suffix("")
    # Obsidian wikilinks use forward slashes
    return f"[[{rel.as_posix()}]]"


def _split_wikilinks_by_kind(
    wikilinks: list[str],
    kind_dir: str,
    paths: VaultPaths,
) -> list[str]:
    """From `extract_wikilinks` output, return just the basenames whose link
    starts with `kind_dir/` (e.g. 'People/' or 'Companies/').
    """
    prefix = f"{kind_dir}/"
    out = []
    for link in wikilinks:
        if link.startswith(prefix):
            out.append(link[len(prefix):])
    return out
