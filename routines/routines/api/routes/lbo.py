"""LBO skill bridge route (#21 — first SKILL.md migration; #61 substrate hardening).

``POST /api/workflows/lbo`` — assembles an LBO via the valuation engine and
returns a structured :class:`LBOOutput`. The handler:

  1. Requires citations (422 if empty) — every assumption needs a source.
     This is skill-specific INPUT validation, not a sensitivity gate, so it
     stays here at the route.
  2. Wraps the engine call in the real ``tool_call_hooks`` context manager.
     The central ``enforce_skill_sensitivity`` guard (#61) fires in the
     before-tool phase: it reads the LBO skill's declared ``workspace_scope``
     (``project``) from the registry and the call's ``sensitivity`` from the
     context, and raises :class:`SkillScopeRefused` for a non-project
     workspace or MNPI inputs. The route no longer hand-rolls that gate — it
     just maps the central refusal to HTTP 403. (Pre-#61 this route inlined
     ``_SKILL_METADATA`` + the workspace/MNPI check; both are now gone.)
  3. Maps engine failures → HTTP: EngineTimeout→504, EngineRunFailed→502
     (this is also the Iron-Law / validation-gate path), other LBOSkillError→500.

The audit row (``runs/tool.lbo.jsonl``) is written by the ``audit_tool_call``
after-hook on the same ``tool_call_hooks`` path.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from routines.skills._runtime.anton_skill import (
    SkillSuspended,
    anton_skill,
    current_resume,
)
from routines.skills.lbo.scripts.lbo import (
    EngineRunFailed,
    EngineTimeout,
    LBOInput,
    LBOOutput,
    LBOSkillError,
    ValidationGateFailed,
    run as run_lbo,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# #lbo-dashboard-wiring — intake mode (the #63 suspend/resume "boxes" prompt)
# ─────────────────────────────────────────────────────────────────────────────

# How long an intake suspension stays resumable. Deal boxes routinely arrive
# hours after the fire (operator reviews docs first), so this is days not the
# wrapper's 10-minute default. A lapsed intake is a clean 410 → re-fire.
INTAKE_TTL_S = 7 * 24 * 3600


class LBOIntakeInput(BaseModel):
    """Intake-mode fire: no engine inputs yet — the skill suspends and asks the
    operator for the deal-assumption boxes (#63 cooperative suspend/resume).

    ``mode`` is the union discriminator vs the full :class:`LBOInput` (which has
    no ``mode`` field), so the two payload shapes can share ``POST /workflows/lbo``
    without ambiguity. Workspace fields mirror LBOInput — the ``@anton_skill``
    wrapper reads them for the central scope/MNPI gate on the FIRST call (on
    resume, governance comes from the suspension row).
    """

    mode: Literal["intake"]
    deal_name: Annotated[str, Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9 _-]*$",
    )]
    workspace_type: Literal["project", "bd", "general"] = "project"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "confidential"
    # Operator's free-text deal description — carried into the suspension so the
    # boxes prompt (and the future chat-agent leg) has the context.
    deal_context: str = ""
    # Optional pre-filled boxes (e.g. the agent already knows acq_ebitda).
    # Merged under the operator's resume answer — the answer always wins.
    prefill: dict[str, Any] = Field(default_factory=dict)
    # Optional operating-model block (feat/lbo-client-fs) — e.g. the chat-agent
    # leg already built the deal's Client_FS model before firing intake. NOT a
    # boxes field (the manifest is unchanged); carried opaquely in the
    # suspension state and validated as ClientFSBlock when the resumed answer
    # assembles the full LBOInput. A resume answer's own client_fs wins.
    client_fs: dict[str, Any] | None = None


# The deal-assumption boxes manifest — the ONE source of both the engine-input
# defaults and the operator-facing form (the dashboard renders this verbatim,
# so the form follows the skill, not the other way around). Keys mirror
# engine/templates/templates.yaml `lbo` inputs; units per LBOInput docstring.
_BOXES_MANIFEST: list[dict[str, Any]] = [
    # ── Deal-specific — no honest default exists, operator must fill ────────
    {"key": "acq_ebitda",        "label": "Acquisition (structuring) EBITDA", "type": "number", "unit": "m",   "required": True,
     "help": "EV basis only — the operating model drives debt sizing separately"},
    {"key": "acq_multiple",      "label": "Entry multiple",                   "type": "number", "unit": "x",   "required": True},
    {"key": "debt_ebitda",       "label": "Leverage (debt / EBITDA)",         "type": "number", "unit": "x",   "required": True,
     "help": "Sized on the operating-model EBITDA at EDATE(first post-acq FYE, -12)"},
    {"key": "existing_net_debt", "label": "Existing net debt",                "type": "number", "unit": "m",   "required": True},
    {"key": "first_fye",         "label": "First historical FYE",             "type": "date",                  "required": True,
     "help": "Drives the 9-period timeline; periods must match the Client_FS dates"},
    {"key": "fye_post_acq",      "label": "First FYE after acquisition",      "type": "date",                  "required": True},
    {"key": "acq_date",          "label": "Acquisition date",                 "type": "date",                  "required": True,
     "help": "Sets the stub vs the post-acq FYE"},
    # ── Conventions — defaulted, editable ────────────────────────────────────
    {"key": "project_name",      "label": "Project name",                     "type": "text",                  "required": True,  "default": None},
    {"key": "currency",          "label": "Currency",                         "type": "select", "options": ["GBP", "EUR", "USD"], "required": True, "default": "GBP"},
    {"key": "scenario_provider", "label": "Scenario provider",                "type": "text",                  "required": True,  "default": "Management"},
    {"key": "ebitda_basis",      "label": "EBITDA basis",                     "type": "select", "options": ["Adj", "Mgmt", "Reported"], "required": True, "default": "Adj"},
    {"key": "tax_rate",          "label": "Tax rate",                         "type": "number", "unit": "dec", "required": True,  "default": 0.25},
    {"key": "holding_period",    "label": "Holding period",                   "type": "int",    "unit": "yrs", "required": True,  "default": 5,
     "help": "Engine horizon caps the usable hold (~5 for a mid-window acquisition)"},
    {"key": "ma_fees",           "label": "M&A fees",                         "type": "number", "unit": "m",   "required": True,  "default": 2},
    {"key": "step_multiple",     "label": "Sensitivity step",                 "type": "number", "unit": "x",   "required": True,  "default": 0.5},
    {"key": "min_equity",        "label": "Minimum equity",                   "type": "number", "unit": "dec", "required": True,  "default": 0.35},
    {"key": "tla_split",         "label": "TLA share of term debt",           "type": "number", "unit": "dec", "required": True,  "default": 1},
    {"key": "rcf_quantum",       "label": "RCF quantum",                      "type": "number", "unit": "m",   "required": True,  "default": 25},
    {"key": "rcf_switch",        "label": "RCF drawn at close",               "type": "select", "options": [0, 1], "required": True, "default": 0},
    {"key": "pref_interest",     "label": "Preferred interest",               "type": "number", "unit": "dec", "required": True,  "default": 0.10},
]


def _boxes_manifest(deal_name: str, prefill: dict[str, Any]) -> list[dict[str, Any]]:
    """The manifest with per-deal defaults resolved: ``project_name`` defaults
    to the deal name; any caller ``prefill`` overrides a field's default.
    ``options`` lists are copied too — the returned manifest is handed to the
    suspension store / JSON layer and must never alias the module global
    (codex wiring review, NIT)."""
    out: list[dict[str, Any]] = []
    for field in _BOXES_MANIFEST:
        f = dict(field)
        if "options" in f:
            f["options"] = list(f["options"])
        if f["key"] == "project_name" and f.get("default") is None:
            f["default"] = deal_name
        if f["key"] in prefill:
            f["default"] = prefill[f["key"]]
        out.append(f)
    return out


def _intake_prompt(deal_name: str, note: str = "") -> str:
    head = (
        f"LBO intake for {deal_name!r}: provide the deal-assumption boxes "
        "(see options for the field manifest). Answer shape: "
        '{"boxes": {<key>: <value>, ...}, "citations": [{...}, ...]}. '
        "NB: the engine runs on the operating model currently in the template's "
        "Client_FS sheet — build/refresh it upstream for a real deal."
    )
    return f"{note}\n\n{head}" if note else head


def _resume_intake(rc: Any) -> LBOOutput:
    """The resume leg: assemble a full LBOInput from the suspension checkpoint +
    the operator's boxes, re-suspending (same run, fresh token) on a fixable
    answer (missing citations / failed validation) instead of burning the run."""
    state = rc.state or {}
    deal_name = state.get("deal_name", "unknown")
    prefill = state.get("prefill") or {}
    answer = rc.input if isinstance(rc.input, dict) else {}
    boxes = answer.get("boxes") if isinstance(answer.get("boxes"), dict) else {}
    citations = answer.get("citations") if isinstance(answer.get("citations"), list) else []
    # Operating model (feat/lbo-client-fs). Default = the block carried from
    # the intake fire / a prior resume; the answer's KEY PRESENCE is honoured
    # explicitly (codex review 2026-06-10 SEV-2 — a malformed answer must
    # never silently fall back to stale carried state):
    #   answer has no "client_fs"          → keep the carried block (if any)
    #   answer "client_fs": {...}          → the answer wins
    #   answer "client_fs": null           → CLEAR the carried block
    #   answer "client_fs": anything else  → fixable re-suspend (carried kept)
    client_fs = state.get("client_fs")

    def _resuspend(note: str) -> SkillSuspended:
        # Carry the operator's SUBMITTED boxes forward as the next manifest's
        # defaults (merged over the original prefill) so a fixable answer
        # survives modal close / pending pickup — without this, a validation
        # re-suspend silently reverts the form to pristine defaults (codex
        # wiring review, CONCERN 2). Scalars only — the manifest renders them
        # into form inputs. Citations are NOT carried: the source note is
        # cheap to retype and must be re-affirmed with the corrected boxes.
        carried = {
            **prefill,
            **{k: v for k, v in boxes.items() if isinstance(v, (str, int, float))},
        }
        # Carry the operating model too (same don't-lose-the-answer rationale):
        # a client_fs submitted on a fixable resume survives the re-suspend; an
        # explicit null clear removes any previously carried block.
        new_state = {**state, "prefill": carried}
        new_state.pop("client_fs", None)
        if client_fs is not None:
            new_state["client_fs"] = client_fs
        return SkillSuspended(
            _intake_prompt(deal_name, note=note),
            state=new_state,
            options=_boxes_manifest(deal_name, carried),
            timeout_s=INTAKE_TTL_S,
        )

    if "client_fs" in answer:
        answer_client_fs = answer["client_fs"]
        if answer_client_fs is None or isinstance(answer_client_fs, dict):
            client_fs = answer_client_fs
        else:
            raise _resuspend(
                "client_fs must be a JSON object (or null to clear the carried "
                f"operating model) — got {type(answer_client_fs).__name__}.")

    if not citations:
        raise _resuspend("Citations are required — every assumption needs a source.")

    merged: dict[str, Any] = {}
    for field in _BOXES_MANIFEST:
        if field.get("default") is not None:
            merged[field["key"]] = field["default"]
    merged["project_name"] = deal_name
    merged.update(prefill)
    merged.update(boxes)
    merged.update({
        "deal_name": deal_name,
        "workspace_type": state.get("workspace_type", "project"),
        "workspace_name": state.get("workspace_name", "default"),
        "workspace_sensitivity": state.get("workspace_sensitivity", "confidential"),
        "citations": citations,
    })
    if client_fs is not None:
        merged["client_fs"] = client_fs

    try:
        inputs = LBOInput(**merged)
    except ValidationError as e:
        issues = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:6]
        )
        raise _resuspend(f"Boxes failed validation — fix and resubmit: {issues}")

    return _run_engine(inputs)


def _run_engine(inputs: LBOInput) -> LBOOutput:
    """The engine call with the engine→HTTP error contract (shared by the
    one-shot and the intake-resume paths): EngineTimeout→504,
    ValidationGateFailed/EngineRunFailed→502, LBOSkillError→500."""
    try:
        return run_lbo(inputs)
    except EngineTimeout as e:
        raise HTTPException(status_code=504, detail=f"engine wall-clock exceeded: {e}")
    except ValidationGateFailed as e:
        # Iron Law fired post-run: returns suppressed.
        raise HTTPException(status_code=502, detail=f"validation gate failed: {e}")
    except EngineRunFailed as e:
        # Engine exited non-zero — includes the validation-gate failure path (exit 2).
        raise HTTPException(status_code=502, detail=f"engine run failed: {e}")
    except LBOSkillError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lbo", response_model=LBOOutput)
@anton_skill("lbo")
def run_workflow_lbo(
    # left_to_right pins the union: a payload carrying mode:"intake" ALWAYS
    # resolves to LBOIntakeInput (even if it also happens to satisfy the full
    # LBOInput shape, whose validation ignores the extra `mode` key) — smart-
    # union scoring must never route an intake fire into the engine path
    # (codex wiring review, CONCERN 1). A modeless full payload fails
    # LBOIntakeInput (mode required) and falls through to LBOInput unchanged.
    inputs: Annotated[
        Union[LBOIntakeInput, LBOInput],
        Field(union_mode="left_to_right"),
    ],
) -> LBOOutput:
    """Run the LBO skill. See module docstring for the gate / error contract.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks`` workspace-scope/MNPI gate →
    403, lifecycle, dedup) AND the #76 deliverable→vault capture. lbo declares
    ``captures_to_vault``, so on a completed run the wrapper emits the SAME
    operator-gated proposal the route used to hand-roll — it passes
    ``result.model_dump()`` (identical payload), with ``meta.sensitivity`` and
    ``VAULT``, under the default ``capture=True``. The body is now just input
    validation + the engine call. Behaviour-identical: the citations 422 and the
    engine→HTTP map (504/502/502/500) are unchanged, passed through as inner
    body HTTPExceptions; ``LBOInput`` carries workspace_type/name/sensitivity so
    the wrapper's ``_extract_workspace`` reads the call tier for the MNPI gate.

    PRECEDENCE (operator-accepted 2026-06-08): the wrapper runs the governance
    gate (workspace-scope / MNPI → 403) in ``tool_call_hooks.__enter__``, i.e.
    BEFORE this body. Pre-migration the citations 422 fired before the gate, so a
    request that is BOTH malformed (no citations) AND governance-refused (general
    workspace / MNPI) now returns 403 instead of 422. This is the intended
    outer-jacket semantics (govern before validate skill-specific input); it only
    affects doubly-invalid requests no real caller sends, and the single-fault
    contracts are unchanged (citations-only → 422; scope/MNPI-only → 403).

    #lbo-dashboard-wiring (2026-06-09) — the route now ALSO accepts an
    intake-mode payload (:class:`LBOIntakeInput`, discriminated by
    ``mode: "intake"``): the body suspends with the deal-assumption boxes
    manifest (#63), and ``POST /api/skills/{run_id}/resume`` re-invokes this
    SAME body with ``inputs=None`` + a :class:`ResumeContext` — hence the
    resume branch FIRST. A fixable resume answer (missing citations / failed
    box validation) RE-SUSPENDS (same run_id, fresh token) instead of burning
    the run. The one-shot full-LBOInput path is byte-for-byte unchanged."""
    # Resume re-invocation (#63): the original request is None — continue from
    # the suspension checkpoint + the operator's boxes.
    rc = current_resume()
    if rc is not None:
        return _resume_intake(rc)

    # Intake-mode fire: no engine inputs yet — suspend and ask for the boxes.
    if isinstance(inputs, LBOIntakeInput):
        state = {
            "deal_name": inputs.deal_name,
            "workspace_type": inputs.workspace_type,
            "workspace_name": inputs.workspace_name,
            "workspace_sensitivity": inputs.workspace_sensitivity,
            "deal_context": inputs.deal_context,
            "prefill": inputs.prefill,
        }
        # Operating model handed in at fire time (e.g. by the chat-agent leg)
        # rides the checkpoint to the resume; a resume answer's block wins.
        if inputs.client_fs is not None:
            state["client_fs"] = inputs.client_fs
        raise SkillSuspended(
            _intake_prompt(inputs.deal_name),
            state=state,
            options=_boxes_manifest(inputs.deal_name, inputs.prefill),
            timeout_s=INTAKE_TTL_S,
        )

    # Citations required — every assumption needs a source (input validation).
    if not inputs.citations:
        raise HTTPException(
            status_code=422,
            detail="LBO inputs require citations (every assumption needs a source)",
        )

    # Engine call. The engine→HTTP error contract is preserved as inner body
    # excepts — the wrapper passes a body-raised HTTPException straight through
    # (the skill chooses its own status). On success the wrapper boxes the
    # result, records ctx.result, and fires the #76 capture.
    return _run_engine(inputs)
