"""lbo-intake-agent bridge route (agent-leg Phase 2, Option C).

``POST /api/workflows/lbo-intake-agent`` — the governed, local-first agent leg
of the LBO intake: read deal documents deterministically, judge them via the
gated ``llm()`` helper (confidential ⇒ local; an operator
``#llm-routing-override`` window lifts judgment to the claude lane per fire),
clarify via the #63 suspend loop, and terminate by suspending into the
STANDARD LBO boxes manifest with ``prefill`` + per-box citations + an optional
``client_fs`` block — i.e. the run BECOMES an lbo intake: the operator
confirms boxes (modal or API) and the resume delegates to
``lbo._resume_intake`` so LBOInput assembly, validation re-suspends, the
engine call and the error contract are byte-identical to ``/lbo`` intake mode.

Stage machine (``state["stage"]``):

  fire            → extract → digest×N → synthesis → parse (one repair try)
  "clarify"       → one round of open-question answers; manifest-keyed answers
                    merge into prefill (cited ``operator-resume:<date>``),
                    free-text answers append to the boxes note
  "boxes"         → agent citations merged UNDER the operator's answer, then
                    ``_resume_intake`` (operator always wins; fixable answers
                    re-suspend with stage preserved)

Degrade-don't-burn: a judgment that cannot be parsed after one repair attempt
falls through to the boxes suspension with EMPTY prefill + an explanatory note
— the operator gets the manual form, never a dead run. ``llm()`` refusals
(budget / sensitivity / cap) map to 403 — the fire is cheap to re-issue once
the gate condition is resolved.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from routines.api.routes.lbo import (
    INTAKE_TTL_S,
    _BOXES_MANIFEST,
    _boxes_manifest,
    _intake_prompt,
    _resume_intake,
)
from routines.skills._runtime import llm_gateway
from routines.skills._runtime.anton_skill import (
    SkillSuspended,
    anton_skill,
    current_resume,
)
from routines.skills._runtime.llm_gateway import SkillLLMRefused
from routines.skills.lbo.scripts.lbo import ClientFSBlock, LBOOutput
from routines.skills.lbo_intake_agent.scripts.intake_agent import (
    JudgmentParseError,
    digest_prompt,
    parse_judgment,
    read_document,
    repair_prompt,
    synthesis_prompt,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)

MAX_DOCS = 8
MAX_CLARIFY_QUESTIONS = 8
PROVIDED_VIA = "lbo-intake-agent"
SKILL_NAME = "lbo-intake-agent"
# The synthetic manifest field that surfaces an agent-transcribed client_fs
# block for explicit operator acknowledgement (codex slice-2 SEV-2: a hidden
# engine input is not operator-gated). NOT an engine box — stripped from the
# answer before LBOInput assembly.
CLIENT_FS_ACK_KEY = "client_fs_apply"

# Fallback doc roots — the SAME values SKILL.md declares as capabilities.fs_roots.
# The registry copy is authoritative; this is the fail-closed default if the
# registry lookup ever errors.
_FALLBACK_DOC_ROOT_GLOBS = ("<workspace-root>/**", "<workspace-root>/**")


def _doc_roots() -> list[Path]:
    """Allowed document roots = the skill's declared ``capabilities.fs_roots``
    (registry = single source of truth; a glob like ``<workspace-root>/**``
    truncates at the wildcard). Registry trouble → the declared defaults —
    fail-closed, never fail-open."""
    try:
        from routines.skills.registry import scan
        md = scan().get(SKILL_NAME)
        globs = list(md.capabilities.fs_roots) if md else []
    except Exception:  # noqa: BLE001 — fall back to the declared defaults
        globs = []
    if not globs:
        globs = list(_FALLBACK_DOC_ROOT_GLOBS)
    roots: list[Path] = []
    for g in globs:
        base = g.split("*", 1)[0].rstrip("/\\")
        if base:
            roots.append(Path(base))
    return roots


def _doc_path_refusal(raw: str, roots: list[Path]) -> Optional[str]:
    """``None`` when the path is admissible, else the refusal reason (codex
    slice-2 SEV-2: doc_paths must be scoped to the declared fs_roots — doc
    content flows to llm() and, under an override window, to a cloud lane)."""
    if raw.startswith(("\\\\", "//")):
        return "UNC/network paths are not allowed"
    try:
        rp = Path(raw).resolve()
    except (OSError, ValueError):
        return "path could not be resolved"
    for root in roots:
        try:
            rp.relative_to(root.resolve())
            return None
        except (ValueError, OSError):
            continue
    return "outside the skill's allowed document roots (capabilities.fs_roots)"


class LBOIntakeAgentInput(BaseModel):
    """Fire payload. Workspace fields mirror LBOIntakeInput — the wrapper reads
    them for the central scope/MNPI gate on the first call (on resume,
    governance comes from the suspension row)."""

    deal_name: Annotated[str, Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9 _-]*$",
    )]
    workspace_type: Literal["project", "bd", "general"] = "project"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "confidential"
    doc_paths: Annotated[list[str], Field(min_length=1, max_length=MAX_DOCS)]
    deal_context: str = ""


def _llm(prompt: str, *, task_type: str = "synthesis") -> Any:
    """Governed llm() with the route's refusal contract: a blocked call is an
    operator-condition (budget window, missing override, cap), not a bug."""
    try:
        return llm_gateway.llm(prompt, task_type=task_type)
    except SkillLLMRefused as e:
        raise HTTPException(status_code=403, detail=f"llm call refused: {e}")


def _deal_state(src: dict[str, Any]) -> dict[str, Any]:
    """The deal/workspace keys every suspension carries — the SAME key names
    ``lbo._resume_intake`` reads from its checkpoint."""
    return {
        "deal_name": src["deal_name"],
        "workspace_type": src["workspace_type"],
        "workspace_name": src["workspace_name"],
        "workspace_sensitivity": src["workspace_sensitivity"],
        "deal_context": src.get("deal_context", ""),
    }


def _boxes_suspension(
    deal: dict[str, Any],
    *,
    prefill: dict[str, Any],
    agent_citations: list[dict[str, Any]],
    client_fs: Optional[dict[str, Any]],
    notes: list[str],
) -> SkillSuspended:
    """The terminal suspension — the standard boxes manifest, agent-annotated.

    State carries exactly what ``_resume_intake`` expects (deal/workspace keys,
    ``prefill``, optional ``client_fs``) plus the agent's citation rows and the
    stage marker; the manifest options additionally carry ``source`` /
    ``provided_via`` per prefilled field for the dashboard's provenance display
    (slice 3) — unknown manifest keys are inert to the current modal."""
    state = {
        **_deal_state(deal),
        "stage": "boxes",
        "prefill": prefill,
        "agent_citations": agent_citations,
    }
    if client_fs is not None:
        state["client_fs"] = client_fs
    src_map = {c["box"]: c for c in agent_citations if c.get("box")}
    options = []
    for f in _boxes_manifest(deal["deal_name"], prefill):
        ann = src_map.get(f["key"])
        if ann:
            f = {**f, "source": ann["source"], "provided_via": ann.get("provided_via", PROVIDED_VIA)}
        # Explicit stage marker (codex slice-3 SEV-2): the dashboard dispatches
        # clarify vs boxes on THIS field, never on prompt wording.
        options.append({**f, "stage": "boxes"})
    if client_fs is not None:
        # Surface the transcribed operating model for EXPLICIT acknowledgement
        # (codex slice-2 SEV-2): default "discard" — the agent's block touches
        # the engine ONLY if the operator actively applies it.
        n_rows = len(client_fs.get("rows") or {})
        dates = client_fs.get("dates") or []
        span = f"{dates[0]} → {dates[-1]}" if dates else "?"
        options.append({
            "key": CLIENT_FS_ACK_KEY,
            "label": "Apply agent-transcribed operating model (Client_FS)",
            "type": "select", "options": ["discard", "apply"],
            "required": True, "default": "discard",
            "help": f"The agent transcribed a {n_rows}-row, 10-period operating "
                    f"model ({span}) from the documents. 'apply' writes it into "
                    "the template's Client_FS sheet for this run; 'discard' "
                    "(default) runs on the sheet as-is. Operator-gated proposal "
                    "— review before applying.",
            "provided_via": PROVIDED_VIA,
        })
    note = " ".join(
        [f"lbo-intake-agent prefilled {len(prefill)} box(es)."] + notes
    ).strip()
    return SkillSuspended(
        _intake_prompt(deal["deal_name"], note=note),
        state=state,
        options=options,
        timeout_s=INTAKE_TTL_S,
    )


def _clarify_suspension(
    deal: dict[str, Any],
    *,
    questions: list[dict[str, str]],
    prefill: dict[str, Any],
    agent_citations: list[dict[str, Any]],
    client_fs: Optional[dict[str, Any]],
    notes: list[str],
) -> SkillSuspended:
    """One clarification round. Options reuse the manifest field shape so the
    dashboard can render them as a form; ``required: False`` throughout —
    unanswered questions simply stay open in the boxes note."""
    questions = questions[:MAX_CLARIFY_QUESTIONS]
    state = {
        **_deal_state(deal),
        "stage": "clarify",
        "prefill": prefill,
        "agent_citations": agent_citations,
        "open_questions": questions,
        "notes": notes,
    }
    if client_fs is not None:
        state["client_fs"] = client_fs
    options = [
        # stage marker: see _boxes_suspension (codex slice-3 SEV-2).
        {"key": q["key"], "label": q["key"], "type": "text",
         "required": False, "help": q["question"], "stage": "clarify"}
        for q in questions
    ]
    prompt = (
        f"lbo-intake-agent needs clarification on {len(questions)} item(s) for "
        f"{deal['deal_name']!r} before prefilling the deal boxes — answer what "
        "you can; blanks stay open. Answer shape: "
        '{"answers": {<key>: <value>, ...}}.'
    )
    return SkillSuspended(
        prompt, state=state, options=options, timeout_s=INTAKE_TTL_S,
    )


def _validated_client_fs(raw: Optional[dict[str, Any]], notes: list[str]) -> Optional[dict[str, Any]]:
    """Validate a transcribed operating-model block against ClientFSBlock —
    invalid blocks are DROPPED with a note (the engine then runs on whatever
    sits in the template's Client_FS sheet), never forwarded to fail later as
    an engine exit-1."""
    if raw is None:
        return None
    try:
        rows = raw.get("rows")
        coerced = {
            **raw,
            "rows": {int(k): v for k, v in rows.items()} if isinstance(rows, dict) else rows,
        }
        return ClientFSBlock(**coerced).model_dump()
    except (ValidationError, ValueError, TypeError, AttributeError) as e:
        notes.append(
            f"A transcribed client_fs block was dropped (failed validation: "
            f"{str(e)[:160]}); the run will use the template's Client_FS sheet."
        )
        return None


def _resume_clarify(rc: Any) -> LBOOutput:
    """Merge the operator's clarification answers, then raise the terminal
    boxes suspension (same run, fresh token)."""
    state = rc.state or {}
    answer = rc.input if isinstance(rc.input, dict) else {}
    answers = answer.get("answers") if isinstance(answer.get("answers"), dict) else {}

    prefill = dict(state.get("prefill") or {})
    citations = list(state.get("agent_citations") or [])
    notes = list(state.get("notes") or [])
    manifest_keys = {f["key"] for f in _BOXES_MANIFEST}
    today = date.today().isoformat()

    answered = unanswered = 0
    for q in state.get("open_questions") or []:
        key = q.get("key")
        v = answers.get(key)
        if v in (None, ""):
            unanswered += 1
            continue
        answered += 1
        if key in manifest_keys and isinstance(v, (str, int, float)):
            prefill[key] = v
            citations.append({
                "box": key, "source": f"operator-resume:{today}",
                "quote": "", "provided_via": "operator",
            })
        else:
            notes.append(f"Operator on {q['question'][:80]!r}: {str(v)[:160]}")
    if unanswered:
        notes.append(f"{unanswered} clarification(s) left open.")

    raise _boxes_suspension(
        state,
        prefill=prefill,
        agent_citations=citations,
        client_fs=state.get("client_fs"),
        notes=notes,
    )


def _route_resume(rc: Any) -> LBOOutput:
    """Dispatch a resume by checkpoint stage — EXPLICITLY (codex slice-2
    SEV-3: a corrupt/unknown stage must refuse, not fall through into the
    boxes resume with missing state)."""
    state = rc.state or {}
    stage = state.get("stage")
    if stage == "clarify":
        return _resume_clarify(rc)
    if stage != "boxes":
        raise HTTPException(
            status_code=409,
            detail=f"unrecognised suspension stage {stage!r} for "
                   f"{SKILL_NAME} — re-fire the intake",
        )
    return _resume_boxes(rc)


def _same_value(a: Any, b: Any) -> bool:
    """Value equality across the JSON/number boundary (18 == 18.0)."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _resume_boxes(rc: Any) -> LBOOutput:
    """The terminal resume: reconcile agent citations against the operator's
    final values, honour the client_fs acknowledgement, then delegate to the
    standard ``lbo._resume_intake`` (operator always wins)."""
    state = dict(rc.state or {})
    answer = rc.input if isinstance(rc.input, dict) else {}
    boxes = answer.get("boxes") if isinstance(answer.get("boxes"), dict) else {}
    prefill = state.get("prefill") or {}

    # client_fs acknowledgement (codex slice-2 SEV-2): the agent's block is an
    # operator-gated PROPOSAL — anything other than an explicit "apply" strips
    # it before the engine ever sees it. The ack key is synthetic — never a box.
    ack = boxes.get(CLIENT_FS_ACK_KEY)
    boxes = {k: v for k, v in boxes.items() if k != CLIENT_FS_ACK_KEY}
    if state.get("client_fs") is not None and ack != "apply":
        state.pop("client_fs", None)

    # Citation reconciliation (codex slice-2 SEV-2): an agent citation row only
    # survives if the operator did NOT override that box's value — a changed
    # value needs the operator's own source, the agent's quote no longer
    # supports it. The downstream citations gate then re-suspends naturally if
    # nothing is left.
    agent_rows = [
        c for c in (state.get("agent_citations") or [])
        if not (
            c.get("box") in boxes
            and c.get("box") in prefill
            and not _same_value(boxes[c["box"]], prefill[c["box"]])
        )
    ]
    op_citations = answer.get("citations") if isinstance(answer.get("citations"), list) else []
    merged = {**answer, "boxes": boxes, "citations": [*op_citations, *agent_rows]}
    return _resume_intake(SimpleNamespace(
        run_id=getattr(rc, "run_id", None), state=state, input=merged,
    ))


@router.post("/lbo-intake-agent", response_model=LBOOutput)
@anton_skill("lbo-intake-agent")
def run_workflow_lbo_intake_agent(
    inputs: LBOIntakeAgentInput,
) -> LBOOutput:
    """Run the LBO intake agent. See module docstring for the stage machine."""
    # Resume re-invocation (#63): route by checkpoint stage.
    rc = current_resume()
    if rc is not None:
        return _route_resume(rc)

    deal = _deal_state(inputs.model_dump())

    # 1. Deterministic extraction. Paths must resolve under the skill's
    #    declared fs_roots (codex slice-2 SEV-2); unreadable/refused paths are
    #    warnings, not failures; ALL unreadable → 422 (nothing to judge).
    roots = _doc_roots()
    docs, doc_notes = [], []
    for p in inputs.doc_paths:
        refusal = _doc_path_refusal(p, roots)
        if refusal:
            doc_notes.append(f"{Path(p).name or p}: {refusal}.")
            continue
        d = read_document(p)
        if d["error"]:
            doc_notes.append(f"{d['name']}: {d['error']}.")
        else:
            docs.append(d)
    if not docs:
        raise HTTPException(
            status_code=422,
            detail="none of the doc_paths could be read — " + " ".join(doc_notes),
        )

    # 2. Governed judgment: per-doc digest → one synthesis (strict JSON).
    digests = [
        {"name": d["name"], "digest": _llm(digest_prompt(d)).text}
        for d in docs
    ]
    synth = _llm(synthesis_prompt(
        inputs.deal_name, inputs.deal_context, digests, _BOXES_MANIFEST,
    ))
    try:
        try:
            judgment = parse_judgment(synth.text, _BOXES_MANIFEST)
        except JudgmentParseError as e:
            repaired = _llm(repair_prompt(synth.text, str(e)))
            judgment = parse_judgment(repaired.text, _BOXES_MANIFEST)
    except JudgmentParseError as e:
        # Degrade to the manual form — never burn the run on a parse failure.
        log.warning("lbo-intake-agent judgment unparseable after repair: %s", e)
        raise _boxes_suspension(
            deal, prefill={}, agent_citations=[], client_fs=None,
            notes=doc_notes + [
                "The agent could not produce a structured judgment from the "
                "documents — fill the boxes manually.",
            ],
        )

    notes = list(doc_notes)
    if judgment.notes:
        notes.append(f"Agent notes: {judgment.notes}")
    client_fs = _validated_client_fs(judgment.client_fs, notes)
    prefill = {k: v["value"] for k, v in judgment.boxes.items()}
    citations = [
        {"box": k, "source": v["source"], "quote": v["quote"],
         "provided_via": PROVIDED_VIA}
        for k, v in judgment.boxes.items()
    ]

    # 3. Clarify once if the judgment raised questions; else straight to boxes.
    if judgment.open_questions:
        raise _clarify_suspension(
            deal,
            questions=judgment.open_questions,
            prefill=prefill,
            agent_citations=citations,
            client_fs=client_fs,
            notes=notes,
        )
    raise _boxes_suspension(
        deal,
        prefill=prefill,
        agent_citations=citations,
        client_fs=client_fs,
        notes=notes,
    )
