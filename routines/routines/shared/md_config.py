"""Parse operator-editable config from markdown files.

We support two formats so the operator can pick the Obsidian editing
experience that works best:

1. **Body code blocks** (preferred — Obsidian Live Preview shows these
   with syntax highlighting and click-to-edit). The data lives in a
   fenced YAML block following an `## <name>` heading. Any markdown
   between the heading and the code block (description, tables, etc.)
   is ignored — we just find the first fenced YAML block under the
   heading, before the next `##`/`#` heading.

       ## ticker_bar

       Some description, tables, etc.

       ```yaml
       - { symbol: JDW.L, name: J D Wetherspoon }
       - { symbol: IHG.L, name: InterContinental }
       ```

2. **Frontmatter** (back-compat for the older config files):

       ---
       ticker_bar:
         - { symbol: JDW.L, name: J D Wetherspoon }
       ---

`extract_section(text, name)` tries the body-block format first, then
falls back to frontmatter. Returns the parsed list (or None if neither
form found a list under that name).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import frontmatter
import yaml

log = logging.getLogger(__name__)


_HEADING_RE = re.compile(r"^\s*#+\s+(.+?)\s*$")
_FENCE_OPEN_RE = re.compile(r"^\s*```\s*(?:yaml|yml)?\s*$", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")

# Public aliases — the WRITE side (routines.operatorconfig.blocks) walks the
# same grammar to surgically replace a block. Shared names keep the two
# sides from drifting.
HEADING_RE = _HEADING_RE
FENCE_OPEN_RE = _FENCE_OPEN_RE
FENCE_CLOSE_RE = _FENCE_CLOSE_RE


def extract_section(text: str, name: str) -> Optional[list[Any]]:
    """Find a YAML list under `## <name>` (body code block) or under
    `<name>:` in YAML frontmatter. Returns the parsed list, or None.
    """
    body_result = _extract_from_body_block(text, name)
    if body_result is not None:
        return body_result

    # Frontmatter back-compat.
    try:
        post = frontmatter.loads(text)
        val = post.metadata.get(name) if isinstance(post.metadata, dict) else None
        if isinstance(val, list):
            return val
    except Exception as e:  # noqa: BLE001
        log.warning("md_config: frontmatter parse failed: %s", e)

    return None


def _extract_from_body_block(text: str, name: str) -> Optional[list[Any]]:
    """Walk the markdown looking for `## <name>` followed by the next
    fenced YAML code block (allowing arbitrary doc content in between).
    Stops scanning if a different `##`/`#` heading appears before the
    block is found — keeps sections isolated.
    """
    target_lower = name.lower()
    lines = text.splitlines()

    in_section = False
    in_block = False
    block_lines: list[str] = []

    for line in lines:
        heading_match = _HEADING_RE.match(line)

        # If we hit a heading while looking for the block, decide:
        if heading_match and not in_block:
            heading_name = heading_match.group(1).strip().lower()
            # `## ticker_bar` (or `# ticker_bar`) — match by exact text.
            if heading_name == target_lower:
                in_section = True
            elif in_section:
                # We were in the target section but a new heading appeared
                # without ever finding a code block — give up.
                return None
            continue

        if not in_section:
            continue

        # Inside the target section, scanning for a fenced YAML block.
        if not in_block:
            if _FENCE_OPEN_RE.match(line):
                in_block = True
                block_lines = []
            continue

        # Inside the YAML block — look for the closing fence.
        if _FENCE_CLOSE_RE.match(line):
            body = "\n".join(block_lines)
            try:
                parsed = yaml.safe_load(body)
            except yaml.YAMLError as e:
                log.warning("md_config: %s block YAML parse failed: %s", name, e)
                return None
            if isinstance(parsed, list):
                return parsed
            log.warning(
                "md_config: %s block parsed but not a list (got %s)",
                name, type(parsed).__name__,
            )
            return None
        block_lines.append(line)

    return None
