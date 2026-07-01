"""CIMTriage crew — `/triage <pdf_path>` (#32, autonomous-crews §3).

Reads a CIM/teaser and flags what matters: red flags, opportunities, key
metrics, and buyer DD questions, then a 1-page memo. Six roles:

    Ingestor ──▶ ┌─ RedFlags ─────┐
                 ├─ Opportunities ┤
                 ├─ KeyMetrics ───┼──▶ Summariser (fan-in JOIN) ──▶ memo
                 └─ QuestionsForMgmt ┘

ALWAYS local Ollama (`qwen3:14b` synthesis + `qwen3:8b` analysts) — CIM inputs default MNPI per the vault
constitution §4 / [no-mnpi-to-cloud]; the bridge sensitivity guard refuses 403
on any attempt to route this verb to a cloud lane (registry MNPI lock), so this
module is authored to NEVER reach an external endpoint.

Boundary notes (why this crew differs from the spec sketch — #32 build):
  * The crew venv has NO pdf library and NO llama_index, so the spec's
    "Ingestor chunks the PDF via metagpt.rag, builds a vector index" is not
    buildable here. Instead the BRIDGE extracts the CIM to page-tagged text
    (it owns `pypdf`) and passes the pages in via `CrewInput.args["pages"]`;
    the Ingestor builds a keyword `PageIndex` (`_shared.triage_lib`) and the
    analysts retrieve page-tagged passages from it. Same MNPI containment —
    the text only ever crosses the local stdin pipe.
  * The memo file is NOT written here. The crew venv cannot import
    `routines.shared.write_policy`; the crew returns the memo as a
    `CrewDocument` and the BRIDGE writes it through the central write policy +
    raises the Inbox flag (route worker thread → `routines.crew.artefacts`).

Deployed by `routines/crew/install/install_metagpt.py` from `crews_src/` to
`<repo>\\crews\\`; the repo copy is the single source of truth. Mirrors
`hello_world_crew.py` (the #31 template) — read its docstring first.

Exit codes: 0 = success · 1 = crew ran but errored (structured CrewOutput
status="error" on stdout) · 2 = input parse failure (stderr).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from _shared.boundary import (
    CrewDocument,
    CrewInput,
    CrewOutput,
    RoleLogEntry,
    capture_protocol_stream,
    collect_roles_log,
    read_input,
    write_error,
    write_result,
)
from _shared.ollama_config import (
    build_ollama_llm_for_role,
    model_for_role,
    ollama_structured_chat,
)
from _shared import triage_lib as tl

# Drop loguru's default stderr sink BEFORE any metagpt import — `metagpt.const`
# logs to stderr at import time, and stderr non-empty is a fault signal to the
# bridge (spec §2.4). Same load-bearing ordering as hello_world_crew.
from loguru import logger as _loguru_root

_loguru_root.remove()

from metagpt.actions import Action  # noqa: E402
from metagpt.roles import Role  # noqa: E402
from metagpt.schema import Message  # noqa: E402
from metagpt.team import Team  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Manifest — routines/crew/registry.py mirrors this shape. Keep field names
# identical on both sides.
# ════════════════════════════════════════════════════════════════════════════

# Local-Ollama-only models (CIMs are MNPI). Per-role tiering: the three
# EXTRACTION analyst roles run the schema-constrained structured call (the
# grammar carries the structure) on qwen3:8b (~2x faster). The Ingestor
# (mechanical, no LLM call), the Summariser (memo narrative), AND
# QuestionsForMgmt (8b produced 0 usable DD questions in the 2026-06-15 smoke
# `fe81231a` — generation is 8b's weak spot) stay on qwen3:14b. All local
# Ollama, so tiering never weakens MNPI containment.
TRIAGE_MODEL = "qwen3:14b"          # Summariser / QuestionsForMgmt; Ingestor (unused)
TRIAGE_ANALYST_MODEL = "qwen3:8b"   # the three extraction analyst roles

MANIFEST = {
    "verb": "triage",
    "module": "triage_crew",
    # LOCKED MNPI (load-bearing): the bridge refuses 403 on any override that
    # differs (registry.resolve_crew_sensitivity), so /triage can never be
    # flipped onto a cloud lane. autonomous-crews §3.
    "sensitivity_override": "MNPI",
    "cost_cap_tokens": 100_000,  # smoke-tuned 50k->100k (registry sync; a real CIM hit 50k)
    # Spec design budget is 180s, but a SIX-role crew on a cold qwen3:14b load
    # measured well past that for the 3-role hello_world (99s); 600s is the
    # global wall-clock ceiling and the safe value until a live run is timed.
    # The 100k TOKEN cap is the real cost guarantee; this only bounds hangs.
    # (Flagged for operator tuning — see the #32 SUMMARY.)
    "cost_cap_seconds": 600,
    "roles": ["Ingestor", "RedFlags", "Opportunities", "KeyMetrics",
              "QuestionsForMgmt", "Summariser"],
    "models_default": {
        "Ingestor": TRIAGE_MODEL,                 # mechanical — no LLM call
        "RedFlags": TRIAGE_ANALYST_MODEL,
        "Opportunities": TRIAGE_ANALYST_MODEL,
        "KeyMetrics": TRIAGE_ANALYST_MODEL,
        "QuestionsForMgmt": TRIAGE_MODEL,         # 8b gave 0 questions (smoke fe81231a)
        "Summariser": TRIAGE_MODEL,               # memo narrative
    },
    "description": "Read a CIM/teaser and flag red flags, opportunities, key "
                   "metrics + buyer DD questions. Always local Ollama (MNPI).",
    # #captures-to-vault-crews: documentation mirror of the registry capture
    # block (the BRIDGE reads routines/crew/registry.py, not this dict). Opt-in
    # conclusion capture; MNPI-safe — the proposal + Route-append are LOCAL
    # vault writes only (no egress).
    "captures_to_vault": {
        "target": "Companies/{entity}.md",
        "section": "Triage history",
        "fields": ["high", "med", "low", "opportunities", "questions"],
        "headline": "{entity} triage: {high} high / {med} med red flags, "
                    "{opportunities} opportunities, {questions} DD questions",
    },
}


# ════════════════════════════════════════════════════════════════════════════
# Module-scoped run state (a subprocess == one run; cleared in run_team).
# ════════════════════════════════════════════════════════════════════════════

_ROLES_LOG: list[dict[str, Any]] = []
_INDEX: tl.PageIndex | None = None
_FINDINGS = tl.TriageFindings()
# Which analyses have completed — the fan-in JOIN sentinel. The Summariser
# finalises only when all four are present (the "Coordinator pattern").
_DONE: set[str] = set()
_CTX: dict[str, Any] = {}
_SUMMARY_STATE: dict[str, Any] = {}
# Serialize the analysts' schema-constrained Ollama calls. The four analyst roles
# fire concurrently, but single-GPU Ollama serializes inference anyway and
# grammar-constrained decoding is slow enough that 4 in-flight requests blew the
# per-call urlopen timeout (smoke 2026-06-15: a concurrent call timed out at
# 300s). One at a time keeps each call's urlopen window == its own generation.
# Module-level (3.11 binds the loop on first use); a fresh subprocess per run
# gives a fresh lock — never reset in _reset_state.
_STRUCTURED_LOCK = asyncio.Lock()


def _reset_state() -> None:
    global _INDEX, _FINDINGS
    _ROLES_LOG.clear()
    _INDEX = None
    _FINDINGS = tl.TriageFindings()
    _DONE.clear()
    _CTX.clear()
    _SUMMARY_STATE.clear()


# ════════════════════════════════════════════════════════════════════════════
# Helpers (mirrors hello_world_crew)
# ════════════════════════════════════════════════════════════════════════════


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sensitivity_tier() -> str:
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


def _llm_total_tokens(llm: Any) -> int:
    """Best-effort total-token read from a MetaGPT LLM's cost manager
    (defensive across 0.8.x cost-manager attribute drift; mirrors
    hello_world_crew._llm_total_tokens)."""
    cm = getattr(llm, "cost_manager", None)
    if cm is None:
        return 0
    for attr in ("total_prompt_tokens", "total_completion_tokens"):
        if not hasattr(cm, attr):
            return int(getattr(cm, "total_tokens", 0) or 0)
    return int((cm.total_prompt_tokens or 0) + (cm.total_completion_tokens or 0))


def _redirect_output_streams(run_id: str) -> None:
    """Route everything except protocol envelopes away from the boundary —
    identical contract to hello_world_crew (loguru → file; stray prints →
    sink via capture_protocol_stream). The bridge hard-fails on any stderr or
    any non-JSON stdout line."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", run_id or "")[:64] or "no-run-id"
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        stray = open(  # noqa: SIM115 — lives for the process lifetime
            os.path.join(log_dir, f"{safe_id}.stdout.log"),
            "a", encoding="utf-8", buffering=1,
        )
    except Exception:  # noqa: BLE001
        stray = None
    capture_protocol_stream(stray)
    try:
        from metagpt.logs import logger as mg_logger
        mg_logger.remove()
        mg_logger.add(os.path.join(log_dir, f"{safe_id}.log"), level="INFO")
    except Exception:  # noqa: BLE001
        pass


def _build_triage_llm(role_name: str, llm_cfg: Any) -> Any:
    """Instantiate the role's Ollama LLM, asserting it resolves to the model the
    manifest declares for that role.

    ``model_for_role`` resolves from the convergent ``CrewLLMConfig.models`` map
    FIRST (the #33 generic role→model contract — the manifest's
    ``models_default`` carries the per-role matrix verbatim), then the legacy
    ``model_<role>`` field, then a default. Triage tiers the four analyst roles
    to ``qwen3:8b`` and keeps Ingestor/Summariser on ``qwen3:14b``; we assert the
    resolved model equals the manifest's declared model for the role rather than
    assume — a role rename or a manifest typo that silently changed a model would
    be a quiet correctness regression on an MNPI deliverable. Because the bridge
    builds ``llm_cfg.models`` from the registry matrix, this assert also fires on
    any registry↔MANIFEST drift. Every declared model is a local qwen3 variant,
    so tiering never weakens the (separately lane-guarded) MNPI containment."""
    expected = MANIFEST["models_default"].get(role_name)
    resolved = model_for_role(role_name, llm_cfg)
    if expected is None:
        raise ValueError(
            f"triage role {role_name!r} has no model declared in the manifest "
            f"models_default matrix — every role must be declared (codex SEV-3)"
        )
    if resolved != expected:
        raise ValueError(
            f"triage role {role_name!r} resolved to model {resolved!r}, "
            f"expected {expected!r} per the manifest models_default matrix"
        )
    return build_ollama_llm_for_role(role_name, llm_cfg)


# ════════════════════════════════════════════════════════════════════════════
# Actions
# ════════════════════════════════════════════════════════════════════════════


class _MeteredAction(Action):
    """Times the LLM call + stamps ``_last_run_meta`` for the audit walk
    (mirrors hello_world_crew._MeteredAction)."""

    async def _metered_aask(self, prompt: str) -> str:
        ts_start = _iso_now()
        t0 = time.monotonic()
        tokens_before = _llm_total_tokens(self.llm)
        result = await self.llm.aask(prompt)
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": max(0, _llm_total_tokens(self.llm) - tokens_before),
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        return result

    async def _metered_structured(self, prompt: str, schema: dict) -> dict:
        """Schema-constrained structured call (``ollama_structured_chat``). Meters
        timing + tokens into ``_last_run_meta`` exactly like ``_metered_aask`` so
        the audit walk + the Layer-2 cost cap are unchanged — but the token count
        comes from Ollama's eval counters (the direct call bypasses MetaGPT's cost
        manager). base_url + model are read off the role's already-built LLM, so
        the lane/model stay identical to ``_metered_aask``."""
        ts_start = _iso_now()
        t0 = time.monotonic()
        cfg = getattr(self.llm, "config", None)
        base_url = getattr(cfg, "base_url", None) or "http://127.0.0.1:11434/api"
        model = getattr(cfg, "model", None) or TRIAGE_MODEL
        async with _STRUCTURED_LOCK:  # serialise — see _STRUCTURED_LOCK note
            data, tokens = await ollama_structured_chat(base_url, model, prompt, schema)
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": tokens,
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        return data

    def _stamp_mechanical(self, ts_start: str, t0: float, *, skip_log: bool = False) -> None:
        """Record a non-LLM action's timing (0 tokens) — used by the Ingestor
        and the Summariser's partial no-op acts."""
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": 0,
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
            "skip_log": skip_log,
        }


class IngestPages(_MeteredAction):
    """Build the in-memory page index from the bridge-extracted pages. NON-LLM
    — purely mechanical (the spec's `metagpt.rag` vector index is replaced by a
    keyword `PageIndex`; see the module docstring)."""

    name: str = "IngestPages"

    async def run(self, with_messages: list[Message]) -> str:
        global _INDEX
        ts_start, t0 = _iso_now(), time.monotonic()
        pages = tl.normalise_pages(_CTX.get("pages") or [])
        _INDEX = tl.PageIndex(pages)
        _CTX["entity"] = tl.infer_entity(
            pages, explicit=_CTX.get("entity_arg"), fallback=_CTX.get("pdf_stem"),
        )
        self._stamp_mechanical(ts_start, t0)
        # COUNT-ONLY return (integration: operator decision 1 / [[crew-action-
        # return-audit-leak]]): this string becomes the role's audit
        # output_summary, so it must NOT carry the entity (deal) name. The entity
        # flows via the _CTX module global to the analysts/Summariser; it already
        # (necessarily) appears in the artefact path + the operator-facing
        # CrewOutput.summary, so dropping it here removes gratuitous duplication.
        return f"IngestComplete: {len(pages)} page(s) indexed"


class ScanRedFlags(_MeteredAction):
    """Scan for the six red-flag classes; emit schema-constrained red-flag JSON."""

    name: str = "ScanRedFlags"

    PROMPT_TEMPLATE: str = """\
You are a private-equity diligence analyst triaging a confidential information \
memorandum (CIM). Using ONLY the page-tagged extracts below, identify the RED \
FLAGS a buyer must investigate, across these classes:
  - customer concentration / revenue reliance
  - leverage / net debt / covenants
  - declining or compressing margins
  - related-party transactions
  - governance issues / control weaknesses / litigation
  - audit qualifications / going-concern / restatements

For each red flag give: a severity (high, med, or low); the page number you saw \
it on (from the [page N] tag, or null if unclear); and a one-sentence factual \
CLAIM grounded in the text. Do not invent figures. If there are genuinely no red \
flags, return an empty list.

Page-tagged extracts:
{passages}"""

    async def run(self, with_messages: list[Message]) -> str:
        passages = tl.format_passages(
            _INDEX.retrieve(tl.RED_FLAG_TERMS) if _INDEX else []
        )
        data = await self._metered_structured(
            self.PROMPT_TEMPLATE.format(passages=passages), tl.RED_FLAGS_SCHEMA)
        _FINDINGS.red_flags = tl.red_flags_from_json(data)
        _DONE.add("red_flags")
        counts = _FINDINGS.severity_counts()
        return (f"RedFlagsDone: {len(_FINDINGS.red_flags)} flag(s) "
                f"({counts['high']} high / {counts['med']} med / {counts['low']} low)")


class ScanOpportunities(_MeteredAction):
    """Scan for upside; emit schema-constrained opportunity JSON."""

    name: str = "ScanOpportunities"

    PROMPT_TEMPLATE: str = """\
You are a private-equity analyst triaging a CIM. Using ONLY the page-tagged \
extracts below, identify OPPORTUNITIES / upside drivers, across: total \
addressable market (TAM), growth segments, recent investments / capex, \
geographic expansion, and new product lines.

For each opportunity give: the page number (from the [page N] tag, or null if \
unclear) and a one-sentence factual CLAIM grounded in the text. Do not invent \
figures. If none are evident, return an empty list.

Page-tagged extracts:
{passages}"""

    async def run(self, with_messages: list[Message]) -> str:
        passages = tl.format_passages(
            _INDEX.retrieve(tl.OPPORTUNITY_TERMS) if _INDEX else []
        )
        data = await self._metered_structured(
            self.PROMPT_TEMPLATE.format(passages=passages), tl.OPPORTUNITIES_SCHEMA)
        _FINDINGS.opportunities = tl.opportunities_from_json(data)
        _DONE.add("opportunities")
        return f"OpportunitiesDone: {len(_FINDINGS.opportunities)} item(s)"


class ExtractKeyMetrics(_MeteredAction):
    """Extract reported KPIs as schema-constrained JSON. EXTRACTION ONLY —
    values are quoted as stated, never computed ([no-llm-maths])."""

    name: str = "ExtractKeyMetrics"

    PROMPT_TEMPLATE: str = """\
You are a private-equity analyst triaging a CIM. Using ONLY the page-tagged \
extracts below, EXTRACT the key financial + operating metrics AS STATED in the \
document: revenue / turnover, EBITDA, margin, growth rate, customer count, and \
number of geographies / countries.

For each: the METRIC name; its VALUE copied EXACTLY as written (with its \
currency, units, and period); and the PAGE number (from the [page N] tag, or \
null if unclear). CRITICAL: do NOT calculate, sum, average, infer, or estimate \
any number. If a metric is not stated in the extracts, give its value as n/a.

Page-tagged extracts:
{passages}"""

    async def run(self, with_messages: list[Message]) -> str:
        passages = tl.format_passages(
            _INDEX.retrieve(tl.KEY_METRIC_TERMS) if _INDEX else []
        )
        data = await self._metered_structured(
            self.PROMPT_TEMPLATE.format(passages=passages), tl.KEY_METRICS_SCHEMA)
        _FINDINGS.key_metrics = tl.key_metrics_from_json(data)
        _DONE.add("key_metrics")
        return f"KeyMetricsDone: {len(_FINDINGS.key_metrics)} metric(s)"


class GenerateQuestions(_MeteredAction):
    """Generate 10-15 buyer DD questions as schema-constrained JSON."""

    name: str = "GenerateQuestions"

    PROMPT_TEMPLATE: str = """\
You are a private-equity analyst preparing to bid on the company described in \
this CIM. Using the page-tagged extracts below as context, write 10 to 15 \
sharp DUE-DILIGENCE QUESTIONS you would put to management before submitting an \
offer — covering the financials, customers, competition, risks, and the \
growth plan. Each item is one complete question.

Page-tagged extracts:
{passages}"""

    async def run(self, with_messages: list[Message]) -> str:
        passages = tl.format_passages(
            _INDEX.retrieve(tl.QUESTION_TERMS, top_k=8) if _INDEX else []
        )
        data = await self._metered_structured(
            self.PROMPT_TEMPLATE.format(passages=passages), tl.QUESTIONS_SCHEMA)
        _FINDINGS.questions = tl.questions_from_json(data)
        _DONE.add("questions")
        return f"QuestionsDone: {len(_FINDINGS.questions)} question(s)"


class SummariseTriage(_MeteredAction):
    """Fan-in: once all four analyses are present, write the 1-page narrative
    and render the full memo. On a partial fire (the Summariser watches all
    four analyst actions, so it may be invoked before the set is complete) it
    is a no-op that emits nothing the framework watches."""

    name: str = "SummariseTriage"

    PROMPT_TEMPLATE: str = """\
You are a private-equity partner. Four analysts have triaged a CIM for \
"{entity}". Write a tight ONE-PAGE narrative summary (no more than ~250 words) \
for the investment committee: what the business is, the headline financials, \
the most material risks, the upside, and a one-line recommended next step. \
Reference figures only as the analysts reported them — do not compute anything.

RED FLAGS:
{red_flags}

OPPORTUNITIES:
{opportunities}

KEY METRICS (as stated in the CIM):
{key_metrics}

Write prose only — the flag list, metric table and question list are appended \
separately, so do not reproduce them verbatim."""

    def _summarise_inputs(self) -> dict[str, str]:
        rf = "\n".join(
            f"- [{f.severity}] (p.{f.page if f.page else '?'}) {f.claim}"
            for f in _FINDINGS.red_flags
        ) or "(none)"
        opp = "\n".join(
            f"- (p.{o.page if o.page else '?'}) {o.claim}"
            for o in _FINDINGS.opportunities
        ) or "(none)"
        km = "\n".join(
            f"- {m.metric}: {m.value} (p.{m.page if m.page else '?'})"
            for m in _FINDINGS.key_metrics
        ) or "(none)"
        return {"red_flags": rf, "opportunities": opp, "key_metrics": km}

    async def run(self, with_messages: list[Message]) -> str:
        # Fan-in guard: only finalise once, and only when all four analyses
        # have landed. A partial / repeat fire is a logged-skipped no-op.
        if _SUMMARY_STATE.get("done") or len(_DONE) < 4:
            ts_start, t0 = _iso_now(), time.monotonic()
            self._stamp_mechanical(ts_start, t0, skip_log=True)
            return ""  # unwatched empty message — harmless

        entity = str(_CTX.get("entity") or "unknown entity")
        prompt = self.PROMPT_TEMPLATE.format(entity=entity, **self._summarise_inputs())
        narrative = await self._metered_aask(prompt)

        date_iso = str(_CTX.get("date") or datetime.now(timezone.utc).date().isoformat())
        run_id = str(_CTX.get("run_id") or "")
        sensitivity = _sensitivity_tier()
        memo = tl.render_memo(
            entity=entity, date_iso=date_iso, run_id=run_id,
            sensitivity=sensitivity,
            page_count=(_INDEX.page_count if _INDEX else 0),
            findings=_FINDINGS, narrative=narrative,
        )
        rel_path = tl.triage_relative_path(entity, date_iso, run_id)
        _SUMMARY_STATE.update({
            "done": True, "memo": memo, "relative_path": rel_path,
            "entity": entity, "narrative": narrative.strip(),
        })
        counts = _FINDINGS.severity_counts()
        # COUNT-ONLY return (integration: operator decision 1 / [[crew-action-
        # return-audit-leak]]): drop the entity (deal) name — this is the role's
        # audit output_summary. The entity is in the artefact path + the
        # operator-facing CrewOutput.summary already; the per-role audit row
        # carries counts only.
        return (f"TriageComplete: {counts['high']}H/{counts['med']}M/"
                f"{counts['low']}L red flags, {len(_FINDINGS.opportunities)} "
                f"opportunities, {len(_FINDINGS.questions)} questions")


# ════════════════════════════════════════════════════════════════════════════
# Roles
# ════════════════════════════════════════════════════════════════════════════


class _MeteredRole(Role):
    """Appends one roles-log entry per executed Action (mirrors
    hello_world_crew._MeteredRole), honouring an action's ``skip_log`` flag so
    the Summariser's partial no-op fires don't clutter the audit."""

    async def _act(self) -> Message:
        todo = self.rc.todo
        msg = await super()._act()
        meta = dict(getattr(todo, "_last_run_meta", None) or {})
        if meta.get("skip_log"):
            return msg
        _ROLES_LOG.append({
            "role": self.name,
            "action": type(todo).__name__,
            "ts_start": meta.get("ts_start", ""),
            "duration_ms": meta.get("duration_ms", 0),
            "token_count": meta.get("token_count", 0),
            "sensitivity": meta.get("sensitivity", _sensitivity_tier()),
            "status": meta.get("status", "ok"),
            "output_summary": str(getattr(msg, "content", "") or "")[:200],
        })
        return msg


# Per-role LLM is assigned BEFORE set_actions so Role._init_action pins it onto
# each Action (assigning after leaves actions on the config-default LLM — the
# #31 real-boundary smoke caught this).


class IngestorRole(_MeteredRole):
    name: str = "Ingestor"
    profile: str = "CIM ingestor"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)  # unused (mechanical) but uniform
        self.set_actions([IngestPages])
        # No _watch — fires on the kickoff UserRequirement message.


class RedFlagsRole(_MeteredRole):
    name: str = "RedFlags"
    profile: str = "Red-flag analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)
        self.set_actions([ScanRedFlags])
        self._watch([IngestPages])


class OpportunitiesRole(_MeteredRole):
    name: str = "Opportunities"
    profile: str = "Opportunity analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)
        self.set_actions([ScanOpportunities])
        self._watch([IngestPages])


class KeyMetricsRole(_MeteredRole):
    name: str = "KeyMetrics"
    profile: str = "Key-metrics analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)
        self.set_actions([ExtractKeyMetrics])
        self._watch([IngestPages])


class QuestionsForMgmtRole(_MeteredRole):
    name: str = "QuestionsForMgmt"
    profile: str = "DD-questions analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)
        self.set_actions([GenerateQuestions])
        self._watch([IngestPages])


class SummariserRole(_MeteredRole):
    """Watches all four analyst actions; finalises once they have all landed.

    The fan-in JOIN is the module-level ``_DONE`` set (the "Coordinator
    pattern" of autonomous-crews §3): every finding emits a fresh message that
    re-triggers this role, and ``_DONE`` accumulates monotonically, so the
    invocation following the FOURTH analyst always sees a complete set and
    produces the memo (SummariseTriage's guard makes earlier fires no-ops)."""

    name: str = "Summariser"
    profile: str = "Triage summariser"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = _build_triage_llm(self.name, llm_cfg)
        self.set_actions([SummariseTriage])
        self._watch([ScanRedFlags, ScanOpportunities, ExtractKeyMetrics,
                     GenerateQuestions])


# ════════════════════════════════════════════════════════════════════════════
# Team run + Layer-2 cost cap
# ════════════════════════════════════════════════════════════════════════════


class CostCapExceeded(RuntimeError):
    """Layer-2 cost-cap trip — surfaces as CrewOutput status="error"."""


def _tokens_so_far() -> int:
    return sum(int(e.get("token_count", 0) or 0) for e in _ROLES_LOG)


def _chat_summary() -> str:
    counts = _FINDINGS.severity_counts()
    entity = str(_SUMMARY_STATE.get("entity") or _CTX.get("entity") or "the target")
    pages = _INDEX.page_count if _INDEX else 0
    return (
        f"Triaged {entity} ({pages} page(s)): "
        f"{counts['high']} high / {counts['med']} med / {counts['low']} low red "
        f"flags, {len(_FINDINGS.opportunities)} opportunities, "
        f"{len(_FINDINGS.key_metrics)} key metrics, "
        f"{len(_FINDINGS.questions)} DD questions."
    )


async def run_team(crew_input: CrewInput) -> CrewOutput:
    """Build the Team, kick off the Ingestor, run round-by-round until idle —
    checking the running token total against the 50k cap BETWEEN rounds
    (Layer 2; the bridge's after-hook surface is empty for a subprocess)."""
    _reset_state()
    args = crew_input.args or {}
    _CTX.update({
        "pages": args.get("pages") or [],
        "entity_arg": (str(args.get("entity")).strip() if args.get("entity") else None),
        "pdf_stem": args.get("pdf_stem") or args.get("pdf_name") or None,
        "run_id": crew_input.run_id,
        "date": args.get("date") or datetime.now(timezone.utc).date().isoformat(),
    })
    if not _CTX["pages"]:
        raise ValueError(
            "triage crew requires args.pages (bridge-extracted CIM text); none provided"
        )

    llm_cfg = crew_input.llm_config
    team = Team()
    team.hire([
        IngestorRole(llm_cfg=llm_cfg),
        RedFlagsRole(llm_cfg=llm_cfg),
        OpportunitiesRole(llm_cfg=llm_cfg),
        KeyMetricsRole(llm_cfg=llm_cfg),
        QuestionsForMgmtRole(llm_cfg=llm_cfg),
        SummariserRole(llm_cfg=llm_cfg),
    ])
    # Kickoff content is just the trigger; the Ingestor reads pages from _CTX.
    team.run_project(f"Triage the CIM for {_CTX.get('entity_arg') or 'the attached company'}")

    env = team.env
    cost_cap = crew_input.cost_cap_tokens
    running_tokens = 0
    # Ingestor → 4 analysts → Summariser is ~3 productive rounds; 10 gives
    # headroom for staggered fan-in without masking a broken watch chain.
    for round_i in range(10):
        await env.run()
        running_tokens = _tokens_so_far()
        if running_tokens > cost_cap:
            raise CostCapExceeded(
                f"triage crew exceeded {cost_cap} tokens at round {round_i}: "
                f"used {running_tokens}"
            )
        if _SUMMARY_STATE.get("done") and env.is_idle:
            break

    if not _SUMMARY_STATE.get("done"):
        # The memo never rendered — a broken fan-in or a stalled role must be a
        # FAULT, not a healthy-looking empty deliverable (codex precedent, #31).
        raise RuntimeError(
            f"triage did not produce a summary within 10 rounds "
            f"(analyses done: {sorted(_DONE)}; {len(_ROLES_LOG)} role action(s))"
        )

    roles_log = [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    documents = [CrewDocument(
        relative_path=str(_SUMMARY_STATE["relative_path"]),
        content=str(_SUMMARY_STATE["memo"]),
        sensitivity=_sensitivity_tier(),
    )]
    # Structured CONCLUSION for the deliverable→vault capture loop
    # (#captures-to-vault-crews). These are the crew's ACTUAL findings — the same
    # counts the chat summary + memo report, NOT re-derived at capture time — so
    # the operator-gated proposal records real numbers ([no-llm-maths] /
    # [no-invented-sources] safe). ``subject`` keys the proposal filename;
    # ``entity`` templates the Companies/{entity}.md target. Gated on a known
    # entity so a degenerate run captures nothing (the route skips empty outcome).
    counts = _FINDINGS.severity_counts()
    entity = str(_SUMMARY_STATE.get("entity") or _CTX.get("entity") or "").strip()
    # "unknown entity" is infer_entity's last-resort fallback — don't capture a
    # Companies/unknown entity.md proposal off it (Opus review). A real entity
    # (explicit arg / inferred name / pdf_stem) is required to capture.
    _captureable = bool(entity) and entity != "unknown entity"
    outcome = {
        "subject": entity,
        "entity": entity,
        "high": counts["high"],
        "med": counts["med"],
        "low": counts["low"],
        "opportunities": len(_FINDINGS.opportunities),
        "key_metrics": len(_FINDINGS.key_metrics),
        "questions": len(_FINDINGS.questions),
        "date": str(_CTX.get("date") or ""),
    } if _captureable else {}
    return CrewOutput(
        run_id=crew_input.run_id,
        status="ok",
        summary=_chat_summary(),
        artefacts=[],          # the bridge writes the file + reports the path
        documents=documents,
        outcome=outcome,
        roles_log=roles_log,
        token_count=running_tokens,
    )


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════


def _partial_roles_log() -> list[RoleLogEntry]:
    """Best-effort roles_log for ERROR envelopes (telemetry must not mask the
    original fault). Never raises."""
    try:
        return [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    try:
        crew_input = read_input()
    except Exception as e:  # noqa: BLE001 — no usable CrewOutput possible
        write_error(f"input parse failed: {type(e).__name__}: {e}")
        return 2

    _redirect_output_streams(crew_input.run_id)

    try:
        result = asyncio.run(run_team(crew_input))
        write_result(result)
        return 0
    except CostCapExceeded as e:
        write_result(CrewOutput(
            run_id=crew_input.run_id, status="error",
            summary=f"Cost cap exceeded: {e}",
            roles_log=_partial_roles_log(), token_count=_tokens_so_far(),
            error=str(e),
        ))
        return 1
    except Exception as e:  # noqa: BLE001 — last-ditch structured envelope
        write_result(CrewOutput(
            run_id=crew_input.run_id, status="error",
            summary=f"Crew crashed: {type(e).__name__}: {e}",
            roles_log=_partial_roles_log(), token_count=_tokens_so_far(),
            error=f"{type(e).__name__}: {e}",
        ))
        return 1


if __name__ == "__main__":
    sys.exit(main())
