"""Hello-world crew — 3-role smoke test for the MetaGPT subprocess boundary.

OUTSTANDING #31. Deployed by ``routines/crew/install/install_metagpt.py`` from
``<routines repo>/crews_src/`` to ``<repo>\\crews\\`` — the repo copy
is the single source of truth; never edit the deployed copy in place.

This file is the template every future crew copies as a starting point:
  * #32 /triage  → crews/triage_crew.py
  * #33 /explore → crews/explore_crew.py
  * #36 /debate  → crews/debate_crew.py

What it demonstrates (READ BEFORE COPYING):
  1. one-line JSON on stdin → Pydantic ``CrewInput`` (``boundary.read_input``)
  2. MetaGPT Role + Action construction with a linear ``_watch`` chain
  3. per-role Ollama LLM config (bridge resolves sensitivity → models) —
     assigned BEFORE ``set_actions`` so ``Role._init_action`` pins it onto
     each Action (assigning after leaves actions on the config-default LLM)
  4. per-Action metering (``_MeteredRole``) collected into a crew-owned
     roles log — metagpt 0.8.x ``env.history`` is a debug string and
     ``Message`` can't carry metadata, so the crew records entries itself
  5. in-subprocess cost-cap counter (Layer 2 of the two-layer enforcement —
     the bridge's after-hook surface is empty for a subprocess, so THIS is
     the actual enforcement; spec §5.3)
  6. single JSON result line on stdout; stderr reserved for fatal errors
     (MetaGPT/loguru logging is redirected to ``.logs/<run_id>.log`` at
     startup — loguru defaults to stderr, which would trip the bridge's
     fault-signal contract; flagged template fix)

Runs in ``<repo>\\crews\\.venv`` (Python 3.11). The bridge NEVER
imports this file — the boundary is ``subprocess.Popen``.

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
from _shared.ollama_config import build_ollama_llm_for_role

# Drop loguru's default stderr sink BEFORE any metagpt import:
# ``metagpt.const`` logs "Package root set to …" through the raw loguru
# logger at import time — long before main() can redirect logging — and
# stderr non-empty is a fault signal to the bridge (spec §2.4). MetaGPT's
# own ``define_log_level`` re-adds its sinks later; main()'s
# ``_redirect_output_streams`` retargets those to the per-run log file.
# Found by the real-boundary smoke run, 2026-06-10.
from loguru import logger as _loguru_root

_loguru_root.remove()

# MetaGPT imports — only valid inside the crew venv. Deliberately AFTER the
# _shared imports (so an input-parse error can be reported even when the
# MetaGPT install is broken) and AFTER the loguru sink drop above (E402 is
# the point: importing metagpt earlier writes to stderr).
from metagpt.actions import Action  # noqa: E402
from metagpt.roles import Role  # noqa: E402
from metagpt.schema import Message  # noqa: E402
from metagpt.team import Team  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Manifest — routines/crew/registry.py duplicates this shape for
# verb→sensitivity routing. Keep field names identical on both sides.
# ════════════════════════════════════════════════════════════════════════════

MANIFEST = {
    "verb": "hello_world",
    "module": "hello_world_crew",
    "sensitivity_override": None,    # None = inherit from workspace tier
    "cost_cap_tokens": 10_000,
    # 300, not the spec's 60: the bridge clamps the wall clock to this and
    # the 2026-06-10 real-boundary smoke measured 99s healthy wall time
    # (qwen3:14b cold-load dominates) — 60s would kill every cold run.
    "cost_cap_seconds": 300,
    "roles": ["Analyst", "Reviewer", "Synthesist"],
    "models_default": {
        "Analyst": "qwen3:14b",
        "Reviewer": "qwen3:8b",
        "Synthesist": "qwen3:14b",
    },
    "description": "3-role smoke crew for boundary verification. Not user-facing.",
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sensitivity_tier() -> str:
    """Tier the bridge resolved BEFORE launching (env-injected); echoed into
    every role row so the audit carries it."""
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


def _llm_total_tokens(llm: Any) -> int:
    """Best-effort total-token read from a MetaGPT LLM's cost manager.

    Defensive across 0.8.x minor drift: cost-manager attribute names have
    moved before. Returns 0 when nothing is readable — the smoke test's
    token-consistency acceptance check (spec §6.4 #5) is what validates this
    against the real install."""
    cm = getattr(llm, "cost_manager", None)
    if cm is None:
        return 0
    for attr in ("total_prompt_tokens", "total_completion_tokens"):
        if not hasattr(cm, attr):
            return int(getattr(cm, "total_tokens", 0) or 0)
    return int((cm.total_prompt_tokens or 0) + (cm.total_completion_tokens or 0))


def _redirect_output_streams(run_id: str) -> None:
    """Route EVERYTHING except protocol envelopes away from the boundary.

    Two redirects, both load-bearing for the bridge's strict contract:
      * MetaGPT/loguru logging → ``.logs/<run_id>.log`` (loguru defaults to
        stderr; stderr non-empty is a fault signal — spec §2.4).
      * stray ``print()``s (agentops banners, dependency chatter) →
        ``.logs/<run_id>.stdout.log`` via ``capture_protocol_stream`` (the
        bridge hard-fails on any non-JSON stdout line).
    """
    # Sanitize before using in a FILENAME — the route generates well-formed
    # run_ids, but a directly-invoked crew must not be able to traverse out
    # of .logs/ via a hostile run_id (codex-5.5 xhigh SEV-3).
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
    except Exception:  # noqa: BLE001 — logging must never kill the crew;
        pass           # worst case loguru keeps stderr and the smoke run
                       # surfaces it as the fault it would be in prod.


# ════════════════════════════════════════════════════════════════════════════
# Actions — each stamps ``_last_run_meta`` for the audit walk.
# ════════════════════════════════════════════════════════════════════════════


class _MeteredAction(Action):
    """Action base that times the LLM call and stamps run metadata.

    ``_MeteredRole._act`` folds ``_last_run_meta`` into the crew-owned
    ``_ROLES_LOG`` entry for this act. (The staged template stamped
    ``Message.metadata`` instead — but metagpt 0.8.x ``Message`` is a closed
    pydantic model with no such field; found by the real-boundary smoke.)
    """

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


class AnalyzeTopic(_MeteredAction):
    """Analyst's only Action: 5-bullet analysis + 2 source mentions."""

    name: str = "AnalyzeTopic"

    PROMPT_TEMPLATE: str = """\
You are an analyst. Given the topic '{topic}', produce:
  * 5 bullet-point observations (one line each)
  * 2 source mentions (URL or citation key)

Plain text only. Be concise."""

    async def run(self, with_messages: list[Message]) -> str:
        topic = with_messages[-1].content
        return await self._metered_aask(self.PROMPT_TEMPLATE.format(topic=topic))


class CritiqueAnalysis(_MeteredAction):
    """Reviewer's only Action: 2-3 weaknesses or open questions."""

    name: str = "CritiqueAnalysis"

    PROMPT_TEMPLATE: str = """\
You are a reviewer. The analyst produced:

{analysis}

List 2-3 weaknesses, open questions, or things the analyst missed.
Plain text bullet points only."""

    async def run(self, with_messages: list[Message]) -> str:
        analysis = with_messages[-1].content
        return await self._metered_aask(self.PROMPT_TEMPLATE.format(analysis=analysis))


class SynthesizeFinal(_MeteredAction):
    """Synthesist's only Action: 3-sentence synthesis of analysis + critique."""

    name: str = "SynthesizeFinal"

    PROMPT_TEMPLATE: str = """\
The analyst wrote:

{analysis}

The reviewer responded:

{critique}

In 3 sentences (no more), give the final synthesis."""

    async def run(self, with_messages: list[Message]) -> str:
        analysis = ""
        critique = ""
        for m in with_messages:
            tag = str(m.cause_by).rsplit(".", 1)[-1]
            if tag == "AnalyzeTopic":
                analysis = m.content
            elif tag == "CritiqueAnalysis":
                critique = m.content
        return await self._metered_aask(
            self.PROMPT_TEMPLATE.format(analysis=analysis, critique=critique)
        )


# ════════════════════════════════════════════════════════════════════════════
# Roles — linear chain: Analyst (kickoff) → Reviewer → Synthesist.
# ════════════════════════════════════════════════════════════════════════════


# Crew-owned roles log — one RoleLogEntry-shaped dict per executed Action,
# appended by ``_MeteredRole._act``. A subprocess is one run, so module
# scope is the run scope (``run_team`` clears it defensively anyway).
# WHY NOT env.history / Message.metadata: on metagpt 0.8.x the former is a
# debug STRING and the latter doesn't exist (closed pydantic model) — the
# staged template's walk came back empty against the real install
# (real-boundary smoke run, 2026-06-10).
_ROLES_LOG: list[dict[str, Any]] = []


class _MeteredRole(Role):
    """Role base that appends one roles-log entry per executed Action."""

    async def _act(self) -> Message:
        todo = self.rc.todo  # capture — super()._act() advances the state
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


# NOTE for every role below: the per-role LLM is assigned BEFORE
# ``set_actions`` — ``Role._init_action`` pins the role's CURRENT ``llm``
# onto each Action (override=True). Assigning after ``set_actions`` leaves
# the actions on the config-default LLM: the real-boundary smoke run showed
# all three roles silently using the default model that way.


class AnalystRole(_MeteredRole):
    name: str = "Analyst"
    profile: str = "Topic analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([AnalyzeTopic])
        # No _watch — Analyst fires on the kickoff UserRequirement message.


class ReviewerRole(_MeteredRole):
    name: str = "Reviewer"
    profile: str = "Critique reviewer"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([CritiqueAnalysis])
        self._watch([AnalyzeTopic])


class SynthesistRole(_MeteredRole):
    name: str = "Synthesist"
    profile: str = "Final synthesist"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([SynthesizeFinal])
        self._watch([CritiqueAnalysis])


# ════════════════════════════════════════════════════════════════════════════
# Team run + Layer-2 cost cap
# ════════════════════════════════════════════════════════════════════════════


class CostCapExceeded(RuntimeError):
    """Layer-2 cost-cap trip — surfaces as CrewOutput status="error"."""


def _tokens_so_far() -> int:
    """Running token total from the crew-owned roles log."""
    return sum(int(e.get("token_count", 0) or 0) for e in _ROLES_LOG)


async def run_team(crew_input: CrewInput) -> CrewOutput:
    """Build the Team, kick off with the topic, run round-by-round until idle
    — checking the running token total against the cost cap BETWEEN rounds
    (Layer 2; the bridge's hook surface can't see inside a subprocess)."""
    topic = str(crew_input.args.get("topic", "")).strip()
    if not topic:
        raise ValueError("hello_world crew requires args.topic")

    _ROLES_LOG.clear()
    llm_cfg = crew_input.llm_config
    team = Team()
    team.hire([
        AnalystRole(llm_cfg=llm_cfg),
        ReviewerRole(llm_cfg=llm_cfg),
        SynthesistRole(llm_cfg=llm_cfg),
    ])
    team.run_project(topic)

    env = team.env
    cost_cap = crew_input.cost_cap_tokens
    running_tokens = 0
    # 6 rounds: a 3-step linear chain + headroom. The bridge's wall-clock
    # timeout is the outer bound; this is the inner one.
    for round_i in range(6):
        await env.run()
        running_tokens = _tokens_so_far()
        if running_tokens > cost_cap:
            raise CostCapExceeded(
                f"hello_world crew exceeded {cost_cap} tokens at round "
                f"{round_i}: used {running_tokens}"
            )
        if env.is_idle:
            break

    if not env.is_idle:
        # Round exhaustion is a FAULT (codex-5.5 xhigh, 2026-06-10): a
        # broken watch chain or a looping role must not masquerade as a
        # healthy run. main() folds the partial telemetry into the
        # structured error envelope.
        raise RuntimeError(
            f"crew did not reach idle within 6 rounds "
            f"({len(_ROLES_LOG)} role action(s) completed)"
        )

    roles_log = [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    summary = roles_log[-1].output_summary if roles_log else "(no output)"
    return CrewOutput(
        run_id=crew_input.run_id,
        status="ok",
        summary=summary,
        artefacts=[],            # hello_world writes no files
        roles_log=roles_log,
        token_count=running_tokens,
    )


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════


def _partial_roles_log() -> list[RoleLogEntry]:
    """Best-effort roles_log for ERROR envelopes — a cost-cap breach must
    audit the tokens/roles that triggered it, not zeros (codex-5.5 xhigh,
    2026-06-10). Never raises: telemetry must not mask the original fault."""
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
