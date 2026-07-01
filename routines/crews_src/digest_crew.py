"""Digest crew — stages 1-4 (#ingest-digest): doc-scanner → parallel per-doc
analyzers → cross-doc synthesizer → review gate, over the #31 MetaGPT subprocess boundary.

Deployed by ``routines/crew/install/install_metagpt.py`` from
``<routines repo>/crews_src/`` to ``<repo>\\crews\\`` — the repo copy is
the single source of truth; never edit the deployed copy in place. Runs in the
isolated crew venv (Python 3.11); the bridge NEVER imports this file.

Four roles, a linear ``_watch`` chain (mirrors hello_world's structure, which
it copies for all the boundary plumbing — loguru drop, ``capture_protocol_
stream``, per-run log redirect, strict single-result-line stdout):

  * ``DocScanner``  — DETERMINISTIC, no LLM. Inventories the drop dir
    (``_shared.digest.scanner``): type-detect, content-hash dedupe, sensitivity
    pre-classification from project context. Emits the scan as JSON.
  * ``DocAnalyzer`` — LOCAL-Ollama enrichment. Per unique doc: deterministic
    text extraction → public/private routing (``_shared.digest.classifier``,
    fail-closed) → atomic-fact extraction into subject/field/value triples
    (``_shared.digest.analyzer``), under a bounded ``asyncio.Semaphore``.
  * ``Synthesizer`` — DETERMINISTIC cross-doc fusion (``_shared.digest.
    synthesize``): entity-keyed dedupe, contradiction surfacing (same
    subject/field, divergent value), the wikilink web; plus an OPT-IN
    best-effort local-Ollama narrative. Reads the per-doc ``DocAnalysis`` list.
  * ``Reviewer`` — DETERMINISTIC completeness gate (``_shared.digest.review``):
    every fact has provenance (uncited = blocking), every fused entity resolves
    to a vault note or is flagged new, orphan subjects surfaced. 0 tokens.

DEFERRED to a follow-up slice (clean seam, NOT built here):
  * stage 5 emit — atomic vault notes to ``Projects/<deal>/digest/`` (decision 1)
    via ``routines/shared/write_policy.py``, BRIDGE-SIDE (the crew can't import
    ``routines.*``). NOT done here — the crew persists the intermediate per-doc
    + synthesis + review structures only.

EXTRACTION (RECONCILED bridge-side at Phase-7 integration, #ingest-digest ↔ #32):
text extraction now runs BRIDGE-SIDE — ``routines.crew.artefacts.prepare_crew_
input`` (which owns pypdf/pypdfium2 + python-docx) pre-extracts every supported
doc into ``CrewInput.args["extracted_text"]`` (a ``{path: text}`` map), exactly
the /triage precedent (the shared crews venv has no PDF/DOCX libs — crew-venv-no-
pdf project memory). ``DocAnalyzer`` reads that map via ``_bridge_first_extract``
and only falls back to the crew-side ``extract.py`` for a path the bridge didn't
pre-extract (e.g. an MD-only pile, or a direct crew launch without bridge prep) —
where a PDF/DOCX still degrades to a clear "extract bridge-side" DigestExtractError.
The classifier / analyzer / scanner logic is unchanged by where extraction runs.

Exit codes: 0 = success (stdout has CrewOutput) · 1 = crew ran but errored
(stdout has CrewOutput status="error") · 2 = input parse failure (stderr).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from _shared import vault_scan
from _shared.boundary import (
    CrewInput,
    CrewOutput,
    RoleLogEntry,
    capture_protocol_stream,
    collect_roles_log,
    read_input,
    write_error,
    write_result,
)
from _shared.boundary import Artefact
from _shared.digest.analyzer import DEFAULT_CONCURRENCY, analyze_docs
from _shared.digest.extract import extract_text
from _shared.digest.models import (
    DigestSliceResult,
    DocAnalysis,
    DocCandidate,
    ReviewResult,
    ScanResult,
    SynthesisResult,
)
from _shared.digest.review import review_digest
from _shared.digest.scanner import scan_drop_dir, unique_supported
from _shared.digest.synthesize import norm_key, synthesize_facts
from _shared.ollama_config import build_ollama_llm_for_role

# Drop loguru's default stderr sink BEFORE any metagpt import (stderr non-empty
# is a fault signal to the bridge — spec §2.4). See hello_world_crew for the
# full rationale; this is the same load-bearing template fix.
from loguru import logger as _loguru_root

_loguru_root.remove()

from metagpt.actions import Action  # noqa: E402
from metagpt.roles import Role  # noqa: E402
from metagpt.schema import Message  # noqa: E402
from metagpt.team import Team  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Manifest — routines/crew/registry.py duplicates this shape. Keep field names
# identical on both sides.
# ════════════════════════════════════════════════════════════════════════════

MANIFEST = {
    "verb": "digest",
    "module": "digest_crew",
    # None = inherit from workspace tier (project → confidential → local lane).
    # NOT a hard MNPI lock: operator decision 2 is SPLIT routing — the per-doc
    # public/private classifier is the cloud-eligibility gate, not the manifest.
    # In this slice every lane is local regardless (pick_lane is local-only and
    # the classifier pins effective_lane="local"), so inherit is fail-closed.
    "sensitivity_override": None,
    "cost_cap_tokens": 60_000,
    # Wall clock the bridge clamps the subprocess to. A deal pile of several
    # docs each enriched on a cold qwen3:14b needs headroom; the bridge's global
    # 600s ceiling still caps it.
    "cost_cap_seconds": 540,
    "roles": ["DocScanner", "DocAnalyzer", "Synthesizer", "Reviewer"],
    "models_default": {
        # Per-role models, keyed by the EXACT role name so the generic models map
        # (#33) resolves each via ollama_config.model_for_role. (Integration:
        # switched off the legacy "Analyst" stopgap once the models dict landed —
        # operator decision 5b.) Keep in sync with the registry. Tiering
        # (2026-06-15 triage rule): synthesis/generation -> 14b; the Reviewer gate
        # is deterministic (0 tokens) but labelled 8b/classification.
        "DocAnalyzer": "qwen3:14b",
        "Synthesizer": "qwen3:14b",
        "Reviewer": "qwen3:8b",
    },
    "description": (
        "Ingest a drop dir of deal docs (stages 1-4): scan + fail-closed "
        "public/private routing + per-doc atomic-fact extraction + cross-doc "
        "synthesis + completeness review. Local-only."
    ),
    # #captures-to-vault-crews: documentation mirror of the registry capture
    # block (the BRIDGE reads routines/crew/registry.py, not this dict). {project}
    # is the deal identifier; the crew skips the capture when it is empty.
    "captures_to_vault": {
        "target": "Companies/{project}.md",
        "section": "Digest history",
        "fields": ["docs", "facts", "entities", "contradictions", "gate", "uncited"],
        "headline": "{project} digest: {docs} docs, {facts} facts, {entities} entities, "
                    "{contradictions} contradictions; review {gate}",
    },
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers (copied from hello_world_crew — same boundary discipline)
# ════════════════════════════════════════════════════════════════════════════


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sensitivity_tier() -> str:
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


def _llm_total_tokens(llm: Any) -> int:
    """Best-effort total-token read from a MetaGPT LLM's cost manager
    (defensive across 0.8.x attribute drift — see hello_world_crew)."""
    cm = getattr(llm, "cost_manager", None)
    if cm is None:
        return 0
    for attr in ("total_prompt_tokens", "total_completion_tokens"):
        if not hasattr(cm, attr):
            return int(getattr(cm, "total_tokens", 0) or 0)
    return int((cm.total_prompt_tokens or 0) + (cm.total_completion_tokens or 0))


def _redirect_output_streams(run_id: str) -> None:
    """Route everything except protocol envelopes away from the boundary
    (loguru → per-run log; stray prints → per-run stdout log). Copied from
    hello_world_crew; see its docstring."""
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


# ════════════════════════════════════════════════════════════════════════════
# Roles-log (crew-owned — see hello_world_crew for why env.history is unusable)
# ════════════════════════════════════════════════════════════════════════════

_ROLES_LOG: list[dict[str, Any]] = []

# Crew-owned captures of the two roles' structured outputs. Reading them back
# off metagpt 0.8.x's env is API-fragile (env.history is a debug STRING, not a
# Memory — the #31 lesson), so each action stashes its result here as it runs
# and run_team reads these directly. Cleared at the top of run_team.
_LAST_SCAN: ScanResult | None = None
_LAST_ANALYSES: list[DocAnalysis] = []
# Stage-3 cross-doc synthesis capture (set by SynthesizeFacts; None until it
# runs). Read by run_team into the DigestSliceResult, same crew-owned-state
# pattern as _LAST_SCAN / _LAST_ANALYSES.
_LAST_SYNTHESIS: SynthesisResult | None = None
# Stage-4 review-gate verdict (set by ReviewDigest; None until it runs).
_LAST_REVIEW: ReviewResult | None = None
# Stage-3 narrative is OPT-IN (codex review): default OFF, so the default digest
# run is fully DETERMINISTIC and spends no synthesis tokens. Enabled per-run via
# args["narrative"] (CLI --narrative / route arg). The deterministic fusion +
# contradiction core ALWAYS runs regardless of this flag.
_NARRATE_ENABLED: bool = False
# Run-scoped analyzer concurrency (set by run_team from crew_input.args). A
# module global rather than an action field so we can pass the action CLASS to
# set_actions exactly like hello_world (passing an instance + mutating a field
# is not a contract metagpt 0.8.x guarantees).
_ANALYZER_CONCURRENCY: int = DEFAULT_CONCURRENCY
# Bridge-extracted text, ``{candidate.path: text}`` (integration: extraction
# moved bridge-side onto the /triage precedent — the shared crews venv has no
# PDF/DOCX libs). Set by run_team from ``args["extracted_text"]``; the analyzer's
# extract_fn reads it and only falls back to crew-side extract.py for a path the
# bridge didn't pre-extract (e.g. an MD-only run launched without bridge prep).
_EXTRACTED_TEXT: dict[str, str] = {}


def _bridge_first_extract(path: str, *, doc_type: str | None = None) -> str:
    """``extract_fn`` for the analyzer: prefer the BRIDGE-extracted text (the
    integration default — the routines venv has the PDF/DOCX libs the crews venv
    lacks). Falls back to the crew-side ``extract_text`` only for a path the
    bridge didn't pre-extract (e.g. an MD-only pile, or a direct crew launch
    without bridge prep) — and that fallback still raises ``DigestExtractError``
    on a PDF/DOCX in a dep-less venv, exactly as before. Same signature as
    ``extract_text`` so it drops into ``analyze_one`` unchanged."""
    if path in _EXTRACTED_TEXT:
        return _EXTRACTED_TEXT[path]
    # Not pre-extracted by the bridge. For MD this still works dep-free; for a
    # PDF/DOCX in the crews venv it raises the clear "extract bridge-side"
    # DigestExtractError, which analyze_one catches per-doc (degrades that doc,
    # not the run).
    return extract_text(path, doc_type=doc_type)


class _MeteredRole(Role):
    """Role base that appends one roles-log entry per executed Action. The
    Action stamps ``_last_run_meta`` (ts_start, duration_ms, token_count,
    status); we fold it in here."""

    async def _act(self) -> Message:
        todo = self.rc.todo
        msg = await super()._act()
        meta = dict(getattr(todo, "_last_run_meta", None) or {})
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


# ════════════════════════════════════════════════════════════════════════════
# Actions
# ════════════════════════════════════════════════════════════════════════════


class ScanDocs(Action):
    """DocScanner's only action: deterministic inventory of the drop dir.

    No LLM. Reads the kickoff JSON (drop_dir / project / sensitivity), scans,
    and returns the ``ScanResult`` as JSON for the analyzer."""

    name: str = "ScanDocs"

    async def run(self, with_messages: list[Message]) -> str:
        global _LAST_SCAN
        ts_start = _iso_now()
        t0 = time.monotonic()
        spec = json.loads(with_messages[-1].content)
        scan = scan_drop_dir(
            spec["drop_dir"],
            spec.get("project", ""),
            project_sensitivity=spec.get("sensitivity", "confidential"),
            recursive=bool(spec.get("recursive", False)),
        )
        _LAST_SCAN = scan
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": 0,           # deterministic — no LLM
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        # Return a COUNT-ONLY summary — NOT the scan JSON, which carries the
        # drop_dir + file paths/names and would land in this role's audit
        # output_summary (codex data-handling HIGH). The analyzer reads the full
        # scan from the _LAST_SCAN module global, not from this message.
        return (
            f"scanned {scan.total_files} file(s): {scan.unique_docs} unique, "
            f"{scan.duplicates} duplicate, {scan.unsupported} unsupported"
        )


class AnalyzeDocs(Action):
    """DocAnalyzer's only action: per-doc extract → classify → local enrich.

    Builds an Ollama-backed ``chat_fn`` from this action's LLM and runs the
    analyzers under a bounded semaphore. Tokens are metered as the cost-manager
    delta across the whole fan-out."""

    name: str = "AnalyzeDocs"

    async def run(self, with_messages: list[Message]) -> str:  # noqa: ARG002 — framework signature
        global _LAST_ANALYSES
        ts_start = _iso_now()
        t0 = time.monotonic()
        tokens_before = _llm_total_tokens(self.llm)

        # Read the full scan from crew-owned state, NOT the watched message —
        # the message is now a count-only summary (data-handling HIGH).
        scan = _LAST_SCAN
        candidates: list[DocCandidate] = unique_supported(scan) if scan else []

        async def chat_fn(system: str, prompt: str) -> str:
            # Fold the schema/system block into the SINGLE user prompt rather than
            # system_msgs. SMOKE 2026-06-15 (uncommitted): MetaGPT's Ollama aask
            # did not deliver system_msgs to qwen3 — the extraction schema never
            # reached the model, so it returned generic entities + 0 facts; a
            # direct system-message probe extracts 12/12 facts. The other three
            # crews already pass everything in the single prompt for this reason.
            # Uses the role's local Ollama LLM + its TokenCostManager.
            return await self.llm.aask(f"{system}\n\n{prompt}")

        analyses = await analyze_docs(
            candidates, chat_fn=chat_fn, concurrency=_ANALYZER_CONCURRENCY,
            extract_fn=_bridge_first_extract,
        )
        _LAST_ANALYSES = analyses
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": max(0, _llm_total_tokens(self.llm) - tokens_before),
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        # Count-only summary — NOT the analyses JSON (paths/entities/facts),
        # which would leak into the audit output_summary (data-handling HIGH).
        n_facts = sum(len(a.facts) for a in analyses)
        return f"analyzed {len(analyses)} doc(s), {n_facts} fact(s)"


class SynthesizeFacts(Action):
    """Synthesizer's only action (stage 3): DETERMINISTIC cross-doc entity
    fusion + contradiction surfacing over the per-doc analyses, plus a
    best-effort local-Ollama narrative. Reads the analyses from crew-owned state
    (``_LAST_ANALYSES``), NOT the watched message (which is count-only)."""

    name: str = "SynthesizeFacts"

    async def run(self, with_messages: list[Message]) -> str:  # noqa: ARG002 — trigger only; data from module state
        global _LAST_SYNTHESIS
        ts_start = _iso_now()
        t0 = time.monotonic()
        tokens_before = _llm_total_tokens(self.llm)

        async def narrate_fn(prompt: str) -> str:
            # Single prompt — NOT system_msgs (MetaGPT's Ollama aask drops them;
            # the same lesson AnalyzeDocs.chat_fn documents). Force /no_think:
            # global thinking-mode is ON, but a <think> block is reasoning noise
            # in a narrative (synthesize._strip_think also removes a leftover one
            # defensively). Local Ollama, metered via the role's TokenCostManager;
            # synthesize_facts swallows a failure here and leaves narrative="".
            return await self.llm.aask("/no_think\n" + prompt)

        scan = _LAST_SCAN
        project = scan.project if scan else ""
        synthesis = await synthesize_facts(
            list(_LAST_ANALYSES), project=project,
            narrate_fn=narrate_fn if _NARRATE_ENABLED else None,
        )
        _LAST_SYNTHESIS = synthesis
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": max(0, _llm_total_tokens(self.llm) - tokens_before),
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        # Count-only summary — NOT the synthesis JSON (entity/values + the
        # contradiction provenance paths), which would leak into this role's
        # audit output_summary (data-handling, like ScanDocs / AnalyzeDocs).
        return (
            f"synthesized {len(synthesis.entities)} entities, "
            f"{len(synthesis.facts)} fact(s), "
            f"{len(synthesis.contradictions)} contradiction(s)"
        )


def _known_vault_entities() -> set[str]:
    """Normalised stems of existing vault notes, for the stage-4 entity-
    resolution check. Best-effort + LOCAL — reads FILENAMES only (no note
    content, no egress); a scan failure degrades to an empty set (every fused
    entity is then flagged new). Same norm_key as stage-3 fusion."""
    try:
        root = vault_scan.vault_root()
        return {norm_key(p.stem) for p in vault_scan.iter_vault_markdown(root)}
    except Exception:  # noqa: BLE001 — resolution is best-effort, never fatal
        return set()


class ReviewDigest(Action):
    """Reviewer's only action (stage 4): a DETERMINISTIC completeness gate over
    the stage-3 synthesis — provenance (uncited = blocking), entity resolution
    against existing vault notes (new vs known), orphan subjects. Makes NO LLM
    call (0 tokens). Reads the synthesis from crew-owned state."""

    name: str = "ReviewDigest"

    async def run(self, with_messages: list[Message]) -> str:  # noqa: ARG002 — trigger only; data from module state
        global _LAST_REVIEW
        ts_start = _iso_now()
        t0 = time.monotonic()
        synthesis = _LAST_SYNTHESIS or SynthesisResult()
        review = review_digest(synthesis, known_entities=_known_vault_entities())
        _LAST_REVIEW = review
        self._last_run_meta = {
            "ts_start": ts_start,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "token_count": 0,           # deterministic — no LLM
            "sensitivity": _sensitivity_tier(),
            "status": "ok",
        }
        # Count-only summary — booleans + counts, never the uncited descriptors /
        # entity / orphan names (data-handling, like the other roles).
        return (
            f"reviewed: passed={review.passed}, {len(review.uncited)} uncited, "
            f"{len(review.new_entities)} new entity(ies), "
            f"{len(review.orphan_subjects)} orphan subject(s)"
        )


# ════════════════════════════════════════════════════════════════════════════
# Roles — DocScanner (kickoff) → DocAnalyzer
# ════════════════════════════════════════════════════════════════════════════


class DocScannerRole(_MeteredRole):
    name: str = "DocScanner"
    profile: str = "Deterministic drop-dir scanner"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.set_actions([ScanDocs])
        # No _watch — scanner fires on the kickoff message.


class DocAnalyzerRole(_MeteredRole):
    name: str = "DocAnalyzer"
    profile: str = "Per-doc atomic-fact extractor"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        # Per-role LLM assigned BEFORE set_actions so Role._init_action pins it
        # onto the action (assigning after leaves the action on the config
        # default — the real-boundary lesson from hello_world). The "DocAnalyzer"
        # key resolves via the generic models map (#33) →
        # CrewLLMConfig.models["DocAnalyzer"] — the bridge-resolved local
        # enrichment model (integration: switched off the legacy "Analyst"
        # stopgap — operator decision 5b). Concurrency + bridge-extracted text
        # flow via module globals (set in run_team), so we pass the action CLASS.
        self.llm = build_ollama_llm_for_role("DocAnalyzer", llm_cfg)
        self.set_actions([AnalyzeDocs])
        self._watch([ScanDocs])


class SynthesizerRole(_MeteredRole):
    name: str = "Synthesizer"
    profile: str = "Cross-doc fact synthesizer"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        # Per-role LLM BEFORE set_actions (Role._init_action pins it onto the
        # action). "Synthesizer" resolves via the generic models map (#33) →
        # qwen3:14b (generation/synthesis tier — the 2026-06-15 triage rule: 8b
        # extracts but does not generate). The deterministic fusion/contradiction
        # core burns 0 tokens; only the best-effort narrative calls the model.
        self.llm = build_ollama_llm_for_role("Synthesizer", llm_cfg)
        self.set_actions([SynthesizeFacts])
        self._watch([AnalyzeDocs])


class ReviewerRole(_MeteredRole):
    name: str = "Reviewer"
    profile: str = "Digest completeness reviewer"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        # LLM assigned for symmetry/init, but the gate is DETERMINISTIC — it
        # makes NO LLM call (0 tokens), like explore's Coordinator. "Reviewer"
        # resolves via the models map (#33) → qwen3:8b (classification tier);
        # kept so a future fuzzy-resolution pass is a one-line change.
        self.llm = build_ollama_llm_for_role("Reviewer", llm_cfg)
        self.set_actions([ReviewDigest])
        self._watch([SynthesizeFacts])


# ════════════════════════════════════════════════════════════════════════════
# Team run + Layer-2 cost cap
# ════════════════════════════════════════════════════════════════════════════


class CostCapExceeded(RuntimeError):
    """Layer-2 cost-cap trip — surfaces as CrewOutput status="error"."""


def _tokens_so_far() -> int:
    return sum(int(e.get("token_count", 0) or 0) for e in _ROLES_LOG)


def _summarise(
    analyses: list[dict[str, Any]],
    synthesis: SynthesisResult | None,
    review: ReviewResult | None,
) -> str:
    """Build the chat-bubble summary from the per-doc analyses + stage-3
    synthesis + the stage-4 review verdict."""
    n_docs = len(analyses)
    n_facts = sum(len(a.get("facts") or []) for a in analyses)
    n_cloud = sum(1 for a in analyses
                  if (a.get("routing") or {}).get("cloud_eligible"))
    n_ent = len(synthesis.entities) if synthesis else 0
    n_con = len(synthesis.contradictions) if synthesis else 0
    gate = "passed" if (review and review.passed) else ("FAILED" if review else "n/a")
    n_uncited = len(review.uncited) if review else 0
    return (
        f"Digest (stages 1-4): analyzed {n_docs} doc(s), extracted "
        f"{n_facts} atomic fact(s); synthesised {n_ent} entities with "
        f"{n_con} contradiction(s); review gate {gate} ({n_uncited} uncited). "
        f"All enrichment ran LOCAL ({n_cloud} doc(s) flagged cloud-eligible but "
        f"routed local — cloud not wired). Emit deferred."
    )


def _write_intermediate(result: DigestSliceResult, run_id: str) -> Artefact | None:
    """Persist the intermediate per-doc fact structures to a NON-VAULT file the
    CLI reads + prints (``<crew dir>/.logs/<run_id>.digest.json``).

    This is deliberately NOT a vault write — stage 5 (emit to
    ``Projects/<deal>/digest/`` via ``routines/shared/write_policy.py``) is the
    deferred seam. Best-effort: a write miss costs the CLI its detailed print,
    not the run."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", run_id or "")[:64] or "no-run-id"
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".logs")
    path = os.path.join(log_dir, f"{safe_id}.digest.json")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(result.model_dump_json(indent=2))
    except Exception:  # noqa: BLE001 — telemetry/intermediate, never fatal
        return None
    return Artefact(path=path, sensitivity=_sensitivity_tier())


async def run_team(crew_input: CrewInput) -> CrewOutput:
    """Build the 2-role team, kick off with the scan spec, run round-by-round
    until idle — checking the running token total against the cost cap between
    rounds (Layer 2; the bridge can't see inside the subprocess)."""
    global _LAST_SCAN, _LAST_ANALYSES, _LAST_SYNTHESIS, _LAST_REVIEW, _NARRATE_ENABLED, _ANALYZER_CONCURRENCY, _EXTRACTED_TEXT
    drop_dir = str(crew_input.args.get("drop_dir", "")).strip()
    if not drop_dir:
        raise ValueError("digest crew requires args.drop_dir")
    project = str(crew_input.args.get("project", "")).strip()
    sensitivity = (
        crew_input.workspace.sensitivity_tier
        or _sensitivity_tier()
        or "confidential"
    )

    _ROLES_LOG.clear()
    _LAST_SCAN = None
    _LAST_ANALYSES = []
    _LAST_SYNTHESIS = None
    _LAST_REVIEW = None
    _NARRATE_ENABLED = bool(crew_input.args.get("narrative", False))
    _ANALYZER_CONCURRENCY = max(1, int(crew_input.args.get("concurrency", DEFAULT_CONCURRENCY)))
    # Bridge-extracted text (integration): the route/CLI pre-extracts each doc
    # via routines.crew.artefacts (which has the PDF/DOCX libs) and passes the
    # path→text map here. Empty when launched without bridge prep — the analyzer
    # then falls back to crew-side extract.py per doc (MD-only piles still work).
    raw_extracted = crew_input.args.get("extracted_text") or {}
    _EXTRACTED_TEXT = {str(k): str(v) for k, v in raw_extracted.items()} \
        if isinstance(raw_extracted, dict) else {}
    team = Team()
    team.hire([
        DocScannerRole(),
        DocAnalyzerRole(llm_cfg=crew_input.llm_config),
        SynthesizerRole(llm_cfg=crew_input.llm_config),
        ReviewerRole(llm_cfg=crew_input.llm_config),
    ])
    kickoff = json.dumps({
        "drop_dir": drop_dir, "project": project, "sensitivity": sensitivity,
        "recursive": bool(crew_input.args.get("recursive", False)),
    })
    team.run_project(kickoff)

    env = team.env
    cost_cap = crew_input.cost_cap_tokens
    running_tokens = 0
    # 6 rounds: a 3-step linear chain (scan → analyze → synthesize) + headroom
    # for the deferred review step. The bridge wall-clock is the outer bound;
    # this is the inner one.
    for round_i in range(6):
        await env.run()
        running_tokens = _tokens_so_far()
        if running_tokens > cost_cap:
            raise CostCapExceeded(
                f"digest crew exceeded {cost_cap} tokens at round {round_i}: "
                f"used {running_tokens}"
            )
        if env.is_idle:
            break

    if not env.is_idle:
        raise RuntimeError(
            f"crew did not reach idle within 6 rounds "
            f"({len(_ROLES_LOG)} role action(s) completed)"
        )

    # Read the roles' captured outputs from crew-owned state (set as the
    # actions ran) — not off the env, whose 0.8.x history is a debug string.
    slice_result = _assemble_slice_result(project)
    analyses_payload = [a.model_dump() for a in slice_result.analyses]
    summary = _summarise(analyses_payload, slice_result.synthesis, slice_result.review)
    artefact = _write_intermediate(slice_result, crew_input.run_id)

    roles_log = [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    # Structured CONCLUSION for the deliverable→vault capture loop
    # (#captures-to-vault-crews) — the SAME deterministic counts ``_summarise``
    # reports, not re-derived at capture time. Gated on a known deal ``project``
    # so a project-less run captures nothing (the route skips empty outcome).
    syn = slice_result.synthesis
    rev = slice_result.review
    outcome = {
        "subject": project,
        "project": project,
        "docs": len(slice_result.analyses),
        "facts": sum(len(a.facts) for a in slice_result.analyses),
        "entities": len(syn.entities) if syn else 0,
        "contradictions": len(syn.contradictions) if syn else 0,
        "gate": "passed" if (rev and rev.passed) else ("FAILED" if rev else "n/a"),
        "uncited": len(rev.uncited) if rev else 0,
    } if project else {}
    return CrewOutput(
        run_id=crew_input.run_id,
        status="ok",
        summary=summary,
        artefacts=[artefact] if artefact else [],
        outcome=outcome,
        roles_log=roles_log,
        token_count=running_tokens,
    )


def _assemble_slice_result(project: str) -> DigestSliceResult:
    """Build the ``DigestSliceResult`` from the crew-owned captures (``_LAST_
    SCAN`` / ``_LAST_ANALYSES`` / ``_LAST_SYNTHESIS`` / ``_LAST_REVIEW``),
    falling back to an empty scan if the scanner somehow produced nothing — a
    partial result beats losing the run. ``synthesis`` / ``review`` are ``None``
    only if those stages never ran."""
    scan = _LAST_SCAN or ScanResult(
        drop_dir="", project=project, project_sensitivity="confidential",
    )
    return DigestSliceResult(
        project=project, scan=scan, analyses=list(_LAST_ANALYSES),
        synthesis=_LAST_SYNTHESIS, review=_LAST_REVIEW, deferred_stages=["emit"],
    )


# ════════════════════════════════════════════════════════════════════════════
# Entry point (copied shape from hello_world_crew)
# ════════════════════════════════════════════════════════════════════════════


def _partial_roles_log() -> list[RoleLogEntry]:
    try:
        return [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    try:
        crew_input = read_input()
    except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
        write_result(CrewOutput(
            run_id=crew_input.run_id, status="error",
            summary=f"Crew crashed: {type(e).__name__}: {e}",
            roles_log=_partial_roles_log(), token_count=_tokens_so_far(),
            error=f"{type(e).__name__}: {e}",
        ))
        return 1


if __name__ == "__main__":
    sys.exit(main())
