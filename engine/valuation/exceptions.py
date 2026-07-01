"""Domain exceptions for the valuation engine.

Engine-layer code should raise these; the CLI / caller layer translates them
to exit codes or user-facing errors. All errors carry context (template name,
cell ref, expected vs actual) so debugging a failed run from the audit log is
possible without re-running.
"""
from __future__ import annotations


class EngineError(Exception):
    """Base class for all engine errors."""


class TemplateNotFound(EngineError):
    """Raised when a template name is not registered or its file does not exist."""


class TemplateHashMismatch(EngineError):
    """Raised when the live .xlsx file's sha256 differs from what was registered.

    Means someone edited the template since the cell map was locked. The cell
    map may now point at the wrong cells. Caller must either re-lock the map
    (`engine validate <name> --rehash`) or revert the file."""


class InputCellMismatch(EngineError):
    """Raised when a caller provides an input not present in the template's cell
    map, or fails to provide a required input. Optional inputs may be omitted."""


class CellRefUnresolved(EngineError):
    """Raised when a cell reference in the cell map doesn't resolve in the live
    workbook (named range deleted, sheet renamed, A1 ref out of bounds)."""


class ValidationFailed(EngineError):
    """Raised when a post-recalc validation rule fails (e.g., EV <= 0)."""


class ClientFSBlockInvalid(EngineError):
    """Raised when a caller-supplied `client_fs` operating-model block fails
    shape validation (non-int row keys, array length != dates length, bad ISO
    date, …). Caller error — fix the block, not the template."""


class ClientFSFormulaCollision(EngineError):
    """Raised when a `client_fs` target cell holds a formula. Formula cells in
    the Client_FS layout are total rows (=SUM(...)) — overwriting one would
    silently destroy the template's aggregation. The engine refuses instead."""


class ConvergenceFailed(EngineError):
    """Raised when a post_recalc_hardcode step is set to `converge: true` and
    the source cell hasn't stabilised within `max_iters` passes."""


class ExcelDriverError(EngineError):
    """Raised when xlwings / Excel COM fails (Excel crashed, file locked, dialog
    open). The wrapper layer surfaces this rather than swallowing it — the
    caller decides whether to retry."""


class ProjectBootstrapError(EngineError):
    """Raised when atomic project bootstrap fails (file system or vault side)."""
