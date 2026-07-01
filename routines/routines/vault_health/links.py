"""Orphan wikilink scan.

Plan v3 §6.9 Phase 6. Walks every `.md` file in the vault, parses
`[[wikilinks]]`, and flags any whose resolved target doesn't exist.

Resolution follows Obsidian semantics (case-insensitive on Windows; file
basename match if no path is given; full path otherwise; anchor `#` and
alias `|` are stripped before resolution).

This is the most mechanical of the decay defences — no LLM, no domain
knowledge required. Deterministic; ~2s to scan ~1k file vault.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# Matches [[target]], [[target|alias]], [[target#anchor]], [[target/path]]
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+?)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")

# Inline-code spans — wikilinks INSIDE backticks are illustrative, not references.
# Markdown supports both `single` and ``double`` backtick spans; we strip both.
_INLINE_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")

# Subdirs to skip when SCANNING — code/output staging, not part of the wikilink graph
_SKIP_DIRS = {".obsidian", ".trash", ".recall-index", "_Trackers"}

# Subdirs whose orphans are EXPECTED — template files contain placeholder wikilinks
# by design (operator fills them in on instantiation). Skip these when reporting.
# Detected by path-component match.
_TEMPLATE_PATH_COMPONENTS = {"Templates", "_template"}

# Placeholder wikilink targets that are KNOWN to be intentional non-references:
# - Documentation examples (literal "wikilink" / "source:xyz" demonstrations)
# - Template empty placeholders (`[[Companies/]]` with trailing slash)
# - Generic angle-bracket placeholders (`[[Companies/<X>]]`)
# - "Source Register" / "file or URL" used as instruction text in templates
# Anything matching these patterns is filtered out before reporting.
_PLACEHOLDER_TARGETS = {
    "wikilink",
    "source:xyz",
    "source register",
    "file or url",
}

# Placeholder patterns — checked as regexes against the normalized target
_PLACEHOLDER_PATTERNS = [
    re.compile(r"^[a-z]+/$"),                    # bare-folder placeholder like "companies/"
    re.compile(r".*<[a-z]>$"),                    # placeholder with `<X>` suffix
    re.compile(r"^projects/?/.*"),                # legacy "Projects//06 Valuation" form
]


@dataclass
class OrphanLink:
    """One [[wikilink]] whose target doesn't exist."""
    source_path: str        # vault-relative POSIX, file containing the link
    target: str             # the unresolved wikilink target (as written)
    line_number: int


def scan(vault_root: Path, *, include_templates: bool = False) -> list[OrphanLink]:
    """Walk the vault and return all orphan wikilinks.

    Args:
        vault_root: vault path.
        include_templates: if True, include orphans inside Templates/ and
            _template/ directories. Default False — those orphans are
            intentional placeholders (operator fills them on instantiation).
    """
    # 1. Build the set of all existing .md files (basename + full path, lowercase)
    existing: set[str] = set()
    for f in vault_root.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        # Index by full relative path WITHOUT .md
        rel = f.relative_to(vault_root).with_suffix("")
        existing.add(str(rel).replace("\\", "/").lower())
        # Also index by basename (Obsidian allows bare-filename links)
        existing.add(f.stem.lower())

    # 2. Walk again and check each wikilink
    out: list[OrphanLink] = []
    for f in vault_root.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        if not include_templates and _is_template_file(f):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            log.warning("links: failed to read %s: %s", f, e)
            continue
        in_fenced_block = False
        for line_no, line in enumerate(content.splitlines(), start=1):
            # Skip fenced-code-block content entirely (```...```)
            if line.lstrip().startswith("```"):
                in_fenced_block = not in_fenced_block
                continue
            if in_fenced_block:
                continue
            # Strip inline-code spans before scanning for wikilinks
            stripped = _INLINE_CODE_RE.sub("", line)
            for m in _WIKILINK_RE.finditer(stripped):
                target = m.group(1).strip()
                if not target or target.startswith("http"):
                    continue
                # Normalize: strip .md, strip leading slash, lowercase
                norm = target.removesuffix(".md").lstrip("/").lower()
                if norm in existing:
                    continue
                # Try as bare basename match (last path segment)
                basename = norm.split("/")[-1]
                if basename in existing:
                    continue
                # Skip placeholder patterns (intentional non-references)
                if _is_placeholder_target(norm):
                    continue
                out.append(OrphanLink(
                    source_path=str(f.relative_to(vault_root)).replace("\\", "/"),
                    target=target,
                    line_number=line_no,
                ))

    log.info("links: %d orphan wikilinks across %d files",
             len(out), len({o.source_path for o in out}))
    return out


def _is_template_file(path: Path) -> bool:
    """True if the file lives inside Templates/ or any _template/ directory."""
    return any(part in _TEMPLATE_PATH_COMPONENTS for part in path.parts)


def _is_placeholder_target(norm_target: str) -> bool:
    """True if the (normalized) wikilink target is an intentional placeholder.

    Matched by either the explicit allowlist or one of the placeholder regexes.
    """
    if norm_target in _PLACEHOLDER_TARGETS:
        return True
    return any(p.match(norm_target) for p in _PLACEHOLDER_PATTERNS)


def render_report(orphans: list[OrphanLink]) -> str:
    """Markdown report of orphan wikilinks, grouped by source file."""
    if not orphans:
        return "# Orphan wikilink scan\n\n_No orphan wikilinks found._\n"

    # Group by source file
    by_source: dict[str, list[OrphanLink]] = {}
    for o in orphans:
        by_source.setdefault(o.source_path, []).append(o)

    out = [
        "---",
        "type: vault-health-report",
        "report_kind: orphan-links",
        "sensitivity: internal",
        f"orphan_count: {len(orphans)}",
        f"affected_files: {len(by_source)}",
        "status: pending-review",
        "tags: [vault-health, orphan-links, routines]",
        "---",
        "",
        "# Orphan wikilink scan",
        "",
        f"Found **{len(orphans)} orphan wikilinks** across "
        f"**{len(by_source)} files**. These `[[wikilinks]]` point to "
        "files that don't exist in the vault — either typos, renamed "
        "files, or stubs never written.",
        "",
    ]
    for source, items in sorted(by_source.items()):
        out += [
            f"## {source}",
            "",
        ]
        for it in sorted(items, key=lambda x: x.line_number):
            out.append(f"- L{it.line_number}: `[[{it.target}]]`")
        out.append("")

    out += [
        "## Operator action",
        "",
        "For each orphan: either (a) fix the link target (typo, renamed "
        "file), (b) create the missing file (especially if it's a stub "
        "for a known person/company), or (c) remove the link if the "
        "reference is no longer relevant.",
    ]
    return "\n".join(out)
