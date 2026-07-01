"""Auto-stub People/Companies cross-references on first mention.

Pattern: when a new structured meeting note mentions `[[Jane Doe]]` and
`People/Jane Doe.md` doesn't exist, create a stub from `Templates/person.md`.
Same for companies. Idempotent — re-running on the same note appends
mentions to existing stubs, doesn't overwrite.

Why this matters:
    - Per the plan §6 W3 D3, this is the magic moment where the second
      brain starts compounding. After a few weeks of meetings, every
      person you've talked to has a file with full context.
    - Without auto-stubs, every new wikilink is a broken link until the
      operator manually creates the file. High-friction; never happens.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from routines.shared.vault_writer import (
    ParsedNote,
    VaultPaths,
    atomic_write,
    parse_note,
    serialise_note,
)

logger = logging.getLogger(__name__)


PERSON_STUB_TEMPLATE = """\
{body_intro}

## Snapshot
{snapshot}

## Background
-

## History with us
{history_bullet}

## Mentions
*(populated automatically by the watcher)*

## Notes
-
"""


COMPANY_STUB_TEMPLATE = """\
{body_intro}

## Snapshot
{snapshot}

## Business
-

## Financials
- **Revenue:**
- **EBITDA / margin:**
- **KPIs:**

## Ownership and governance
-

## Transaction history
-

## Why we care
-

## Mentions
*(populated automatically by the watcher)*
"""


def stub_people(
    names: list[str],
    *,
    paths: VaultPaths,
    source_note_link: str,
    source_date: str | None,
    exclude_names: set[str] | None = None,
) -> list[Path]:
    """For each name, ensure `People/<name>.md` exists. Return list of paths
    actually created (not paths that already existed).

    Args:
        names: candidate names to stub
        paths: vault paths
        source_note_link: wikilink form of the source note
        source_date: ISO date for the mention bullet
        exclude_names: names to skip entirely. Typical use: filter the operator
            (the operator shouldn't be stubbed as a Person in their own vault
            when they appear as an attendee in their own meetings). Match is
            case-insensitive on the full name; provide variants if needed
            (e.g. {"Operator Name", "Operator"}).

    Idempotent: if a person file exists, append a mention line under "## Mentions"
    rather than creating a new file.
    """
    exclude_lower = {n.lower() for n in (exclude_names or set())}
    created: list[Path] = []
    for name in names:
        if not _is_valid_proper_noun(name):
            continue
        if name.lower() in exclude_lower:
            logger.debug("skipping excluded name: %s (operator or other excluded)", name)
            continue
        path = paths.people / f"{name}.md"
        if path.exists():
            _append_mention(path, source_note_link, source_date, vault_root=paths.root)
        else:
            atomic_write(path, _build_person_stub(name, source_note_link, source_date), vault_root=paths.root)
            created.append(path)
            logger.info("stubbed person: %s", path)
    return created


def stub_companies(
    companies: list[dict[str, str]] | list[str],
    *,
    paths: VaultPaths,
    source_note_link: str,
    source_date: str | None,
) -> list[Path]:
    """Same idea for companies. `companies` may be a list of names (strs) or
    a list of {name, context} dicts (from extract.py)."""
    created: list[Path] = []
    for entry in companies:
        if isinstance(entry, dict):
            name = entry.get("name", "").strip()
            context = entry.get("context", "").strip()
        else:
            name = str(entry).strip()
            context = ""
        if not _is_valid_proper_noun(name):
            continue
        path = paths.companies / f"{name}.md"
        if path.exists():
            _append_mention(path, source_note_link, source_date, vault_root=paths.root)
        else:
            atomic_write(path, _build_company_stub(name, context, source_note_link, source_date), vault_root=paths.root)
            created.append(path)
            logger.info("stubbed company: %s", path)
    return created


# ---------------------------------------------------------------- internals


def _is_valid_proper_noun(name: str) -> bool:
    """Filter out obviously-bad mentions: empty, too long, contains weird chars,
    looks like a sentence rather than a name."""
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if len(name) < 2 or len(name) > 80:
        return False
    # Must start with a capital letter (proper noun)
    if not name[0].isupper():
        return False
    # Reject if it has too many words (likely a sentence not a name)
    if len(name.split()) > 8:
        return False
    # Reject if it has characters that aren't safe in filenames or are unusual
    bad_chars = set('<>:"/\\|?*\n\r\t')
    if any(c in bad_chars for c in name):
        return False
    return True


def _build_person_stub(name: str, source_note_link: str, source_date: str | None) -> str:
    today = source_date or date.today().isoformat()
    metadata = {
        "type": "person",
        "name": name,
        "role": None,
        "firm": None,
        "sector": None,
        "location": None,
        "last-contact": today,
        "relationship-strength": "cold",
        "sensitivity": "internal",
        "tags": ["person", "stub"],
        "tldr": f"Stub created {today} by HiNotes watcher on first mention.",
    }
    body_intro = f"# {name}"
    snapshot = f"*(stub — populate as more context surfaces)*"
    history_bullet = f"- {today} — first mention → {source_note_link}"
    body = PERSON_STUB_TEMPLATE.format(
        body_intro=body_intro,
        snapshot=snapshot,
        history_bullet=history_bullet,
    )
    return serialise_note(metadata, body)


def _build_company_stub(
    name: str,
    context: str,
    source_note_link: str,
    source_date: str | None,
) -> str:
    today = source_date or date.today().isoformat()
    metadata = {
        "type": "company",
        "name": name,
        "status": "other",
        "sector": None,
        "hq": None,
        "ticker": None,
        "website": None,
        "sensitivity": "internal",
        "tags": ["company", "stub"],
        "tldr": (
            f"Stub created {today} by HiNotes watcher on first mention. "
            f"Context: {context}" if context else
            f"Stub created {today} by HiNotes watcher on first mention."
        ),
    }
    body_intro = f"# {name}"
    snapshot = context if context else "*(stub — populate as more context surfaces)*"
    body = COMPANY_STUB_TEMPLATE.format(
        body_intro=body_intro,
        snapshot=snapshot,
    )
    # Append the mention immediately
    body = body.rstrip() + f"\n\n- {today} — first mention → {source_note_link}\n"
    return serialise_note(metadata, body)


def _append_mention(path: Path, source_note_link: str, source_date: str | None, *, vault_root: Path) -> None:
    """Append a mention bullet under '## Mentions' in an existing People/Company
    note. If '## Mentions' is missing, append the section."""
    today = source_date or date.today().isoformat()
    bullet = f"- {today} — {source_note_link}"

    text = path.read_text(encoding="utf-8")
    parsed: ParsedNote = parse_note(text)
    body = parsed.body

    # If this exact bullet already exists, do nothing (idempotency)
    if bullet in body:
        return

    if "## Mentions" in body:
        # Insert bullet under the Mentions heading. Find the end of the
        # Mentions section (next heading or EOF) and append before it.
        lines = body.splitlines()
        in_section = False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if line.strip() == "## Mentions":
                in_section = True
                continue
            if in_section and line.startswith("## ") and line.strip() != "## Mentions":
                insert_at = i
                break
        # Find the last non-empty content line in the section to append after
        # Skip the "*(populated automatically...)*" placeholder if present
        actual_end = insert_at
        for j in range(insert_at - 1, -1, -1):
            if lines[j].strip() and not lines[j].strip().startswith("*("):
                actual_end = j + 1
                break
        lines.insert(actual_end, bullet)
        body = "\n".join(lines)
    else:
        # No Mentions section — append one
        body = body.rstrip() + f"\n\n## Mentions\n{bullet}\n"

    atomic_write(path, serialise_note(parsed.metadata, body), vault_root=vault_root)
    logger.debug("appended mention to %s", path)
