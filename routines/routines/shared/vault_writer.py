"""Safe writes to the vault.

Atomic file operations + frontmatter parsing + wikilink extraction. Every
routine that writes vault content goes through this module — never write
directly with Path.write_text().

Atomic write pattern:
    1. Write to <target>.tmp
    2. Rename <target>.tmp -> <target>
    3. Rename is atomic on POSIX; on Windows-via-WSL it's atomic enough for
       the granularity at which Obsidian / file watchers care.

Why this matters:
    - Obsidian watches files and re-indexes on change. A half-written note
      visible to Obsidian during a write produces a parse error and a noise
      notification.
    - Smart Connections may re-embed mid-write and cache a corrupt embedding.
    - Future routines may concurrently read the same path; atomic rename
      means they never see a partial file.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from routines.shared.write_policy import ensure_write_allowed

logger = logging.getLogger(__name__)


WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")


# --------------------------------------------------------------------- writes


def atomic_write(
    target: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    vault_root: Path | None = None,
) -> None:
    """Write `content` to `target` via tempfile + rename.

    The tempfile is created in the same directory so the rename is on the
    same filesystem (cross-fs renames aren't atomic).

    F-4 ``#sec-workspace-policy-chokepoint``: the target must satisfy the
    central write policy (``routines.shared.write_policy``) or the write is
    refused with :class:`~routines.shared.write_policy.WorkspacePolicyViolation`
    BEFORE any side effect. ``vault_root`` is the vault anchor the caller
    built the path from (server-config-derived — ``deps.VAULT`` /
    ``VaultPaths.root`` — never request input); pass it through so tmp-vault
    tests enforce the same sandbox shape as production.
    """
    ensure_write_allowed(target, vault_root=vault_root, op="atomic_write")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use NamedTemporaryFile in the parent dir so rename is intra-fs
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        suffix=".tmp",
        prefix=f".{target.name}.",
        dir=target.parent,
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(content)
        tf.flush()
    # Rename. On Windows this is atomic if target doesn't exist; if it does,
    # we need shutil.move which uses os.replace under the hood (atomic).
    shutil.move(str(tmp_path), str(target))
    logger.debug("atomic_write -> %s (%d chars)", target, len(content))


def atomic_move(src: Path, dst: Path, *, vault_root: Path | None = None) -> None:
    """Atomic-ish move, with parent creation.

    F-4: BOTH ends pass the central write policy — the destination is a
    write, and relocating the source destroys it in place (a move out of a
    protected file is as destructive as overwriting it). Fail-closed before
    any side effect."""
    ensure_write_allowed(src, vault_root=vault_root, op="atomic_move (source)")
    ensure_write_allowed(dst, vault_root=vault_root, op="atomic_move (destination)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    logger.debug("atomic_move %s -> %s", src, dst)


# ------------------------------------------------------------------- frontmatter


@dataclass
class ParsedNote:
    """A note split into frontmatter dict + body string."""

    metadata: dict[str, Any]
    body: str
    raw: str  # original full text


def parse_note(text: str) -> ParsedNote:
    """Parse a vault markdown file into frontmatter + body."""
    post = frontmatter.loads(text)
    return ParsedNote(metadata=dict(post.metadata), body=post.content, raw=text)


def serialise_note(metadata: dict[str, Any], body: str) -> str:
    """Inverse of parse_note. Produces canonical `--- yaml ---\\nbody` output.

    Uses pyyaml so quoting / escaping matches what Obsidian itself produces.
    """
    yaml_block = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{yaml_block}\n---\n\n{body.lstrip()}"


# ------------------------------------------------------------------- wikilinks


def extract_wikilinks(text: str) -> list[str]:
    """Return a list of `[[link]]` targets in order of appearance.

    Handles aliases (`[[Target|Alias]]`) and section refs (`[[Target#Section]]`)
    by returning just the Target. Deduplicates while preserving order.
    """
    seen: set[str] = set()
    results: list[str] = []
    for m in WIKILINK_PATTERN.finditer(text):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            results.append(target)
    return results


# --------------------------------------------------------------- vault paths


@dataclass(frozen=True)
class VaultPaths:
    """Canonical vault paths. One instance per process; pass into routines."""

    root: Path

    @property
    def claude(self) -> Path: return self.root / "_claude"
    @property
    def daily(self) -> Path: return self.root / "Daily"
    @property
    def projects(self) -> Path: return self.root / "Projects"
    @property
    def archive(self) -> Path: return self.root / "Archive"
    @property
    def people(self) -> Path: return self.root / "People"
    @property
    def companies(self) -> Path: return self.root / "Companies"
    @property
    def sectors(self) -> Path: return self.root / "Sectors"
    @property
    def topics(self) -> Path: return self.root / "Topics"
    @property
    def resources(self) -> Path: return self.root / "Resources"
    @property
    def inbox(self) -> Path: return self.root / "Inbox"
    @property
    def hinotes_incoming(self) -> Path: return self.root / "Inbox" / "HiNotes" / "incoming"
    @property
    def hinotes_processed(self) -> Path: return self.root / "Inbox" / "HiNotes" / "processed"
    @property
    def captures(self) -> Path: return self.root / "Inbox" / "Captures"
    @property
    def hinotes_unrouted(self) -> Path:
        """Landing zone for HiNotes transcripts when project inference fails.

        Surfaced as `kind: hinotes-unrouted` by GET /api/proposals/pending;
        the operator routes them to a workspace via POST
        /api/proposals/{id}/route. See [[workspace-write-policy]] + #8."""
        return self.root / "Inbox" / "hinotes-unrouted"
    @property
    def registers(self) -> Path: return self.root / "Registers"
    @property
    def templates(self) -> Path: return self.root / "Templates"
    @property
    def routines(self) -> Path: return self.root / "Routines"

    def list_projects(self) -> list[str]:
        """Return active project names (excludes _template, _Trackers)."""
        if not self.projects.exists():
            return []
        return sorted(
            p.name for p in self.projects.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        )

    def project_meeting_notes(self, project_name: str) -> Path:
        """Path to a project's meeting-notes folder."""
        return self.projects / project_name / "02 Meeting Notes"

    def match_project(self, candidate: str) -> str | None:
        """Best-effort match of a candidate project name to an existing project dir.

        Match logic:
        1. Exact match (case-sensitive)
        2. Case-insensitive exact match
        3. Normalised match (lowercase, separators normalised to '-') — handles
           "DemoCo" / "HB-Leisure" / "hb_leisure" all matching the same project
        4. Substring match on the normalised forms

        Returns the matched project name, or None.
        """
        if not candidate:
            return None
        projects = self.list_projects()

        # 1. Exact case-sensitive
        if candidate in projects:
            return candidate

        # 2. Case-insensitive exact
        cand_lower = candidate.lower()
        for p in projects:
            if p.lower() == cand_lower:
                return p

        # 3. Normalised exact (separator-agnostic)
        cand_norm = _normalise_for_match(candidate)
        for p in projects:
            if _normalise_for_match(p) == cand_norm:
                return p

        # 4. Normalised substring
        for p in projects:
            p_norm = _normalise_for_match(p)
            if cand_norm in p_norm or p_norm in cand_norm:
                return p

        return None


def _normalise_for_match(s: str) -> str:
    """Normalise a string for fuzzy project matching: lowercase, collapse all
    common separators (space, dash, underscore, dot) to a single dash."""
    s = s.lower().strip()
    # Collapse runs of separator chars to a single dash
    s = re.sub(r"[\s_.\-]+", "-", s)
    return s.strip("-")
