"""LBO skill — Pydantic IO + engine-subprocess wrapper (#21 first SKILL.md migration).

Thin IO layer over the existing valuation engine. The skill assembles inputs,
shells to ``engine run lbo --output-json``, and maps the engine's real output
back into a structured :class:`LBOOutput`. **No LBO maths happens here** — the
engine (``<repo>/engine``) is the single source of truth for the model;
wanting to "improve" a number is STOP territory (that is #18, a separate repo).

Reality notes baked in from the 2026-05-29 baseline runs against the live v4
DemoDeal-shape template (do not re-derive):

  * ``LBOInput`` mirrors ``engine/templates/inputs/lbo-DemoDeal-example.json``
    + ``templates.yaml`` (20 required + 10 optional engine inputs) — NOT the
    idealised entry_multiple/ltm_ebitda shape the migration plan sketched.
  * The engine emits ``irr_grid`` (9×9 numeric, decimal), a formatted
    "IRR%/MOICx" ``output_summary_table`` (text grid), and headline scalars
    (ftev, sponsor_equity, …). It does NOT emit a numeric moic_grid, a
    return bridge, or covenant-headroom paths — so ``LBOOutput`` exposes what
    the engine actually produces and leaves the rest explicitly unavailable.
  * Sources & Uses is a STRUCTURAL identity in the template (Sources = Uses by
    construction; check cell G97 — the last row of ``sources_and_uses`` — is
    always TRUE). It cannot be broken via inputs. The realisable "garbage in →
    STOP" is the engine's own validation gate (e.g. holding_period beyond the
    projection horizon → IRR-grid centre non-numeric → ValidationFailed,
    engine exit code 2).
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Engine location (engine is its own repo + venv — Python 3.13, separate from
# the routines venv). Resolved as module constants so the wrapper has no import
# coupling to the engine package.
# ─────────────────────────────────────────────────────────────────────────────

ENGINE_REPO = Path(r"<repo>\engine")
ENGINE_VENV_PYTHON = ENGINE_REPO / ".venv" / "Scripts" / "python.exe"
ENGINE_CLI_MODULE = "valuation.cli"
ENGINE_TIMEOUT_SECONDS = 90  # matches SKILL.md cost_ceiling_seconds


# ─────────────────────────────────────────────────────────────────────────────
# #18-engine-roots-sync: the operator profile is the SINGLE source of truth for
# the workspace filesystem roots. The engine mirrors them in its DEFAULT_ROOTS
# only as a bare-CLI fallback; the bridge (this skill) must resolve the root
# FROM THE PROFILE and always pass ``--workspace-root <resolved-root>`` so the
# two can never silently diverge.
#
# Mapping (mirrors routines/api/routes/workspaces.py::_roots_for — the bridge's
# canonical workspace-root resolver):
#   project → external_project_paths[0]   (the active mandate root; index 0 is
#                                           the bridge's create-default too)
#   bd      → external_bd_path
#   general → external_general_path
#
# Resolution is best-effort: if the profile is missing/unparseable or the root
# for the type is unconfigured, we return None and DO NOT pass --workspace-root,
# leaving the engine on its DEFAULT_ROOTS (the prior behaviour — zero impact
# while the two match).
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_workspace_root(workspace_type: str) -> Path | None:
    """Resolve the filesystem root for ``workspace_type`` from the operator
    profile, or ``None`` if it cannot be resolved (caller then omits the flag
    and the engine falls back to its DEFAULT_ROOTS).

    Imports are deferred to call time so the module stays importable without the
    bridge's heavier deps and so tests can monkeypatch the profile loader."""
    try:
        from routines.api.deps import VAULT
        from routines.shared import profile as profile_mod

        prof = profile_mod.load(VAULT)
        if workspace_type == "project":
            # Honor index 0 LITERALLY (the active mandate root). An empty/unset
            # [0] means the active project root is unconfigured ⇒ fall back to
            # None (engine DEFAULT_ROOTS) — do NOT silently promote [1].
            paths = prof.external_project_paths or []
            root = paths[0] if paths else ""
        elif workspace_type == "bd":
            root = prof.external_bd_path
        elif workspace_type == "general":
            root = prof.external_general_path
        else:
            root = ""
        return Path(root) if root else None
    except Exception as e:  # noqa: BLE001 — profile unreachable ⇒ engine fallback
        log.warning(
            "LBO: could not resolve workspace root from profile for type %r "
            "(%s); falling back to engine DEFAULT_ROOTS", workspace_type, e,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions — let the bridge route classify failures into HTTP codes.
# ─────────────────────────────────────────────────────────────────────────────


class LBOSkillError(Exception):
    """Base for all LBO-skill failures."""


class EngineTimeout(LBOSkillError):
    """The engine subprocess exceeded its wall-clock budget."""


class EngineRunFailed(LBOSkillError):
    """The engine returned a non-zero exit code (input mismatch, hash drift,
    convergence failure, or — most importantly — a validation-gate failure)."""


class EngineOutputMalformed(LBOSkillError):
    """The engine exited 0 but its --output-json line could not be parsed."""


class ValidationGateFailed(LBOSkillError):
    """Raised when an Iron-Law verification phase fails (see the bridge route)."""


# ─────────────────────────────────────────────────────────────────────────────
# Input model — mirrors the real engine fixture (templates.yaml `lbo`).
# ─────────────────────────────────────────────────────────────────────────────


class ClientFSBlock(BaseModel):
    """Operating-model block written into the template's `Client_FS` sheet
    BEFORE the input cells (engine `--client-fs`; feat/client-fs-write).

    Mirrors the engine schema (engine `templates/templates.yaml` → lbo →
    "client_fs pre-write"). The engine is the enforcement point (formula-cell
    refusal, fail-fast shape checks); this model mirrors the shape checks so a
    bad block fails at the bridge as a 422/re-suspend instead of an engine
    exit-1.

    UNITS CONTRACT: values are passed VERBATIM to the workbook — `Client_FS`
    holds FULL currency units, so callers send full £ values (17900000.0 for
    £17.9m). NB this deliberately differs from the LBOInput cells
    (``acq_ebitda`` etc.), which the template takes in millions.

    Layout contract (post-restructure Client_FS): ``dates`` → row 4, J:S (the
    SUMIFS join keys OpModel_Link matches against the LBO timeline — keep them
    consistent with ``first_fye``); data rows 6=revenue, 13=EBITDA, 25=D&A,
    32=ΔNWC, 38=capex; component rows zeroed via ``zero_rows``; total rows
    (r11/r20/r30/r36/r43) are =SUM() formulas the engine refuses to touch.
    """

    # extra="forbid": a typoed key ("rowz", "zero_row") must 422/re-suspend at
    # the bridge, not be silently dropped before model_dump() builds the engine
    # tempfile — that would bypass the engine's own unknown-key refusal and
    # leave stale component rows in Client_FS (codex review 2026-06-10 SEV-1).
    model_config = ConfigDict(extra="forbid")

    dates: Annotated[list[str], Field(
        min_length=10, max_length=10,
        description="Exactly 10 ISO dates (the J:S period window) -> Client_FS row 4",
    )]
    rows: dict[int, list[float]]            # row number -> 10 values (verbatim, full £)
    zero_rows: list[int] = Field(default_factory=list)
    sheet: str = "Client_FS"

    # The engine's Client_FS date row — owned by `dates`, refused as a data row.
    _DATE_ROW = 4

    @model_validator(mode="after")
    def _check_shape(self) -> "ClientFSBlock":
        """Mirror the engine's _normalize_client_fs checks (codex 2026-06-10
        SEV-2: a gap here turns a fixable re-suspend into an engine exit-1)."""
        import math
        from datetime import date

        for i, d in enumerate(self.dates):
            try:
                date.fromisoformat(d)
            except ValueError:
                raise ValueError(f"dates[{i}] is not an ISO date: {d!r}")
        if not self.sheet.strip():
            raise ValueError("sheet must be a non-empty string")
        if not self.rows:
            raise ValueError("rows must be a non-empty dict of row -> values")
        for row, values in self.rows.items():
            if row < 1:
                raise ValueError(f"rows[{row}] out of range (must be >= 1)")
            if row == self._DATE_ROW:
                raise ValueError(
                    f"rows[{row}] collides with the date row (row {self._DATE_ROW} "
                    "is owned by dates)")
            if len(values) != len(self.dates):
                raise ValueError(
                    f"rows[{row}] has {len(values)} values; expected "
                    f"{len(self.dates)} (== dates length)")
            for j, v in enumerate(values):
                if not math.isfinite(v):
                    raise ValueError(f"rows[{row}][{j}] is not finite: {v!r}")
        for z in self.zero_rows:
            if z < 1:
                raise ValueError(f"zero_rows[{z}] out of range (must be >= 1)")
            if z == self._DATE_ROW:
                raise ValueError(
                    f"zero_rows[{z}] collides with the date row (row {self._DATE_ROW} "
                    "is owned by dates)")
        if len(set(self.zero_rows)) != len(self.zero_rows):
            raise ValueError("zero_rows contains duplicates")
        overlap = set(self.rows) & set(self.zero_rows)
        if overlap:
            raise ValueError(f"rows {sorted(overlap)} appear in both rows and zero_rows")
        return self


class LBOInput(BaseModel):
    """Inputs to the LBO skill.

    Field names + units mirror ``engine/templates/inputs/lbo-DemoDeal-example.json``
    and the ``lbo`` entry in ``engine/templates/templates.yaml``. EBITDA / fees /
    quanta are in the deal currency's millions; rates and fractions are decimals
    (0.25 = 25%); multiples are literal (10 = 10.0x); dates are ISO YYYY-MM-DD.
    """

    # ─── Routing + workspace context (NOT engine inputs) ─────────────────────
    deal_name: Annotated[str, Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9 _-]*$",
        description="Workspace folder name — back-compat fallback when workspace_name "
                    "is unset/'default'. Resolved under the workspace_type root "
                    "(<workspace-root>/{1. Projects|2. Business development|3. General}).",
    )]
    workspace_type: Literal["project", "bd", "general"] = "project"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "confidential"

    # ─── Required engine inputs (20) ─────────────────────────────────────────
    first_fye: str                       # First_FYE  (I5)
    project_name: str                    # Project_name (I8)
    scenario_provider: str               # Scenario_provider (I9), usually "Management"
    currency: str                        # Currency (I10): "GBP" | "EUR" | "USD"
    fye_post_acq: str                    # FYE_post_acq (I11)
    tax_rate: Annotated[float, Field(ge=0, le=1)]            # Tax_rate (I12)

    acq_multiple: Annotated[float, Field(gt=0)]             # Acq_multiple (I25)
    ebitda_basis: str                    # EBITDA_basis (H26): "Adj" | "Mgmt" | "Reported"
    acq_ebitda: Annotated[float, Field(gt=0)]               # Acq_EBITDA (M28), £m
    acq_date: str                        # Acq_date (I28)
    holding_period: Annotated[int, Field(ge=1, le=10)]      # Holding_period (I30); engine gate caps usable horizon (~5 for Jun acq)
    ma_fees: Annotated[float, Field(ge=0)]                  # MA_fees (I32), £m
    step_multiple: Annotated[float, Field(gt=0)]            # Step_multiple (J25)

    debt_ebitda: Annotated[float, Field(ge=0)]              # Debt_EBITDA (I38)
    min_equity: Annotated[float, Field(ge=0, le=1)]         # Min_equity (I40), floors sponsor equity %
    tla_split: Annotated[float, Field(ge=0, le=1)]          # TLA_split (H54); TLB = 1 - TLA
    rcf_quantum: Annotated[float, Field(ge=0)]              # RCF_quantum (H56), £m
    rcf_switch: Literal[0, 1]                                # RCF_switch (I56)
    pref_interest: Annotated[float, Field(ge=0, le=1)]      # Pref_interest (I66)

    existing_net_debt: Annotated[float, Field(ge=0)]        # Existing_net_debt (I81), £m

    # ─── Optional engine inputs (omit → cell keeps its template value) ───────
    scenario_1: int | None = None
    scenario_2: int | None = None
    scenario_3: int | None = None
    scenario_selected: int | None = None
    scenario_5: int | None = None
    sweet_ord_pct: float | None = None
    mgmt_ord_pct: float | None = None
    sponsor_ord_pct: float | None = None
    ord_pref_split: float | None = None
    min_cash: float | None = None

    # ─── Operating model (optional) — NOT an engine cell input ──────────────
    # Shipped to the engine as a separate JSON file via `--client-fs` (the
    # engine writes it into Client_FS before the input cells). Omit → the run
    # uses whatever operating model sits in the template's Client_FS sheet.
    client_fs: ClientFSBlock | None = None

    # ─── ANTON-side, stripped before the engine sees the file ────────────────
    citations: list[dict] = Field(
        default_factory=list,
        description="Per-assumption source register entries. Required at the bridge layer; "
                    "permissive here so engine smoke tests can run without them.",
    )

    # Keys that are NOT engine inputs and must be stripped before shelling out.
    # client_fs is engine-bound but NOT a cell input — it travels as its own
    # JSON file on --client-fs, so it must not appear in the --inputs payload
    # (the engine strict-checks --inputs keys against its cell map).
    _NON_ENGINE_FIELDS = (
        "deal_name", "citations",
        "workspace_type", "workspace_name", "workspace_sensitivity",
        "client_fs",
    )

    def engine_inputs(self) -> dict[str, Any]:
        """The exact dict the engine expects: required + provided optionals,
        with routing/ANTON fields removed and unset optionals dropped (the
        engine strict-checks keys against its inputs/optional_inputs maps)."""
        dumped = self.model_dump(exclude_none=True)
        for k in self._NON_ENGINE_FIELDS:
            dumped.pop(k, None)
        return dumped


# ─────────────────────────────────────────────────────────────────────────────
# Output models — mirror what the engine ACTUALLY returns (see module docstring).
# ─────────────────────────────────────────────────────────────────────────────


class LBOReturns(BaseModel):
    """Central-case returns. IRR is the numeric grid centre; MOIC is parsed
    from the formatted summary grid (engine emits no numeric MOIC grid)."""
    irr_central_pct: float | None        # irr_grid centre × 100
    moic_central_x: float | None         # parsed "…/N.Nx" central cell; None if unparseable
    equity_cheque_m: float               # sponsor_equity (new sponsor cheque at close)
    hold_years: int                      # echo of holding_period


class LBOHeadline(BaseModel):
    """Headline S&U / cap-structure values used in narration (all £m / literal x)."""
    ftev_m: float                        # fully-loaded TEV = total sources (= I76 / G96)
    entry_multiple: float                # Acq_multiple echo (I25)
    exit_multiple: float                 # Exit_multiple echo (I27)
    tla_quantum_m: float
    tlb_quantum_m: float
    net_debt_at_close_m: float
    sponsor_equity_m: float
    management_equity_m: float
    total_equity_m: float
    stub_period: float


class LBOSensitivity(BaseModel):
    """The sensitivity surface. ``irr_grid`` is the robust numeric primary;
    the formatted grid + axes are read from the workbook's Output Summary table
    (per CLAUDE.md §14 Q7 — read, don't compute). ``moic_grid`` is intentionally
    None: the v4 engine does not emit a numeric MOIC grid."""
    irr_grid: list[list[float | None]]               # 9×9 numeric IRR (decimal)
    entry_axis: list[float]                          # entry multiples (cols)
    exit_axis: list[float]                           # exit multiples (rows of the formatted grid)
    summary_grid: list[list[str | None]]             # formatted "IRR%/MOICx" exit × entry block
    moic_grid: None = None                           # NOT produced by engine v4


class LBOValidation(BaseModel):
    """Honest validation surface. The engine runs 7 internal rules (S&U/FTEV/
    equity/stub/IRR-grid sanity); ``engine_rules_passed`` is True iff status=="ok".
    ``sources_and_uses_ties`` reflects the template's structural tie-check cell
    (G97). Fields the engine does NOT check (balance-sheet balance, covenant
    headroom, MOIC×hold reconciliation) are deliberately absent rather than
    fabricated."""
    engine_status: str                   # "ok" | "validation_failed" | "excel_error"
    engine_rules_passed: bool
    sources_and_uses_ties: bool


class LBOOutput(BaseModel):
    ok: bool
    deal_name: str
    run_id: str
    output_xlsx_path: str
    duration_ms: int
    convergence_iters: int
    returns: LBOReturns
    headline: LBOHeadline
    sensitivity: LBOSensitivity
    sources_and_uses: list[list[Any]]    # raw G89:H97 (value, %) + tie-check row
    validation: LBOValidation
    warnings: list[str]
    citations: list[dict]


# ─────────────────────────────────────────────────────────────────────────────
# Summary-table parsing (defensive — read from the workbook, warn-and-fallback
# rather than crash if the layout shifts).
# ─────────────────────────────────────────────────────────────────────────────

# In the v4 DemoDeal-shape template the Output Summary block (B101:Q120) lays
# the 9 entry multiples / 9 formatted values out starting at column index 7 of
# each row. Treated as a hint, validated at parse time.
_GRID_DATA_START_COL = 7


def _parse_summary_table(
    table: list[list[Any]],
    entry_multiple_chosen: float,
    exit_multiple_chosen: float,
) -> tuple[list[float], list[float], list[list[str | None]], float | None, list[str]]:
    """Return (entry_axis, exit_axis, formatted_grid, central_moic, warnings)."""
    warnings: list[str] = []
    entry_axis: list[float] = []
    exit_axis: list[float] = []
    formatted_grid: list[list[str | None]] = []

    # Entry axis: the row whose first cell is "x" carries the entry multiples.
    for row in table:
        if row and isinstance(row[0], str) and row[0].strip() == "x":
            entry_axis = [c for c in row[_GRID_DATA_START_COL:] if isinstance(c, (int, float))]
            break
    if not entry_axis:
        warnings.append("entry axis not found in Output Summary table (layout drift?)")

    # Exit rows: every row labelled "Exit at <m>x …" holds a formatted IRR/MOIC line.
    for row in table:
        if not row or not isinstance(row[0], str):
            continue
        label = row[0].strip()
        if label.lower().startswith("exit at"):
            # "Exit at 10.0x LTM EBITDA" → 10.0
            try:
                token = label.split("at", 1)[1].strip().split("x", 1)[0].strip()
                exit_axis.append(float(token))
            except (ValueError, IndexError):
                warnings.append(f"could not parse exit multiple from label {label!r}")
                exit_axis.append(float("nan"))
            formatted_grid.append([
                c if isinstance(c, str) else None
                for c in row[_GRID_DATA_START_COL:_GRID_DATA_START_COL + max(len(entry_axis), 9)]
            ])

    if not formatted_grid:
        warnings.append("no 'Exit at …' rows found in Output Summary table")

    # Central MOIC: the formatted cell at (exit==chosen, entry==chosen).
    central_moic: float | None = None
    try:
        exit_idx = min(
            range(len(exit_axis)),
            key=lambda i: abs(exit_axis[i] - exit_multiple_chosen),
        )
        entry_idx = min(
            range(len(entry_axis)),
            key=lambda i: abs(entry_axis[i] - entry_multiple_chosen),
        )
        cell = formatted_grid[exit_idx][entry_idx]
        if isinstance(cell, str) and "/" in cell:
            moic_token = cell.split("/", 1)[1].strip().rstrip("xX").strip()
            central_moic = float(moic_token)
    except (ValueError, IndexError):
        warnings.append("central MOIC not parseable from Output Summary grid")

    return entry_axis, exit_axis, formatted_grid, central_moic, warnings


def _grid_centre(grid: list[list[Any]]) -> Any:
    """Centre cell of a square-ish 2D grid (the chosen-entry × chosen-exit IRR)."""
    if not grid:
        return None
    r = grid[len(grid) // 2]
    if not isinstance(r, list) or not r:
        return None
    return r[len(r) // 2]


# ─────────────────────────────────────────────────────────────────────────────
# run() — the one public entrypoint. Shells to the engine; maps output.
# ─────────────────────────────────────────────────────────────────────────────


def run(inputs: LBOInput) -> LBOOutput:
    """Shell to ``engine run lbo --output-json`` and map the result.

    NO behaviour change vs invoking the engine CLI directly. Raises
    LBOSkillError subclasses on every failure mode so the bridge can map them
    to HTTP status codes.
    """
    # 1. Write the engine inputs to a tempfile (the CLI takes --inputs <path>).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
        prefix=f"lbo-{inputs.deal_name}-",
    ) as f:
        json.dump(inputs.engine_inputs(), f, indent=2)
        inputs_path = Path(f.name)

    # 1b. Optional operating model — its own tempfile on --client-fs (the
    #     engine writes it into Client_FS before the input cells; values
    #     verbatim, full currency units per the ClientFSBlock contract).
    client_fs_path: Path | None = None
    if inputs.client_fs is not None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
            prefix=f"lbo-clientfs-{inputs.deal_name}-",
        ) as f:
            json.dump(inputs.client_fs.model_dump(), f, indent=2)
            client_fs_path = Path(f.name)

    # 2. Shell to the engine (its own venv + repo cwd).
    #    #18: address the workspace by type:name. ``deal_name`` is the canonical,
    #    required, validated folder identity (every caller populates it; the gate's
    #    ``workspace_name`` is a separate, often-"default" field — NOT the folder).
    #    Using deal_name avoids the sentinel-misrouting trap where a real workspace
    #    happens to be named "default".
    #
    #    #18-engine-roots-sync: the operator profile is the single source of truth
    #    for the workspace root. Resolve it here and ALWAYS pass --workspace-root so
    #    the engine never falls back to its (duplicated) DEFAULT_ROOTS. If the
    #    profile is unreachable, ``_resolve_workspace_root`` returns None and we omit
    #    the flag — the engine then uses DEFAULT_ROOTS (prior behaviour, no impact
    #    while the two match).
    ws_ref = f"{inputs.workspace_type}:{inputs.deal_name}"
    engine_args = [
        str(ENGINE_VENV_PYTHON), "-m", ENGINE_CLI_MODULE,
        "run", "lbo",
        "--inputs", str(inputs_path),
        "--workspace", ws_ref,
    ]
    if client_fs_path is not None:
        engine_args += ["--client-fs", str(client_fs_path)]
    ws_root = _resolve_workspace_root(inputs.workspace_type)
    if ws_root is not None:
        engine_args += ["--workspace-root", str(ws_root)]
    engine_args.append("--output-json")
    try:
        proc = subprocess.run(
            engine_args,
            capture_output=True, text=True,
            timeout=ENGINE_TIMEOUT_SECONDS,
            cwd=str(ENGINE_REPO),
        )
    except subprocess.TimeoutExpired as e:
        raise EngineTimeout(
            f"engine run lbo exceeded {ENGINE_TIMEOUT_SECONDS}s for deal {inputs.deal_name!r}"
        ) from e
    finally:
        inputs_path.unlink(missing_ok=True)
        if client_fs_path is not None:
            client_fs_path.unlink(missing_ok=True)

    # 3. Non-zero exit = engine refusal. Validation-gate failures (exit 2)
    #    land here — this is the Iron Law firing: no returns are produced.
    if proc.returncode != 0:
        raise EngineRunFailed(
            f"engine exited {proc.returncode} for deal {inputs.deal_name!r}: "
            f"{(proc.stderr or proc.stdout).strip()[:600]}"
        )

    # 4. Parse the final stdout line as the JSON payload.
    try:
        last_line = proc.stdout.strip().splitlines()[-1]
        engine = json.loads(last_line)
    except (json.JSONDecodeError, IndexError) as e:
        raise EngineOutputMalformed(
            f"engine stdout not parseable as JSON for deal {inputs.deal_name!r}: "
            f"{proc.stdout[-600:]!r}"
        ) from e

    output = _map_engine_output(engine, inputs)

    # Iron Law enforcement (belt-and-braces). The engine's exit code already
    # gates on its internal rules; here we additionally suppress returns if the
    # S&U tie-check cell is ever False. In the v4 template S&U is a structural
    # identity (always True), so this never fires today — but it makes the Iron
    # Law mechanical (see tests/skills/lbo/test_iron_law.py) and survives any
    # future template change that could break the tie.
    if not output.validation.sources_and_uses_ties:
        raise ValidationGateFailed(
            f"Iron Law: S&U tie-check is FALSE for deal {inputs.deal_name!r}; "
            f"returns suppressed (engine status={output.validation.engine_status!r})"
        )
    return output


def _map_engine_output(engine: dict[str, Any], inputs: LBOInput) -> LBOOutput:
    """Map the engine's raw --output-json payload onto LBOOutput."""
    out = engine.get("outputs", {})
    warnings: list[str] = []

    irr_grid = out.get("irr_grid") or []
    entry_chosen = float(out.get("entry_multiple_chosen", inputs.acq_multiple))
    exit_chosen = float(out.get("exit_multiple_chosen", entry_chosen))

    entry_axis, exit_axis, summary_grid, central_moic, parse_warnings = _parse_summary_table(
        out.get("output_summary_table") or [], entry_chosen, exit_chosen,
    )
    warnings.extend(parse_warnings)

    irr_centre = _grid_centre(irr_grid)
    irr_central_pct = round(irr_centre * 100, 2) if isinstance(irr_centre, (int, float)) else None
    if irr_central_pct is None:
        warnings.append("IRR-grid centre is non-numeric — central IRR unavailable")

    sou = out.get("sources_and_uses") or []
    # Tie-check cell (G97) is the first element of the last S&U row.
    sou_ties = bool(sou[-1][0]) if (sou and isinstance(sou[-1], list) and sou[-1]) else False

    status = engine.get("status", "ok")

    return LBOOutput(
        ok=True,
        deal_name=inputs.deal_name,
        run_id=engine.get("run_id", ""),
        output_xlsx_path=engine.get("output_path", ""),
        duration_ms=int(engine.get("duration_ms", 0)),
        convergence_iters=int(engine.get("convergence_iters", 0)),
        returns=LBOReturns(
            irr_central_pct=irr_central_pct,
            moic_central_x=central_moic,
            equity_cheque_m=float(out.get("sponsor_equity", 0.0)),
            hold_years=inputs.holding_period,
        ),
        headline=LBOHeadline(
            ftev_m=float(out.get("ftev", 0.0)),
            entry_multiple=entry_chosen,
            exit_multiple=exit_chosen,
            tla_quantum_m=float(out.get("tla_quantum", 0.0)),
            tlb_quantum_m=float(out.get("tlb_quantum", 0.0)),
            net_debt_at_close_m=float(out.get("net_debt_at_close", 0.0)),
            sponsor_equity_m=float(out.get("sponsor_equity", 0.0)),
            management_equity_m=float(out.get("management_equity", 0.0)),
            total_equity_m=float(out.get("total_equity", 0.0)),
            stub_period=float(out.get("stub_period", 0.0)),
        ),
        sensitivity=LBOSensitivity(
            irr_grid=irr_grid,
            entry_axis=entry_axis,
            exit_axis=exit_axis,
            summary_grid=summary_grid,
        ),
        sources_and_uses=sou,
        validation=LBOValidation(
            engine_status=status,
            engine_rules_passed=(status == "ok"),
            sources_and_uses_ties=sou_ties,
        ),
        warnings=warnings,
        citations=inputs.citations,
    )


__all__ = [
    "ClientFSBlock",
    "LBOInput", "LBOOutput", "LBOReturns", "LBOHeadline", "LBOSensitivity",
    "LBOValidation", "run",
    "LBOSkillError", "EngineTimeout", "EngineRunFailed", "EngineOutputMalformed",
    "ValidationGateFailed",
]
