"""Debate crew — Bull/Bear stress-test of a thesis over N rounds (#36).

OUTSTANDING #36, [[autonomous-crews]] §4. Verb: ``/debate <thesis>``
(``--rounds=N``, default 3). Deployed by
``routines/crew/install/install_metagpt.py`` from ``<routines repo>/crews_src/``
to ``<repo>\\crews\\`` — the repo copy is the single source of truth;
never edit the deployed copy in place.

Roles (per §4):
  * ``ContextLoader`` — builds a neutral, balanced brief both sides can cite
  * ``Bull``          — argues FOR the thesis; cites [[wikilinks]] from the brief
  * ``Bear``          — argues AGAINST; cites [[wikilinks]] from the brief
  * ``Moderator``     — after each round, summarises how positions CHANGED
  * ``Synthesist``    — final note: consensus / open disagreement / recommended action

WHAT IT REUSES FROM THE hello_world TEMPLATE (unchanged): the JSON-over-stdio
boundary (``read_input``/``write_result``/``capture_protocol_stream``), the
loguru-drop-before-import discipline, per-role Ollama LLM construction
(``build_ollama_llm_for_role``), the ``_MeteredAction`` timing/token metering,
the crew-owned ``_ROLES_LOG`` + ``collect_roles_log`` audit walk, the
in-subprocess Layer-2 cost-cap counter, and the single-result-line ``main()``.

WHERE IT DELIBERATELY DIVERGES (documented, justified): hello_world drives its
roles via MetaGPT's ``Team`` / ``env.run()`` / ``_watch`` emergent scheduling —
right for a LINEAR Analyst→Reviewer→Synthesist chain. A debate is NON-LINEAR:
N rounds of Bull↔Bear with a per-round Moderator summary, then a Synthesist,
and the round count is a HARD requirement (default 3, ``--rounds=N``). Emergent
watch-scheduling cannot be bounded to exactly N rounds without fragile counter
state, so this crew orchestrates the structure EXPLICITLY in ``run_team`` (a
Python loop — the same shape hello_world already uses to drive ``env.run()``
round-by-round) and invokes each role's metered action directly. The only
metagpt surface this touches per call is ``role.llm.aask(prompt)``, which the
#31 real-boundary smoke validated.

VAULT EVIDENCE (integration: operator decision 2 — wired NOW): the crew loads
REAL vault evidence via the SHARED ``_shared.vault_scan`` layer (the same
filesystem scan /explore uses), so the ContextLoader/Bull/Bear cite genuine
``[[wikilinks]]`` per [[autonomous-crews]] §4. ``args.evidence`` remains an
explicit operator OVERRIDE (pre-loaded evidence wins; no scan then). With no
override AND no scan hits, the roles argue from first principles and cite no
wikilinks (no-invented-sources — the prompts forbid inventing a [[link]]).
Reading the vault directory from the crew venv is NOT a boundary violation (the
boundary forbids ``import routines.*``, not reading files on disk).

Runs in ``<repo>\\crews\\.venv`` (Python 3.11). The bridge NEVER imports
this file — the boundary is ``subprocess.Popen``.

Exit codes: 0 = success (stdout has CrewOutput) · 1 = crew ran but errored
(stdout has CrewOutput status="error") · 2 = input parse failure (stderr).
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
    CrewInput,
    CrewOutput,
    RoleLogEntry,
    capture_protocol_stream,
    collect_roles_log,
    read_input,
    write_error,
    write_result,
)
from _shared.ollama_config import build_ollama_llm_for_role, ollama_structured_chat
from debate_support import (
    VERDICT_SCHEMA,
    VERDICT_VALUES,
    bear_prompt,
    bull_prompt,
    context_prompt,
    format_transcript,
    load_vault_evidence,
    moderator_prompt,
    resolve_rounds,
    resolve_thesis,
    round_tag,
    synthesis_prompt,
    thesis_slug,
    verdict_prompt,
)

# Drop loguru's default stderr sink BEFORE any metagpt import — see the
# hello_world template for the full rationale: ``metagpt.const`` logs through
# the raw loguru logger at import time, and stderr non-empty is a fault signal
# to the bridge (spec §2.4). main()'s ``_redirect_output_streams`` retargets
# metagpt's own sinks to the per-run log file afterwards.
from loguru import logger as _loguru_root

_loguru_root.remove()

# MetaGPT imports — only valid inside the crew venv. AFTER the _shared imports
# (so an input-parse error can be reported even when metagpt is broken) and
# AFTER the loguru sink drop (E402 is the point). Only Action + Role are needed:
# this crew orchestrates explicitly and never constructs a Team / Environment.
from metagpt.actions import Action  # noqa: E402
from metagpt.roles import Role  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Manifest — routines/crew/registry.py duplicates this shape for
# verb→sensitivity routing. Keep field names identical on both sides.
# ════════════════════════════════════════════════════════════════════════════

MANIFEST = {
    "verb": "debate",
    "module": "debate_crew",
    # None = inherit the workspace tier ([[autonomous-crews]] §4: "Confidential
    # workspaces → all Ollama"). No always-on lock (unlike /triage's MNPI) — a
    # debate follows the workspace it runs in. v1 lanes are local-only anyway.
    "sensitivity_override": None,
    # §1 cost-cap row: 60k tokens; seconds smoke-tuned 300->600 (registry sync).
    "cost_cap_tokens": 60_000,
    "cost_cap_seconds": 600,
    "roles": ["ContextLoader", "Bull", "Bear", "Moderator", "Synthesist"],
    # v1 is local-only on a single model. The bridge's build_llm_config only
    # threads model_synthesist through to a matching role; the other four
    # resolve via build_ollama_llm_for_role's qwen3:14b fallback — so declaring
    # qwen3:14b for all five is honest (it IS what every role runs on). Per-role
    # model differentiation is a v2 concern (flagged open question).
    "models_default": {
        "ContextLoader": "qwen3:14b",
        "Bull": "qwen3:14b",
        "Bear": "qwen3:14b",
        "Moderator": "qwen3:14b",
        "Synthesist": "qwen3:14b",
    },
    "description": (
        "Stress-test a thesis with explicit Bull/Bear voices over N rounds "
        "(default 3, --rounds=N up to 5). A ContextLoader builds a balanced "
        "brief, a Moderator summarises each round, and a Synthesist writes the "
        "final consensus / disagreement / recommended-action note. Chat-only "
        "by default; promotable to Topics/Theses/<thesis>.md."
    ),
    # #captures-to-vault-crews: documentation mirror of the registry capture
    # block (the BRIDGE reads routines/crew/registry.py, not this dict). The crew
    # resolves {target_note} at runtime (Companies/<deal>.md with a --deal arg,
    # else Topics/Theses/<thesis-slug>.md).
    "captures_to_vault": {
        "target": "{target_note}",
        "section": "Debate history",
        "fields": ["verdict", "recommended_action", "rounds"],
        "headline": "Debate — {thesis}: {verdict}; next: {recommended_action}",
    },
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers (generic — copied verbatim from the hello_world template)
# ════════════════════════════════════════════════════════════════════════════


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sensitivity_tier() -> str:
    """Tier the bridge resolved BEFORE launching (env-injected); echoed into
    every role row so the audit carries it."""
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


def _llm_total_tokens(llm: Any) -> int:
    """Best-effort total-token read from a MetaGPT LLM's cost manager.
    Defensive across 0.8.x minor drift; returns 0 when nothing is readable."""
    cm = getattr(llm, "cost_manager", None)
    if cm is None:
        return 0
    for attr in ("total_prompt_tokens", "total_completion_tokens"):
        if not hasattr(cm, attr):
            return int(getattr(cm, "total_tokens", 0) or 0)
    return int((cm.total_prompt_tokens or 0) + (cm.total_completion_tokens or 0))


def _redirect_output_streams(run_id: str) -> None:
    """Route EVERYTHING except protocol envelopes away from the boundary
    (metagpt/loguru logging → ``.logs/<run_id>.log``; stray ``print()``s →
    ``.logs/<run_id>.stdout.log``). See the hello_world template for the full
    rationale; the run_id is filename-sanitised against traversal."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", run_id or "")[:64] or "no-run-id"
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        stray = open(  # noqa: SIM115 — lives for the process lifetime
            os.path.join(log_dir, f"{safe_id}.stdout.log"),
            "a", encoding="utf-8", buffering=1,
        )
    except Exception:  # noqa: BLE001 — fall back to devnull capture
        stray = None
    capture_protocol_stream(stray)
    try:
        from metagpt.logs import logger as mg_logger
        mg_logger.remove()
        mg_logger.add(os.path.join(log_dir, f"{safe_id}.log"), level="INFO")
    except Exception:  # noqa: BLE001 — logging must never kill the crew
        pass


# ════════════════════════════════════════════════════════════════════════════
# Actions — each stamps ``_last_run_meta`` for the audit walk. Unlike the
# template, each ``run`` takes explicit string kwargs (the orchestrator calls
# them directly, not via env message-passing).
# ════════════════════════════════════════════════════════════════════════════


class _MeteredAction(Action):
    """Action base that times the LLM call and stamps run metadata, identical
    to the hello_world template's metering (the role folds it into _ROLES_LOG)."""

    async def _metered_aask(self, prompt: str) -> str:
        ts_start = _iso_now()  # BEFORE the call — this is a START time
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


class LoadContext(_MeteredAction):
    name: str = "LoadContext"

    async def run(self, *, thesis: str, evidence: str) -> str:
        return await self._metered_aask(context_prompt(thesis, evidence))


class ArgueBull(_MeteredAction):
    name: str = "ArgueBull"

    async def run(
        self, *, thesis: str, brief: str, prior_state: str, prior_bear: str,
        round_no: int, rounds: int,
    ) -> str:
        return await self._metered_aask(
            bull_prompt(thesis, brief, prior_state, prior_bear, round_no, rounds)
        )


class ArgueBear(_MeteredAction):
    name: str = "ArgueBear"

    async def run(
        self, *, thesis: str, brief: str, prior_state: str, current_bull: str,
        round_no: int, rounds: int,
    ) -> str:
        return await self._metered_aask(
            bear_prompt(thesis, brief, prior_state, current_bull, round_no, rounds)
        )


class ModerateRound(_MeteredAction):
    name: str = "ModerateRound"

    async def run(
        self, *, thesis: str, round_no: int, rounds: int, bull: str, bear: str,
        prior_state: str,
    ) -> str:
        return await self._metered_aask(
            moderator_prompt(thesis, round_no, rounds, bull, bear, prior_state)
        )


class Synthesize(_MeteredAction):
    name: str = "Synthesize"

    async def run(self, *, thesis: str, brief: str, transcript: str) -> str:
        return await self._metered_aask(synthesis_prompt(thesis, brief, transcript))


# ════════════════════════════════════════════════════════════════════════════
# Roles — each owns a per-role Ollama LLM + one Action; ``act_once`` runs the
# action, meters it, and appends one RoleLogEntry-shaped dict to _ROLES_LOG.
# ════════════════════════════════════════════════════════════════════════════


# Crew-owned roles log — one entry per executed action, appended by
# ``_MeteredRole.act_once``. A subprocess is one run, so module scope is the run
# scope (``run_team`` clears it defensively). Same WHY as hello_world: metagpt
# 0.8.x ``env.history`` is a debug STRING and ``Message`` can't carry metadata.
_ROLES_LOG: list[dict[str, Any]] = []


class CostCapExceeded(RuntimeError):
    """Layer-2 cost-cap trip — surfaces as CrewOutput status="error"."""


def _tokens_so_far() -> int:
    """Running token total from the crew-owned roles log."""
    return sum(int(e.get("token_count", 0) or 0) for e in _ROLES_LOG)


class _MeteredRole(Role):
    """Role base. Per-role LLM is assigned BEFORE ``set_actions`` so metagpt's
    ``Role._init_action`` pins it onto the action (assigning after leaves the
    action on the config-default LLM — the template documents this trap).

    ``act_once`` is the explicit-orchestration entry point: it runs the role's
    single action with the supplied prompt kwargs, then folds the action's
    ``_last_run_meta`` into a roles-log entry. ``log_tag`` (e.g. "(round 2)") is
    prefixed onto the audit ``output_summary`` so recurring roles stay legible;
    it is NOT passed to the action."""

    async def act_once(self, *, log_tag: str = "", **run_kwargs: Any) -> str:
        action = self._only_action()
        if action is None:
            raise RuntimeError(f"role {self.name!r} has no action configured")
        out = await action.run(**run_kwargs)
        meta = dict(getattr(action, "_last_run_meta", None) or {})
        text = str(out or "")
        summary = f"{log_tag} {text}".strip() if log_tag else text
        _ROLES_LOG.append({
            "role": self.name,
            "action": type(action).__name__,
            "ts_start": meta.get("ts_start", ""),
            "duration_ms": meta.get("duration_ms", 0),
            "token_count": meta.get("token_count", 0),
            "sensitivity": meta.get("sensitivity", _sensitivity_tier()),
            "status": meta.get("status", "ok"),
            "output_summary": summary[:200],
        })
        return text

    def _only_action(self) -> Any:
        """The role's single pinned action instance. ``set_actions`` stores it
        in ``self.actions`` on metagpt 0.8.x; fall back to ``self.rc.actions``
        across minor drift. A missing action raises loudly in ``act_once`` —
        never a silent no-op."""
        acts = getattr(self, "actions", None)
        if not acts:
            acts = getattr(getattr(self, "rc", None), "actions", None)
        return acts[0] if acts else None


class ContextLoaderRole(_MeteredRole):
    name: str = "ContextLoader"
    profile: str = "Neutral context loader"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([LoadContext])


class BullRole(_MeteredRole):
    name: str = "Bull"
    profile: str = "Argues for the thesis"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([ArgueBull])


class BearRole(_MeteredRole):
    name: str = "Bear"
    profile: str = "Argues against the thesis"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([ArgueBear])


class ModeratorRole(_MeteredRole):
    name: str = "Moderator"
    profile: str = "Neutral round moderator"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([ModerateRound])


class SynthesistRole(_MeteredRole):
    name: str = "Synthesist"
    profile: str = "Final synthesist"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([Synthesize])


# ════════════════════════════════════════════════════════════════════════════
# Orchestration + Layer-2 cost cap
# ════════════════════════════════════════════════════════════════════════════


async def _classify_and_build_outcome(
    synthesist: Any, thesis: str, synthesis: str, transcript: str,
    rounds: int, args: dict[str, Any],
) -> dict[str, Any]:
    """Run the grammar-constrained verdict classification + assemble the
    deliverable→vault ``outcome`` (#captures-to-vault-crews).

    Reuses the Synthesist's already-built local Ollama LLM (same lane — no
    egress), metered into ``_ROLES_LOG`` like a role action so the Layer-2 cost
    cap + the audit count its tokens. This is the crew's OWN finding (judged on
    the transcript), not a capture-time re-derivation. The target note is
    resolved HERE (the crew knows the run context): an explicit ``--deal`` arg
    lands the verdict on that deal's Companies note; otherwise it goes to a
    per-thesis ``Topics/Theses/<slug>.md`` note. A classification miss degrades
    to verdict=\"mixed\" (never fails the debate — the synthesis already
    succeeded)."""
    cfg = getattr(synthesist.llm, "config", None)
    base_url = getattr(cfg, "base_url", None) or "http://127.0.0.1:11434/api"
    model = getattr(cfg, "model", None) or "qwen3:14b"
    ts_start, t0 = _iso_now(), time.monotonic()
    call_ok = True
    try:
        data, tokens = await ollama_structured_chat(
            base_url, model, verdict_prompt(thesis, synthesis, transcript),
            VERDICT_SCHEMA,
        )
    except Exception:  # noqa: BLE001 — a verdict miss must not fail the debate
        data, tokens, call_ok = {}, 0, False
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICT_VALUES:
        verdict = "mixed"  # default-safe when the model returns an off-enum value
    recommended_action = str(data.get("recommended_action") or "").strip() or "n/a"
    _ROLES_LOG.append({
        "role": "Synthesist",
        "action": "ClassifyVerdict",
        "ts_start": ts_start,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "token_count": int(tokens or 0),
        "sensitivity": _sensitivity_tier(),
        "status": "ok" if call_ok else "error",   # honest audit on a model miss
        "output_summary": f"verdict={verdict}"[:200],
    })

    deal = str(args.get("deal") or "").strip()
    if deal:
        target_note, subject = f"Companies/{deal}.md", deal
    else:
        slug = thesis_slug(thesis)
        target_note, subject = f"Topics/Theses/{slug}.md", slug
    return {
        "subject": subject,
        "thesis": thesis,
        "verdict": verdict,
        "recommended_action": recommended_action,
        "rounds": rounds,
        "target_note": target_note,
    }


async def run_team(crew_input: CrewInput) -> CrewOutput:
    """Run the debate explicitly: ContextLoader → N×(Bull → Bear → Moderator)
    → Synthesist, checking the running token total against the cost cap after
    every LLM call (Layer 2 — the bridge's hook surface can't see inside a
    subprocess; the proxy's wall clock is the outer bound)."""
    thesis = resolve_thesis(crew_input.args)
    rounds = resolve_rounds(crew_input.args)
    # Load REAL vault evidence (integration: operator decision 2). ``args.
    # evidence`` overrides; otherwise the shared vault-scan finds thesis-relevant
    # notes the ContextLoader/Bull/Bear cite with [[wikilinks]]. No hits → the
    # roles argue from first principles (the prompts already forbid inventing a
    # citation). Filesystem read in the crew venv is NOT a boundary violation.
    evidence = load_vault_evidence(thesis, crew_input.args)

    _ROLES_LOG.clear()
    llm_cfg = crew_input.llm_config
    cost_cap = crew_input.cost_cap_tokens

    def _check_cap(phase: str) -> None:
        used = _tokens_so_far()
        if used > cost_cap:
            raise CostCapExceeded(
                f"debate crew exceeded {cost_cap} tokens at {phase}: used {used}"
            )

    context_loader = ContextLoaderRole(llm_cfg=llm_cfg)
    bull = BullRole(llm_cfg=llm_cfg)
    bear = BearRole(llm_cfg=llm_cfg)
    moderator = ModeratorRole(llm_cfg=llm_cfg)
    synthesist = SynthesistRole(llm_cfg=llm_cfg)

    brief = await context_loader.act_once(thesis=thesis, evidence=evidence)
    _check_cap("context-load")

    rounds_data: list[tuple[int, str, str, str]] = []
    prior_state = ""   # the moderator's running position summary
    prior_bear = ""     # the Bear's argument from the previous round
    for r in range(1, rounds + 1):
        tag = round_tag(r)
        bull_arg = await bull.act_once(
            log_tag=tag, thesis=thesis, brief=brief,
            prior_state=prior_state, prior_bear=prior_bear,
            round_no=r, rounds=rounds,
        )
        _check_cap(f"round {r} bull")
        bear_arg = await bear.act_once(
            log_tag=tag, thesis=thesis, brief=brief,
            prior_state=prior_state, current_bull=bull_arg,
            round_no=r, rounds=rounds,
        )
        _check_cap(f"round {r} bear")
        summary = await moderator.act_once(
            log_tag=tag, thesis=thesis, round_no=r, rounds=rounds,
            bull=bull_arg, bear=bear_arg, prior_state=prior_state,
        )
        _check_cap(f"round {r} moderator")
        rounds_data.append((r, bull_arg, bear_arg, summary))
        prior_state = summary
        prior_bear = bear_arg

    transcript = format_transcript(rounds_data)
    synthesis = await synthesist.act_once(
        thesis=thesis, brief=brief, transcript=transcript,
    )
    _check_cap("synthesis")

    # Structured CONCLUSION step (#captures-to-vault-crews): classify the debate
    # into a verdict + recommended action (the crew's OWN finding), and assemble
    # the operator-gated capture ``outcome`` incl. the resolved target note.
    outcome = await _classify_and_build_outcome(
        synthesist, thesis, synthesis, transcript, rounds, crew_input.args or {},
    )
    # NB: deliberately NO _check_cap here. The verdict step is a best-effort,
    # opt-in capture add-on (its LLM call is already try/except'd to degrade to
    # verdict="mixed"); its tokens are metered + reported, but they must NOT be
    # able to turn an already-complete debate into an error run — the
    # #captures-to-vault-crews "a capture miss never fails the run" contract.
    # Matches /explore, which also does not re-check the cap after its headline.

    roles_log = [RoleLogEntry(**e) for e in collect_roles_log(_ROLES_LOG)]
    # The deliverable IS the synthesis (consensus / disagreement / action) — it
    # becomes the chat bubble AND what the [📌 Save to Topics/Theses/<thesis>.md]
    # chip promotes. Chat-only by default: the crew writes no file (artefacts=[]).
    summary = synthesis.strip() or (
        roles_log[-1].output_summary if roles_log else "(no synthesis produced)"
    )
    return CrewOutput(
        run_id=crew_input.run_id,
        status="ok",
        summary=summary,
        artefacts=[],
        outcome=outcome,
        roles_log=roles_log,
        token_count=_tokens_so_far(),
    )


# ════════════════════════════════════════════════════════════════════════════
# Entry point (mirrors the hello_world template)
# ════════════════════════════════════════════════════════════════════════════


def _partial_roles_log() -> list[RoleLogEntry]:
    """Best-effort roles_log for ERROR envelopes — a cost-cap breach must audit
    the tokens/roles that triggered it, not zeros. Never raises."""
    try:
        return [RoleLogEntry(**e) for e in collect_roles_log(_ROLES_LOG)]
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
            run_id=crew_input.run_id,
            status="error",
            summary=f"Cost cap exceeded: {e}",
            roles_log=_partial_roles_log(),
            token_count=_tokens_so_far(),
            error=str(e),
        ))
        return 1
    except Exception as e:  # noqa: BLE001 — last-ditch structured envelope so
        # the bridge never sees a crashed process with empty stdout.
        write_result(CrewOutput(
            run_id=crew_input.run_id,
            status="error",
            summary=f"Crew crashed: {type(e).__name__}: {e}",
            roles_log=_partial_roles_log(),
            token_count=_tokens_so_far(),
            error=f"{type(e).__name__}: {e}",
        ))
        return 1


if __name__ == "__main__":
    sys.exit(main())
