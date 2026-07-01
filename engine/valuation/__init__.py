"""Agentic OS valuation engine — Excel-template wrapper.

A thin Python orchestrator that drives the operator's *existing* Excel templates
(LBO, DCF, comps, sensitivity, etc.) as the deterministic numerical engine.
**No financial logic is rewritten in Python.** Excel does the maths; Python
feeds inputs in, triggers recalc, reads outputs back, and logs the run.

Public surface:

    from valuation.registry import TemplateRegistry
    from valuation.excel_engine import run, validate
    from valuation.models import TemplateSpec, EngineRun
    from valuation.exceptions import EngineError, ...

CLI entry-point: `engine` (defined in pyproject.toml as `valuation.cli:main`).

Architecture rationale + extension path for new templates: see README.md.
"""

__version__ = "0.2.0"

from valuation.exceptions import (
    CellRefUnresolved,
    ClientFSBlockInvalid,
    ClientFSFormulaCollision,
    ConvergenceFailed,
    EngineError,
    ExcelDriverError,
    InputCellMismatch,
    ProjectBootstrapError,
    TemplateHashMismatch,
    TemplateNotFound,
    ValidationFailed,
)
from valuation.models import EngineRun, TemplateSpec

__all__ = [
    "__version__",
    # Models
    "TemplateSpec",
    "EngineRun",
    # Exceptions
    "EngineError",
    "TemplateNotFound",
    "TemplateHashMismatch",
    "InputCellMismatch",
    "CellRefUnresolved",
    "ClientFSBlockInvalid",
    "ClientFSFormulaCollision",
    "ConvergenceFailed",
    "ValidationFailed",
    "ExcelDriverError",
    "ProjectBootstrapError",
]
