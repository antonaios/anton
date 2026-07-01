"""Render a ``ParsedDocument`` to a vault markdown note.

Output lands in ``Inbox/Documents/<datestamp>-<slug>.md`` with
``status: needs-review`` so the operator triages it before any other
routine consumes the content.

Slug derivation prefers ``target_descriptor`` (anonymised codename)
over ``target_revealed_name`` to keep filenames safe for git status /
remote backup even if the doc reveals a sensitive target name.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

from routines.intake.schema import ParsedDocument
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)


# Strip everything that's not safe for a vault filename. Conservative —
# Obsidian renders most things but git status on Windows + WSL is fussier.
_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")


# F-25 (HR): every free-form field below is UNTRUSTED LLM-extracted PDF text
# (the PDF author controls it via content or embedded prompt injection).
# Interpolated raw, it can break out of its section — a line-leading ``---``
# forges a frontmatter/HR boundary, ``#`` injects headings, ``[[…]]`` plants
# wikilinks into the vault graph, and a ``` fence can open an executable
# Obsidian dataview block. The note is consumed downstream by Obsidian
# rendering, recall indexing, and later LLM context — so the fence happens
# HERE, at the single interpolation point.

_WIKILINK_OPEN = re.compile(r"\[\[")
# Structure-opening lines (≤3 leading spaces, per CommonMark): ATX headings,
# code fences, and ANY marker-only line — which covers thematic breaks in all
# their forms (---, ----, ***, ___, "- - -") AND setext heading underlines
# (===, --- under a text line). codex F-25 r1: the original ---/``` set
# missed setext + the * / _ / spaced variants.
_LINE_LEADING_STRUCTURE = re.compile(
    r"^(\s{0,3})(#{1,6}\s|```|~~~|(?=[=\-*_])[=\-*_ \t]+$)",
    re.MULTILINE,
)
# Wikilink neutralisation: a zero-width space (U+200B) between the brackets
# renders identically in Obsidian but no longer parses as a link.
_INERT_WIKILINK_OPEN = "[​["


def _fence_block(text: str) -> str:
    """Neutralise structure-breaking markdown in multi-line extracted text."""
    # Heading / thematic-break / code-fence at line start -> escaped.
    t = _LINE_LEADING_STRUCTURE.sub(lambda m: m.group(1) + "\\" + m.group(2), text)
    # Wikilinks -> visually identical, link-inert.
    t = _WIKILINK_OPEN.sub(_INERT_WIKILINK_OPEN, t)
    return t


def _fence_inline(text: str) -> str:
    """Single-line contexts (headings, list items, table cells): wikilink +
    pipe neutralisation plus newline flattening (a newline in a cell or
    heading breaks out of that structure entirely)."""
    t = " ".join(text.splitlines())
    t = _WIKILINK_OPEN.sub(_INERT_WIKILINK_OPEN, t)
    t = t.replace("|", "\\|")
    return t.strip()


def slug_from(descriptor: str, fallback: str = "untitled") -> str:
    """Filename-safe slug from a free-form descriptor."""
    s = (descriptor or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_CHARS.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60] or fallback


def intake_path(
    vault_root: Path,
    parsed: ParsedDocument,
    *,
    when: datetime | None = None,
) -> Path:
    """Compute the vault-relative path the intake note will land at."""
    when = when or datetime.now(timezone.utc)
    datestamp = when.date().isoformat()
    base = slug_from(parsed.target_descriptor or parsed.target_revealed_name)
    suffix_bits = [b for b in (parsed.doc_kind, base) if b]
    suffix = "-".join(suffix_bits) or "intake"
    return vault_root / "Inbox" / "Documents" / f"{datestamp}-{suffix}.md"


def write_intake_note(
    vault_root: Path,
    parsed: ParsedDocument,
    *,
    source_pdf: Path,
    run_id: str,
    when: datetime | None = None,
) -> Path:
    """Render ``parsed`` to a vault note and atomically write it."""
    when = when or datetime.now(timezone.utc)
    path = intake_path(vault_root, parsed, when=when)
    body = _render_markdown(parsed, source_pdf=source_pdf, run_id=run_id, when=when)
    atomic_write(path, body, vault_root=vault_root)
    return path


def _render_markdown(
    parsed: ParsedDocument,
    *,
    source_pdf: Path,
    run_id: str,
    when: datetime,
) -> str:
    fm = (
        "---\n"
        "type: intake-document\n"
        f"doc_kind: {parsed.doc_kind}\n"
        "sensitivity: confidential\n"
        "status: needs-review\n"
        "memory_kind: episodic\n"
        f"source_pdf: {source_pdf.name!r}\n"
        f"ingested: {when.isoformat(timespec='seconds')}\n"
        f"run_id: {run_id}\n"
        "tags: [intake, auto-generated]\n"
        "---\n\n"
    )

    # F-25: every parsed.* string below is fenced — block fields keep their
    # newlines with structure escaped; inline fields (headings, list items,
    # table cells, the blockquote) additionally flatten newlines + pipes.
    lines: list[str] = []
    title_bits = [
        _fence_inline(parsed.target_descriptor or parsed.target_revealed_name or "Untitled intake"),
        f"({parsed.doc_kind})",
    ]
    lines.append(f"# {' '.join(title_bits)}")
    lines.append("")
    lines.append(f"_Parsed from `{source_pdf.name}` · {when.date().isoformat()} · gemma4:e4b · review before use._")
    lines.append("")

    if parsed.summary:
        lines += ["## Summary", "", _fence_block(parsed.summary), ""]

    facts: list[str] = []
    if parsed.target_revealed_name:
        facts.append(f"- **Target (revealed):** {_fence_inline(parsed.target_revealed_name)}")
    if parsed.industry:
        facts.append(f"- **Industry:** {_fence_inline(parsed.industry)}")
    if parsed.sector:
        facts.append(f"- **Sector:** {_fence_inline(parsed.sector)}")
    if parsed.subsector:
        facts.append(f"- **Subsector:** {_fence_inline(parsed.subsector)}")
    if parsed.geography:
        facts.append(f"- **Geography:** {_fence_inline(parsed.geography)}")
    if parsed.advisor:
        facts.append(f"- **Advisor:** {_fence_inline(parsed.advisor)}")
    if facts:
        lines += ["## Identification", ""] + facts + [""]

    if parsed.financials:
        lines += ["## Financial highlights", "", "| Metric | Value | Period |", "|---|---|---|"]
        for f in parsed.financials:
            lines.append(
                f"| {_fence_inline(f.metric)} | {_fence_inline(f.value)} | {_fence_inline(f.period)} |"
            )
        lines.append("")

    if parsed.investment_highlights:
        lines += ["## Investment highlights", ""]
        for h in parsed.investment_highlights:
            lines.append(f"- {_fence_inline(h)}")
        lines.append("")

    if parsed.process_notes:
        lines += ["## Process / timing", "", _fence_block(parsed.process_notes), ""]

    if parsed.image_notes:
        lines += ["## Visuals noted", "", "| Page | Kind | Summary |", "|---|---|---|"]
        for img in parsed.image_notes:
            lines.append(f"| {img.page} | {_fence_inline(img.kind)} | {_fence_inline(img.summary)} |")
        lines.append("")

    if parsed.confidentiality:
        lines += ["## Confidentiality", "", f"> {_fence_inline(parsed.confidentiality)}", ""]

    lines += [
        "## Operator review",
        "",
        "- [ ] Sensitivity classification confirmed (default: confidential).",
        "- [ ] Linked to project (or moved to a project Inbox).",
        "- [ ] Any factual claim above spot-checked against the source PDF.",
        "",
    ]

    return fm + "\n".join(lines)
