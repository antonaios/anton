"""Speculation TTL — stub.

Plan v3 §6.9 Phase 6. Finds `(speculation)` markers in claim files
older than N days, suggests re-classification.

**Stub implementation.** Full version requires:
  - Convention for dating speculation markers (currently inconsistent)
  - Operator workflow for how to handle expired speculations
  - Decision: in-place edit, separate proposal file, or just morning-brief flag

Defer full implementation until operator has accumulated enough
speculation markers in claim files for the workflow to be load-bearing.
For now this module exists so the CLI subcommand can exist + emit a
"not yet implemented" message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SpeculationMarker:
    """One `(speculation)` marker found in a vault file."""
    source_path: str
    line_number: int
    line_text: str
    associated_date: str | None  # parsed from nearby context, or None


def scan(vault_root: Path) -> list[SpeculationMarker]:
    """STUB: returns empty list. Real implementation deferred."""
    log.info("speculation.scan: stub — not yet implemented")
    return []


def render_report(markers: list[SpeculationMarker]) -> str:
    return (
        "# Speculation TTL scan\n\n"
        "_Stub implementation — Plan v3 §6.9 Phase 6.4 deferred._\n\n"
        "Will check every `(speculation)` marker in claim files against "
        "its associated date and suggest re-classification when stale.\n\n"
        "**Trigger to implement:** when claim files accumulate 10+ "
        "speculation markers with consistent dating convention.\n"
    )
