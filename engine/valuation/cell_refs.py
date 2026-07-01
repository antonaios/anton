"""Cell-reference parsing.

Cell-map entries accept any of:
    "Sheet!A1"           — A1-style, sheet qualified
    "Sheet!A1:B10"       — A1 range
    "'Sheet With Space'!A1"  — quoted sheet name
    "MyNamedRange"       — workbook-scope named range
    "Sheet!MyNamedRange" — sheet-scope named range

This module parses and resolves them against a live workbook (xlwings) or
a parsed file (openpyxl, for offline validation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_A1_RE = re.compile(
    r"""^
        (?:                                # optional sheet prefix
            (?:'(?P<qsheet>[^']+)')        # 'Quoted Sheet'!
            | (?P<sheet>[A-Za-z_][A-Za-z0-9_.]*)
        )?
        (?:!)?
        (?P<addr>.+)                       # the rest
        $""",
    re.VERBOSE,
)

_A1_ADDR = re.compile(r"^\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class CellRef:
    sheet: str | None    # None for workbook-scope named range
    addr: str            # A1, A1:B10, or named-range
    is_range: bool       # True if A1:B10 form
    is_named: bool       # True if addr is a name (not A1)


def parse(ref: str) -> CellRef:
    """Parse a cell-map ref into structured form.

    Examples:
        parse("LBO!I25")       → CellRef(sheet="LBO", addr="I25", is_range=False, is_named=False)
        parse("LBO!B101:Q120") → CellRef(sheet="LBO", addr="B101:Q120", is_range=True, is_named=False)
        parse("Exit_multiple") → CellRef(sheet=None, addr="Exit_multiple", is_range=False, is_named=True)
        parse("LBO!Exit_multiple") → CellRef(sheet="LBO", addr="Exit_multiple", is_range=False, is_named=True)
    """
    if not ref or not isinstance(ref, str):
        raise ValueError(f"Cell ref must be a non-empty string, got {ref!r}")

    # Two-part: "Sheet!Addr" or "'Sheet Name'!Addr"
    if "!" in ref:
        sheet_part, addr_part = ref.split("!", 1)
        sheet = sheet_part.strip()
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1]
        addr = addr_part.strip()
    else:
        sheet = None
        addr = ref.strip()

    is_a1 = bool(_A1_ADDR.match(addr))
    is_range = is_a1 and ":" in addr
    is_named = not is_a1

    return CellRef(sheet=sheet, addr=addr, is_range=is_range, is_named=is_named)


def references(ref: str) -> tuple[str | None, str]:
    """Convenience: return (sheet_or_None, addr_or_name)."""
    c = parse(ref)
    return c.sheet, c.addr
