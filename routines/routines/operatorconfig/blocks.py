"""Surgical YAML-block replacement in markdown config files.

READ-side parsing lives in ``routines.shared.md_config.extract_section``;
this module is its WRITE-side mirror. It walks the exact same grammar
(``## <name>`` heading → first fenced YAML block before the next heading,
regexes imported from md_config so the two sides cannot drift) and
replaces ONLY the lines between the fences. Heading, prose, tables, other
sections, trailing content, and the original line-ending style are all
byte-preserved.
"""

from __future__ import annotations

from typing import Any

import yaml

from routines.shared.md_config import FENCE_CLOSE_RE, FENCE_OPEN_RE, HEADING_RE


class SectionBlockNotFound(ValueError):
    """The ``## <name>`` heading (or its fenced YAML block) is missing."""


def _line_ending(text: str) -> str:
    """Dominant line ending — used for the lines we insert."""
    return "\r\n" if "\r\n" in text else "\n"


def replace_yaml_block(text: str, name: str, new_body: str) -> str:
    """Return ``text`` with the fenced YAML block under ``## <name>``
    replaced by ``new_body``. Everything else is byte-preserved.

    Raises ``SectionBlockNotFound`` if the heading is absent, or another
    heading appears before the section's code block (same give-up rule
    as the read side).
    """
    eol = _line_ending(text)
    lines = text.splitlines(keepends=True)
    target_lower = name.lower()

    in_section = False
    open_idx: int | None = None

    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        heading_match = HEADING_RE.match(line)

        if heading_match and open_idx is None:
            heading_name = heading_match.group(1).strip().lower()
            if heading_name == target_lower:
                in_section = True
            elif in_section:
                raise SectionBlockNotFound(
                    f"section {name!r}: heading found but no fenced YAML "
                    f"block before the next heading"
                )
            continue

        if not in_section:
            continue

        if open_idx is None:
            if FENCE_OPEN_RE.match(line):
                open_idx = i
            continue

        if FENCE_CLOSE_RE.match(line):
            # Guard (codex pass-2 SEV-1): if the target block's own closing
            # fence was hand-deleted, the close we just found may belong to
            # a LATER section — replacing up to it would destroy unrelated
            # markdown. The consumed span must parse as a YAML list (or be
            # empty), same contract the read side enforces; merged prose /
            # headings / a swallowed section won't.
            old_body = "".join(lines[open_idx + 1 : i])
            try:
                parsed = yaml.safe_load(old_body) if old_body.strip() else None
            except yaml.YAMLError as e:
                raise SectionBlockNotFound(
                    f"section {name!r}: existing block does not parse as "
                    f"YAML ({e}) — refusing to replace"
                ) from e
            if parsed is not None and not isinstance(parsed, list):
                raise SectionBlockNotFound(
                    f"section {name!r}: existing block is not a YAML list "
                    f"(got {type(parsed).__name__}) — refusing to replace"
                )
            body_lines = [f"{b}{eol}" for b in new_body.splitlines()]
            return "".join(lines[: open_idx + 1] + body_lines + lines[i:])

        # An OPENING fence with a language token (```yaml) while already
        # inside a block means the target's closing fence is missing and
        # we've run into the next section's block — fail loud.
        if FENCE_OPEN_RE.match(line):
            raise SectionBlockNotFound(
                f"section {name!r}: block never closed before the next "
                f"fenced block — fix the closing ``` in Obsidian"
            )

    if open_idx is not None:
        raise SectionBlockNotFound(
            f"section {name!r}: fenced block never closed"
        )
    raise SectionBlockNotFound(f"section {name!r} not found")


# ── Serialisation ────────────────────────────────────────────────────────


def dump_flow_rows(rows: list[dict[str, Any]]) -> str:
    """Serialise list-of-dicts in the house flow style, one row per line::

        - {symbol: JDW.L, name: J D Wetherspoon}

    yaml.safe_dump handles quoting (apostrophes, ``&``, ``:`` in names).
    An empty list serialises as ``[]`` — an empty fence parses back as
    None and would fail round-trip verification (codex pass-2 SEV-2).
    """
    if not rows:
        return "[]"
    out: list[str] = []
    for row in rows:
        dumped = yaml.safe_dump(
            row,
            default_flow_style=True,
            sort_keys=False,
            allow_unicode=True,
            width=4096,
        ).strip()
        out.append(f"- {dumped}")
    return "\n".join(out)


def dump_block(value: Any) -> str:
    """Serialise in block style — used for the nested news-coverage rows."""
    return yaml.safe_dump(
        value,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=4096,
    ).strip()
