"""Vault walker + ``[[wikilink]]`` parser for the Stage-1 graph layer (#45).

Responsibilities (parsing only — graph construction lives in ``graph.py``):

  1. Walk every ``.md`` note under the vault root (skipping non-content dirs
     that mirror the recall indexer's ``SKIP_DIRS``).
  2. For each note, parse ``[[wikilink]]`` references from BOTH:
       - the note **body**, and
       - the **frontmatter values** (scalars AND list items).
  3. Classify each link by the *field context* it was found in — this becomes
     the graph **edge kind** (``frontmatter.<field>`` / ``body`` /
     ``mentions`` / ``sources``).
  4. Derive each note's **node kind** from its path prefix
     (``People/`` → Person, ``Companies/`` → Company, ``Sectors/`` → Sector,
     ``Projects/`` → Project, else Note).

Wikilink syntax handled (Obsidian):
  - plain            ``[[Companies/Foo]]``
  - aliased          ``[[Companies/Foo|Foo Ltd]]``       → target ``Companies/Foo``
  - heading anchor   ``[[Companies/Foo#Snapshot]]``      → target ``Companies/Foo``
  - block anchor     ``[[Companies/Foo#^abc123]]``       → target ``Companies/Foo``
  - combined         ``[[Companies/Foo#Snapshot|label]]``→ target ``Companies/Foo``
  - bare name        ``[[CLAUDE]]``                       → target ``CLAUDE``

Frontmatter values in this vault are quoted wikilinks, e.g.
``target: "[[Companies/DemoTelco Group plc]]"`` and may also be bare scalars
(``sectors: [telecoms]``) — only substrings that match the ``[[...]]`` pattern
become edges; bare scalars are ignored.

The link *target* is normalised (``.md`` suffix stripped, surrounding
whitespace trimmed) but otherwise preserved verbatim, including any path
prefix. Target resolution to an actual node (path-qualified vs bare-name) is
deferred to ``graph.py`` so this module stays a pure text parser.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import frontmatter

logger = logging.getLogger(__name__)


# Folders skipped during the walk. Mirrors recall's ``index.SKIP_DIRS`` so the
# graph and the embedding index agree on what "content" means. Templates and
# the index sidecars are structural, not relational content.
SKIP_DIRS = {
    ".git",
    ".obsidian",
    ".smart-env",
    ".recall-index",
    ".vault-graph",        # reserved for a future Stage-2 persisted graph
    "Templates",
    "Projects/_template",
    "Projects/_Trackers",
    "Inbox/HiNotes/incoming",
}

# Top-level meta-docs skipped (not relational content).
SKIP_FILES = {
    "README.md",
    "DEPLOYMENT.md",
}


# ── node kinds (derived from path prefix) ─────────────────────────────────────

PERSON = "Person"
COMPANY = "Company"
SECTOR = "Sector"
PROJECT = "Project"
NOTE = "Note"  # fallback for everything else (Daily/, Topics/, Registers/, …)

# Path-prefix → node kind. Order matters only for documentation; prefixes are
# mutually exclusive by construction.
_KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("People/", PERSON),
    ("Companies/", COMPANY),
    ("Sectors/", SECTOR),
    ("Projects/", PROJECT),
)


def node_kind_for_path(rel_path: str) -> str:
    """Return the node kind for a vault-relative path (POSIX-style).

    ``People/Jane Doe.md`` → ``Person``; ``Projects/X/00 Brief.md`` →
    ``Project``; anything outside the four typed prefixes → ``Note``.
    """
    p = rel_path.lstrip("./")
    for prefix, kind in _KIND_BY_PREFIX:
        if p.startswith(prefix):
            return kind
    return NOTE


# ── wikilink regex ────────────────────────────────────────────────────────────
#
# Matches ``[[target]]`` / ``[[target|alias]]`` / ``[[target#heading]]`` /
# ``[[target#heading|alias]]``. The target capture group is everything up to
# the first ``#`` (heading/block anchor) or ``|`` (alias) — whichever comes
# first — so anchors and aliases are stripped to leave the link destination.
# We deliberately do NOT match ``![[embed]]`` image/transclusion embeds as a
# distinct kind — an embed is still a structural reference, so the leading
# ``!`` is simply ignored and the link is captured like any other.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def _normalise_target(raw: str) -> str:
    """Strip alias / heading-anchor / ``.md`` suffix from a raw link inner.

    ``Companies/Foo#Snapshot|Foo Ltd`` → ``Companies/Foo``. Returns "" when
    the link resolves to nothing usable (e.g. a pure ``[[#heading]]``
    same-note anchor, which has no cross-note target)."""
    inner = raw.strip()
    # Drop alias (everything after the first '|').
    inner = inner.split("|", 1)[0]
    # Drop heading / block anchor (everything from the first '#').
    inner = inner.split("#", 1)[0]
    inner = inner.strip()
    # Strip a trailing .md if an operator wrote the explicit filename.
    if inner.endswith(".md"):
        inner = inner[:-3]
    return inner.strip()


def extract_wikilinks(text: str) -> list[str]:
    """Return all normalised wikilink targets in ``text``, in document order.

    Empty / anchor-only links are dropped. Duplicates are preserved (a note
    that links the same target twice yields two edges in a MultiDiGraph)."""
    if not text:
        return []
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = _normalise_target(m.group(1))
        if target:
            out.append(target)
    return out


# ── frontmatter wikilink extraction ───────────────────────────────────────────


def _iter_frontmatter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf inside a frontmatter value.

    Handles scalars, lists, and nested dicts/lists. Non-string leaves
    (ints, dates, bools, None) are skipped — only text can carry a
    ``[[wikilink]]``."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_frontmatter_strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _iter_frontmatter_strings(v)
    # other scalar types (int/float/bool/date) carry no wikilink → skip


def extract_frontmatter_links(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(field, target)`` pairs for every wikilink in frontmatter.

    ``field`` is the *top-level* frontmatter key the link was found under
    (e.g. ``target``, ``firm``, ``sector``) — used to build the edge kind
    ``frontmatter.<field>``. A field whose value is a list of wikilinks
    yields one pair per link, all sharing the same field name."""
    pairs: list[tuple[str, str]] = []
    for key, value in metadata.items():
        for s in _iter_frontmatter_strings(value):
            for target in extract_wikilinks(s):
                pairs.append((str(key), target))
    return pairs


# ── body section detection (edge-kind refinement) ─────────────────────────────
#
# Within the body, links found under a ``## Mentions`` heading get edge kind
# ``mentions`` and links under a ``## Sources`` (or ``## Source Register``)
# heading get edge kind ``sources`` — these are the operator's relational
# sections (CLAUDE.md §3). Everything else in the body is plain ``body``.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# Heading text (lower-cased, trimmed) → body edge kind. Matched on a prefix
# basis so "Sources" and "Source Register" both map to ``sources``.
_SECTION_KINDS: tuple[tuple[str, str], ...] = (
    ("mentions", "mentions"),
    ("sources", "sources"),
    ("source register", "sources"),
)


def _section_kind_for_heading(heading_text: str) -> str | None:
    """Map a heading's text to a body edge kind, or None for a plain section."""
    h = heading_text.strip().lower()
    for prefix, kind in _SECTION_KINDS:
        if h == prefix or h.startswith(prefix):
            return kind
    return None


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def extract_body_links(body: str) -> list[tuple[str, str]]:
    """Return ``(edge_kind, target)`` pairs for every wikilink in the body.

    Walks the body line by line, tracking the current section heading so a
    link's edge kind reflects whether it sits under a ``## Mentions`` /
    ``## Sources`` section (relational register) or anywhere else
    (``body``). Section state resets at every heading of level <= the
    section heading's level, so a link under a nested sub-heading of
    ``## Mentions`` is still ``mentions`` but a later ``## Style`` resets to
    ``body``.
    """
    if not body:
        return []
    pairs: list[tuple[str, str]] = []
    current_kind = "body"
    current_level = 0  # heading level that set current_kind (0 = top/body)
    fence_char = ""   # "" = not currently inside a fenced code block
    fence_len = 0
    for line in body.splitlines():
        # Fenced code blocks: wikilinks inside ``` / ~~~ fences are example
        # text, NOT edges. Track the OPENER's marker char + length so a
        # different-marker (or shorter) run INSIDE the fence can't falsely
        # close it — CommonMark close rule (Codex pass-2 SEV-2).
        fm = _FENCE_RE.match(line)
        if fence_char:
            # Close ONLY on the opener's marker char, length >= opener, AND
            # nothing but whitespace after the run — a closing fence carries no
            # info text, so ```python inside a ``` fence does NOT close it
            # (Codex pass-3 SEV-2).
            if (fm and fm.group(1)[0] == fence_char
                    and len(fm.group(1)) >= fence_len
                    and line[fm.end():].strip() == ""):
                fence_char, fence_len = "", 0
            continue
        if fm:
            fence_char = fm.group(1)[0]
            fence_len = len(fm.group(1))
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            section_kind = _section_kind_for_heading(m.group(2))
            if section_kind is not None:
                current_kind = section_kind
                current_level = level
            elif current_kind != "body" and level <= current_level:
                # A new heading at the same-or-shallower level closes the
                # relational section; fall back to plain body context.
                current_kind = "body"
                current_level = 0
            # (a deeper heading inside a Mentions/Sources section keeps the
            #  inherited kind — continue without resetting)
        # Strip inline-code spans (`...`) so a `[[link]]` rendered as code is
        # not treated as an edge (Codex SEV-2).
        cleaned = _INLINE_CODE_RE.sub(" ", line)
        for target in extract_wikilinks(cleaned):
            pairs.append((current_kind, target))
    return pairs


# ── data model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Edge:
    """A single directed wikilink edge, source → target.

    ``target`` is the raw normalised link text (may be path-qualified like
    ``Companies/Foo`` or a bare name like ``CLAUDE``); resolution to a real
    node id happens in ``graph.py``. ``kind`` is the edge context:
    ``frontmatter.<field>`` / ``body`` / ``mentions`` / ``sources``.
    """

    target: str
    kind: str


@dataclass
class ParsedNote:
    """One parsed note: its identity + all outgoing wikilink edges."""

    rel_path: str            # vault-relative POSIX path, WITHOUT .md suffix
    node_kind: str           # Person / Company / Sector / Project / Note
    title: str               # frontmatter title, else file stem
    edges: list[Edge] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedVault:
    """Result of walking + parsing the whole vault."""

    root: Path
    notes: list[ParsedNote] = field(default_factory=list)

    @property
    def note_count(self) -> int:
        return len(self.notes)


# ── walk ──────────────────────────────────────────────────────────────────────


def _walk_vault_notes(vault_root: Path) -> Iterator[Path]:
    """Yield all .md files under vault_root, skipping SKIP_DIRS / SKIP_FILES.

    Mirrors ``recall.index._walk_vault_notes`` so the graph and the recall
    index see the same content surface. Skip checks are done on the
    vault-relative POSIX path (string prefix) rather than calling
    ``Path.resolve()`` per file — ``resolve()`` is a per-file syscall that
    dominated the Stage-1 rebuild on Windows (the perf budget is < 2s for a
    1000-note vault, and the resolve()-per-file walk alone consumed a large
    fraction of that). String-prefix matching on the relative path is both
    faster and portable.
    """
    # Normalised skip-dir prefixes (POSIX, no leading/trailing slash).
    skip_dir_prefixes = tuple(d.strip("/") for d in SKIP_DIRS)
    for path in vault_root.rglob("*.md"):
        try:
            rel = path.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        # Skip if the file IS a skip-file, or lives under any skip-dir
        # prefix (``Templates/...`` / ``Projects/_template/...`` / …).
        if rel in SKIP_FILES:
            continue
        if any(
            rel == d or rel.startswith(d + "/")
            for d in skip_dir_prefixes
        ):
            continue
        yield path


def _rel_path_no_ext(note_path: Path, vault_root: Path) -> str:
    """Vault-relative POSIX path with the ``.md`` suffix removed."""
    rel = note_path.relative_to(vault_root).as_posix()
    if rel.endswith(".md"):
        rel = rel[:-3]
    return rel


def parse_note(note_path: Path, vault_root: Path) -> ParsedNote:
    """Parse a single note file into a ``ParsedNote`` (frontmatter + body links)."""
    rel_no_ext = _rel_path_no_ext(note_path, vault_root)
    rel_with_ext = note_path.relative_to(vault_root).as_posix()
    text = note_path.read_text(encoding="utf-8", errors="replace")
    post = frontmatter.loads(text)
    metadata = dict(post.metadata)
    body = post.content

    title = note_path.stem
    if metadata.get("title"):
        title = str(metadata["title"]).strip()

    edges: list[Edge] = []
    for fm_field, target in extract_frontmatter_links(metadata):
        edges.append(Edge(target=target, kind=f"frontmatter.{fm_field}"))
    for body_kind, target in extract_body_links(body):
        edges.append(Edge(target=target, kind=body_kind))

    return ParsedNote(
        rel_path=rel_no_ext,
        node_kind=node_kind_for_path(rel_with_ext),
        title=title,
        edges=edges,
        metadata=metadata,
    )


def parse_vault(vault_root: Path) -> ParsedVault:
    """Walk + parse the entire vault into a ``ParsedVault``.

    Per-note parse failures are logged and skipped (a single malformed
    note must never abort the whole graph build)."""
    vault_root = Path(vault_root)
    notes: list[ParsedNote] = []
    for note_path in _walk_vault_notes(vault_root):
        try:
            notes.append(parse_note(note_path, vault_root))
        except Exception as e:  # noqa: BLE001 — one bad note must not kill the build
            logger.warning("vault_graph: failed to parse %s: %s", note_path, e)
    return ParsedVault(root=vault_root, notes=notes)
