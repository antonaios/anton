"""Core data types shared across the engine.

Kept in a leaf module to break the excel_engine ↔ audit dependency cycle:
both modules depend on `EngineRun` and `TemplateSpec`; pulling them here
means neither has to import the other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TemplateSpec:
    """The wire-up contract for a single Excel template.

    A TemplateSpec is the durable description of what the engine knows about
    one template: where the .xlsx lives, what its inputs are (logical name →
    cell reference), what its outputs are, what validation rules apply
    post-recalc, and what hardcode-after-recalc steps the template needs to
    break circular references the user resolves manually today.

    Cell references can be either:
      - "SheetName!A1" / "SheetName!A1:B10"  — A1-style
      - "MyNamedRange"                         — workbook-scope named range
      - "SheetName!MyNamedRange"               — sheet-scope named range

    For 2D ranges (e.g. `Sensitivity!B5:F15`), the engine returns a nested
    list when reading and accepts a nested list when writing.
    """

    name: str
    path: Path
    version: str
    description: str
    inputs: dict[str, str]                   # logical-name -> ref
    outputs: dict[str, str]                  # logical-name -> ref
    optional_inputs: dict[str, str] = field(default_factory=dict)
    validation: list[dict[str, Any]] = field(default_factory=list)
    # Post-recalc hardcoding (breaks circular refs by paste-as-value).
    # Each step: {"source": "Sheet!Cell", "targets": [...], "converge": bool,
    #             "tolerance": float, "max_iters": int}
    post_recalc_hardcode: list[dict[str, Any]] = field(default_factory=list)
    template_hash: str = ""                  # sha256 of the .xlsx at registration


@dataclass(frozen=True)
class EngineRun:
    """Result of one template execution. Feeds the audit log and the caller."""

    run_id: str           # short uuid — feeds audit log + output filename
    template_name: str
    template_version: str
    template_hash: str
    output_path: Path     # populated .xlsx on disk
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    started_at: datetime
    duration_ms: int
    status: str           # "ok" | "validation_failed" | "excel_error"
    convergence_iters: int = 0   # passes through post_recalc_hardcode
    notes: list[str] = field(default_factory=list)
