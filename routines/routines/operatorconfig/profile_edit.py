"""Comment-preserving edits to ``profile.md`` frontmatter.

profile.md's frontmatter is heavily annotated with operator comments —
a naive parse→mutate→dump (python-frontmatter / PyYAML) would destroy
every one of them. These helpers do TARGETED LINE SURGERY instead: only
the line(s) carrying the edited value change; comments, key order, and
all other keys are byte-preserved.

Fail-loud contract: if the frontmatter doesn't have the shape these
narrow editors expect (key missing, a list block carrying structural
comment lines whose anchoring can't survive a reorder), raise
``ProfileEditError`` — that case is edited in Obsidian, never mangled
here. The store verifies every transform by re-parsing before anything
is written to disk.
"""

from __future__ import annotations

import re
from typing import Any

import yaml


class ProfileEditError(ValueError):
    """The frontmatter doesn't have the shape this editor can safely change."""


_ITEM_RE = re.compile(r"^(?P<indent>\s+)-\s+(?P<rest>.*)$")
_TOP_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\s*:")


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _frontmatter_span(lines: list[str]) -> tuple[int, int]:
    """Return ``(start, end)`` line indices of the frontmatter body —
    exclusive of both ``---`` fences. Raises if there is no frontmatter.
    """
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ProfileEditError("file has no YAML frontmatter")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return 1, i
    raise ProfileEditError("frontmatter never closed")


def _yaml_scalar(value: Any) -> str:
    """Serialise a scalar (or flow list) the way YAML wants it quoted.

    PyYAML emits a ``...`` document-end line after a top-level scalar —
    trim it (it's a document marker, not part of the value; values with
    embedded newlines dump quoted-and-escaped on a single line, so only
    the marker line can appear here).
    """
    if isinstance(value, list):
        return yaml.safe_dump(
            value, default_flow_style=True, sort_keys=False,
            allow_unicode=True, width=4096,
        ).strip()
    lines = yaml.safe_dump(value, allow_unicode=True, width=4096).splitlines()
    if lines and lines[-1].strip() == "...":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _split_value_comment(after_colon: str) -> tuple[str, str]:
    """Split ``after_colon`` into (value, trailing_comment). The comment
    starts at a ``#`` that is outside quotes AND outside flow collections
    (``[...]`` / ``{...}`` — a ``#`` inside one is value material, e.g.
    ``["C#"]``), preceded by whitespace (or starting the value). Returns
    the comment INCLUDING its ``#``.
    """
    quote: str | None = None
    depth = 0
    for i, ch in enumerate(after_colon):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in "[{":
            depth += 1
            continue
        if ch in "]}":
            depth = max(0, depth - 1)
            continue
        if ch == "#" and depth == 0 and (i == 0 or after_colon[i - 1] in " \t"):
            return after_colon[:i].rstrip(), after_colon[i:].rstrip("\r\n")
    return after_colon.rstrip("\r\n").rstrip(), ""


def set_scalar(
    text: str, key: str, value: Any, *, parent: str | None = None,
) -> str:
    """Replace the value of ``key:`` (top-level, or nested one level under
    ``parent:``) inside the frontmatter. The line's trailing comment is
    preserved; nothing else in the file changes.
    """
    eol = _eol(text)
    lines = text.splitlines(keepends=True)
    start, end = _frontmatter_span(lines)

    if parent is None:
        search_from, search_to = start, end
        key_re = re.compile(rf"^(?P<prefix>{re.escape(key)}\s*:\s?)(?P<rest>.*)$")
    else:
        parent_idx = None
        parent_re = re.compile(rf"^{re.escape(parent)}\s*:\s*$")
        for i in range(start, end):
            if parent_re.match(lines[i].rstrip("\r\n")):
                parent_idx = i
                break
        if parent_idx is None:
            raise ProfileEditError(f"frontmatter key {parent!r} not found")
        # The nested block = consecutive indented lines after the parent.
        search_from = parent_idx + 1
        search_to = search_from
        while search_to < end and (
            lines[search_to].startswith((" ", "\t"))
            or not lines[search_to].strip()
        ):
            search_to += 1
        # DIRECT children only (codex SEV-2): a deeper descendant named
        # ``title`` must never be matched. The direct-child indent is the
        # indent of the first non-blank line after the parent.
        child_indent: str | None = None
        for j in range(search_from, search_to):
            stripped = lines[j].rstrip("\r\n")
            if stripped.strip():
                child_indent = stripped[: len(stripped) - len(stripped.lstrip())]
                break
        if child_indent is None:
            raise ProfileEditError(f"frontmatter key {parent!r} has no children")
        key_re = re.compile(
            rf"^(?P<prefix>{re.escape(child_indent)}{re.escape(key)}\s*:\s?)(?P<rest>.*)$"
        )

    for i in range(search_from, search_to):
        m = key_re.match(lines[i].rstrip("\r\n"))
        if not m:
            continue
        _, comment = _split_value_comment(m.group("rest"))
        new_line = f"{m.group('prefix')}{_yaml_scalar(value)}"
        if comment:
            new_line += f"  {comment}"
        lines[i] = f"{new_line}{eol}"
        return "".join(lines)

    where = f"{parent}.{key}" if parent else key
    raise ProfileEditError(f"frontmatter key {where!r} not found")


def set_string_list_block(text: str, key: str, items: list[str]) -> str:
    """Replace the items of a top-level block list (``key:`` followed by
    ``  - item`` lines) with ``items``, in order.

    Comment preservation: an item that survives the edit keeps its
    original line verbatim (inline comment included), in its new
    position. If the block contains structural lines that aren't items
    (indented comment-only lines, nested values), raise — a reorder
    can't preserve where those anchor.
    """
    eol = _eol(text)
    lines = text.splitlines(keepends=True)
    start, end = _frontmatter_span(lines)

    key_re = re.compile(rf"^{re.escape(key)}\s*:\s*(?P<rest>.*)$")
    key_idx = None
    for i in range(start, end):
        m = key_re.match(lines[i].rstrip("\r\n"))
        if m:
            key_idx = i
            inline_rest = m.group("rest")
            break
    if key_idx is None:
        raise ProfileEditError(f"frontmatter key {key!r} not found")

    value_part, _ = _split_value_comment(inline_rest)
    if value_part:
        # Inline form (``key: [a, b]``) — treat as a scalar replacement.
        return set_scalar(text, key, items)

    # Block form — collect lines until the next top-level key / fence.
    block_end = key_idx + 1
    item_lines: list[tuple[str, str]] = []   # (parsed value, original line)
    for i in range(key_idx + 1, end):
        stripped = lines[i].rstrip("\r\n")
        if not stripped.strip():
            # Blank line: only the END of the block is safe. A blank
            # *inside* the list (more items after it) would leave stale
            # item lines behind on rewrite — refuse rather than mangle.
            for j in range(i + 1, end):
                later = lines[j].rstrip("\r\n")
                if _TOP_KEY_RE.match(later):
                    break
                if _ITEM_RE.match(later):
                    raise ProfileEditError(
                        f"list {key!r} has a blank line between items — "
                        "edit this one in Obsidian"
                    )
            break
        if _TOP_KEY_RE.match(stripped):
            break
        m = _ITEM_RE.match(stripped)
        if not m:
            raise ProfileEditError(
                f"list {key!r} contains a non-item line "
                f"({stripped.strip()[:40]!r}) — edit this one in Obsidian"
            )
        value_part, _ = _split_value_comment(m.group("rest"))
        try:
            parsed = yaml.safe_load(value_part)
        except yaml.YAMLError as e:
            raise ProfileEditError(f"list {key!r}: unparseable item: {e}") from e
        item_lines.append((str(parsed), lines[i]))
        block_end = i + 1

    indent = (
        _ITEM_RE.match(item_lines[0][1].rstrip("\r\n")).group("indent")  # type: ignore[union-attr]
        if item_lines
        else "  "
    )

    remaining = list(item_lines)
    new_lines: list[str] = []
    for item in items:
        reused = next((pair for pair in remaining if pair[0] == item), None)
        if reused is not None:
            # Surviving item — keep its original line (inline comment
            # included) in its new position.
            remaining.remove(reused)
            new_lines.append(reused[1])
        else:
            new_lines.append(f"{indent}- {_yaml_scalar(item)}{eol}")

    return "".join(lines[: key_idx + 1] + new_lines + lines[block_end:])
