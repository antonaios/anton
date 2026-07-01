"""DeepDive crew — ``/explore <target>`` (#33).

*"Synthesize what we know about X + what's interesting."* Five MetaGPT roles
over the #31 subprocess boundary (per [[autonomous-crews]] §2):

    UserRequirement(target=X)
        ├──▶ VaultArchaeologist  ─┐
        ├──▶ FinancialAnalyst    ─┤  (3 parallel BY_ORDER analysts;
        └──▶ IndustryAnalyst     ─┘   each fires on the kickoff)
                                  │
                                  ▼
                      Coordinator (fan-in sentinel)  ── publishes DeepDiveReady
                                  │                     only once all 3 emitted
                                  ▼                     (fakes a JOIN — MetaGPT
                              Synthesist                 has no first-class one)
                                  │
                                  ▼  one memo: what we know / what's odd /
                              final bubble   open Qs / next actions

This file is the MetaGPT GLUE only — every non-LLM operation (vault scan,
ticker/sector resolution, the bridge HTTP calls, the fan-in pack/unpack, the
prompt text) lives in ``explore_lib`` (stdlib-only, unit-tested in the bridge
venv). Mirrors ``hello_world_crew.py`` for the boundary mechanics; read its
header before changing the metering / stdout-capture / cost-cap scaffolding.

DATA-HANDLING NOTE: the whole crew runs on the LOCAL Ollama lane (v1 is
local-only on every tier). Vault notes — including confidential ones — are
read, summarised by local Ollama, and returned to the local audit/chat; none
of that content leaves the box. The ONLY external egress is the
FinancialAnalyst's markets call, and it transmits exactly one regex-validated
PUBLIC ticker (never the target name or any note content), and is skipped
entirely on an MNPI tier. See ``explore_lib.fetch_comps``.

Runs in ``<repo>\\crews\\.venv`` (Python 3.11). Exit codes match
hello_world: 0 = ok, 1 = ran-but-errored (structured CrewOutput), 2 = input
parse failure (stderr).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Drop loguru's default stderr sink BEFORE importing ANYTHING that could pull
# in metagpt (metagpt.const logs at import time; stderr non-empty is a fault
# signal to the bridge, spec §2.4). Done here — ahead of the _shared/explore_lib
# imports below — so the invariant doesn't depend on those modules staying
# metagpt-free at import (they are today; this removes the implicit coupling).
from loguru import logger as _loguru_root

_loguru_root.remove()

import explore_lib  # noqa: E402
from _shared.boundary import (  # noqa: E402
    CrewInput,
    CrewOutput,
    RoleLogEntry,
    capture_protocol_stream,
    collect_roles_log,
    read_input,
    write_error,
    write_result,
)
from _shared.ollama_config import build_ollama_llm_for_role, ollama_structured_chat  # noqa: E402
from metagpt.actions import Action  # noqa: E402
from metagpt.roles import Role  # noqa: E402
from metagpt.schema import Message  # noqa: E402
from metagpt.team import Team  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Manifest — mirrors routines/crew/registry.py::_REGISTRY["explore"]. Keep the
# field names + values in sync on both sides.
# ════════════════════════════════════════════════════════════════════════════

MANIFEST = {
    "verb": "explore",
    "module": "explore_crew",
    "sensitivity_override": None,        # inherit workspace tier
    "cost_cap_tokens": 80_000,           # [[autonomous-crews]] §1
    "cost_cap_seconds": 480,             # smoke-tuned 240->480 (registry sync)
    "roles": [
        "VaultArchaeologist",
        "FinancialAnalyst",
        "IndustryAnalyst",
        "Coordinator",
        "Synthesist",
    ],
    "models_default": {
        "VaultArchaeologist": "qwen3:14b",
        "FinancialAnalyst": "qwen3:14b",
        "IndustryAnalyst": "qwen3:14b",
        "Coordinator": "qwen3:8b",       # fan-in sentinel — no LLM call
        "Synthesist": "qwen3:14b",
    },
    "description": (
        "DeepDive — synthesize what the vault + market tools know about a "
        "company/topic, plus what's interesting. 5 roles, fan-in synthesis."
    ),
    # #captures-to-vault-crews: documentation mirror of the registry capture
    # block (the BRIDGE reads routines/crew/registry.py, not this dict). The crew
    # resolves {target_note} at runtime (Companies/<note>.md for a company,
    # Topics/<slug>.md for a sector) via explore_lib.resolve_capture_target.
    "captures_to_vault": {
        "target": "{target_note}",
        "section": "Deep-dive history",
        "fields": ["headline"],
        "headline": "Deep-dive — {target}: {headline}",
    },
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers (mirrors hello_world_crew; candidate for _shared extraction at the
# #32/#33/#36 integration, kept inline here to keep this crew self-contained).
# ════════════════════════════════════════════════════════════════════════════


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sensitivity_tier() -> str:
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


def _llm_total_tokens(llm: Any) -> int:
    """Best-effort total-token read from a MetaGPT LLM's cost manager
    (defensive across 0.8.x cost-manager attribute drift; 0 when unreadable)."""
    cm = getattr(llm, "cost_manager", None)
    if cm is None:
        return 0
    for attr in ("total_prompt_tokens", "total_completion_tokens"):
        if not hasattr(cm, attr):
            return int(getattr(cm, "total_tokens", 0) or 0)
    return int((cm.total_prompt_tokens or 0) + (cm.total_completion_tokens or 0))


def _redirect_output_streams(run_id: str) -> None:
    """Route MetaGPT/loguru logging + stray prints away from the boundary —
    same two redirects hello_world documents (logging→file, prints→sink)."""
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
    except Exception:  # noqa: BLE001 — logging must never kill the crew
        pass


# ════════════════════════════════════════════════════════════════════════════
# Metering bases (mirror hello_world_crew._MeteredAction / _MeteredRole).
# ════════════════════════════════════════════════════════════════════════════

_ROLES_LOG: list[dict[str, Any]] = []
# The Synthesist's FULL memo + the run's target, captured for the CrewOutput
# summary (the roles_log only keeps a 200-char output_summary).
_RESULT: dict[str, Any] = {}
# Each analyst's FULL output envelope, keyed by role ("vault"/"financial"/
# "industry"). Integration (operator decision: explore count-only): the analysts
# return a COUNT-ONLY string as their message content (so the audit
# output_summary never carries the full vault/financial/industry payload), and
# stash the real envelope HERE. The Coordinator reads this global for the fan-in
# data + completeness — the analyst MESSAGES still drive the _watch trigger
# timing, so fan-in semantics are unchanged; only the data source moved off the
# message body (the same module-global pattern /triage + /digest use). Reset in
# run_team (a subprocess == one run).
_ANALYST_OUTPUTS: dict[str, dict[str, Any]] = {}
# The Coordinator's packed DeepDiveReady payload (the three analyst envelopes
# keyed by role). Same count-only discipline as the analysts: the Coordinator
# returns a COUNT-ONLY message (so ITS audit row carries no target/excerpt/path
# from pack_deepdive) and stashes the full payload HERE; the Synthesist reads it
# from this global, not from the message content. Reset in run_team.
_DEEPDIVE: dict[str, Any] = {}


def _record_analyst(role_key: str, target: str, payload: dict[str, Any]) -> str:
    """Stash an analyst's FULL envelope in ``_ANALYST_OUTPUTS`` and return a
    COUNT-ONLY message string (the audit output_summary surface). The envelope
    is the same dict ``pack_analyst`` serialises — the Coordinator reads it from
    the global, not from the (now count-only) message."""
    _ANALYST_OUTPUTS[role_key] = {"role": role_key, "target": target, **payload}
    return _analyst_count_summary(role_key, payload)


def _analyst_count_summary(role_key: str, payload: dict[str, Any]) -> str:
    """A content-free, count-only one-liner for an analyst's audit row — never
    the free-text summary / cites / figures (those live in the module global +
    the final memo)."""
    if role_key == "vault":
        n = payload.get("hit_count")
        if n is None:
            n = len(payload.get("cites") or [])
        return f"VaultScanDone: {n} vault hit(s)"
    if role_key == "financial":
        has = payload.get("summary") is not None and payload.get("data") is not None
        return f"FinancialsDone: market data {'found' if has else 'none'}"
    if role_key == "industry":
        sector = payload.get("sector")
        n_src = len(payload.get("sources") or [])
        return f"IndustryDone: sector {'mapped' if sector else 'unmapped'}, {n_src} source(s)"
    return f"{role_key}Done"


class _MeteredAction(Action):
    """Action base that times the LLM call and stamps run metadata."""

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


class _MeteredRole(Role):
    """Role base that appends one roles-log entry per executed Action."""

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
# Actions — three analysts, the fan-in publisher, the synthesist.
# Each analyst returns a CODE-built JSON envelope (explore_lib.pack_analyst),
# so the Coordinator always parses valid JSON; the LLM only fills free text.
# ════════════════════════════════════════════════════════════════════════════


class ScanVault(_MeteredAction):
    """VaultArchaeologist: scan the vault for the target, summarise w/ cites."""

    name: str = "ScanVault"

    async def run(self, with_messages: list[Message]) -> str:
        target = str(with_messages[-1].content).strip()
        scan = explore_lib.scan_vault_for_target(explore_lib.vault_root(), target)
        if scan["hits"]:
            summary = await self._metered_aask(
                explore_lib.vault_prompt(target, scan)
            )
        else:
            # Deterministic + free: don't spend a token to say "nothing found".
            summary = f"No existing vault notes mention '{target}'."
        # COUNT-ONLY message; full envelope → module global (see _record_analyst).
        return _record_analyst("vault", target, {
            "summary": summary,
            "cites": scan["cites"],
            "hit_count": len(scan["hits"]),
        })


class AnalyzeFinancials(Action):
    """FinancialAnalyst: resolve a public ticker from the vault, call the
    engine's market tools, and render the figures DETERMINISTICALLY in code.

    Makes NO LLM call — the numbers come straight from the engine payload, so
    the model can neither compute nor invent a figure (constitution:
    no-llm-maths / no-invented-sources, codex SEV-1). Returns a NULL summary
    when no public ticker resolves (X is a topic/sector), on an MNPI tier, or
    when the tools return no usable data — never a fabricated number."""

    name: str = "AnalyzeFinancials"

    async def run(self, with_messages: list[Message]) -> str:
        target = str(with_messages[-1].content).strip()
        root = explore_lib.vault_root()
        note = explore_lib.find_company_note(root, target)
        ticker = (
            explore_lib.extract_ticker(note["frontmatter"]) if note else None
        )
        # No public ticker, or an MNPI tier → no external call at all, null.
        # COUNT-ONLY message; full envelope → module global (_record_analyst).
        if not explore_lib.is_valid_ticker(ticker) \
                or explore_lib.crew_sensitivity() == "MNPI":
            return _record_analyst("financial", target, {
                "summary": None, "ticker": None, "data": None,
            })
        base = explore_lib.bridge_base_url()
        # urllib is BLOCKING (up to its timeout); offload to a thread so it
        # can't stall the event loop while the Vault/Industry analysts' LLM
        # calls run concurrently in the same round-1 gather.
        comps = await asyncio.to_thread(explore_lib.fetch_comps, base, ticker)
        peers = await asyncio.to_thread(explore_lib.fetch_peers, base, ticker)
        # Tools unreachable OR empty payloads ({"rows": []}) → null, not the
        # LLM path (codex SEV-2) — an empty result is not data to narrate.
        if not explore_lib.has_market_data(comps, peers):
            return _record_analyst("financial", target, {
                "summary": None, "ticker": ticker, "data": None,
            })
        summary = explore_lib.render_financial_summary(target, ticker, comps, peers)
        return _record_analyst("financial", target, {
            "summary": summary, "ticker": ticker, "data": comps,
        })


class AnalyzeIndustry(_MeteredAction):
    """IndustryAnalyst: resolve the sector (target-is-a-sector, else the
    company note's sector wikilink), read the sector knowledge + newsletters,
    summarise positioning + risks."""

    name: str = "AnalyzeIndustry"

    async def run(self, with_messages: list[Message]) -> str:
        target = str(with_messages[-1].content).strip()
        root = explore_lib.vault_root()
        slug = explore_lib.resolve_sector_slug(root, target)
        if slug is None:
            note = explore_lib.find_company_note(root, target)
            if note:
                slug = explore_lib.extract_sector_slug(note["frontmatter"])
        if slug is None:
            return _record_analyst("industry", target, {
                "summary": f"No sector mapping for '{target}' in the vault.",
                "sector": None, "sources": [],
            })
        ctx = explore_lib.read_sector_context(root, slug)
        summary = await self._metered_aask(
            explore_lib.industry_prompt(target, ctx)
        )
        # COUNT-ONLY message; full envelope → module global (_record_analyst).
        return _record_analyst("industry", target, {
            "summary": summary, "sector": slug, "sources": ctx["sources"],
        })


class PublishDeepDiveReady(Action):
    """Coordinator's synthetic fan-in: collect the three analyst envelopes and
    publish DeepDiveReady ONLY once all three have emitted. NOT metered — it
    makes no LLM call (pure routing logic).

    Integration (operator decision: explore count-only): the analysts now return
    COUNT-ONLY message content and stash their FULL envelope in the
    ``_ANALYST_OUTPUTS`` module global, so this reads the data from THERE, not
    from ``with_messages`` content. The analyst MESSAGES still drive the trigger
    — the bridge's round semantics guarantee this Action only runs once all
    three analyst messages are buffered (a role with an empty buffer is idle and
    isn't scheduled) — so the fan-in TIMING is unchanged; only the data source
    moved off the message body. The completeness check is defensive (and, with
    the data off-message, also the place a genuinely-missing analyst surfaces)."""

    name: str = "PublishDeepDiveReady"

    async def run(self, with_messages: list[Message]) -> str:  # noqa: ARG002 — trigger only; data is read from the module global
        collected: dict[str, dict[str, Any]] = {}
        target = ""
        for role_key, payload in _ANALYST_OUTPUTS.items():
            # A malformed/empty/mislabelled envelope must NOT count toward the
            # fan-in (codex SEV-2) — else DeepDiveReady could publish with
            # missing data; an absent analyst then trips the check below and
            # main() emits a structured error instead of a misleadingly-OK memo.
            if not explore_lib.is_valid_analyst_payload(payload, role_key):
                continue
            collected[role_key] = payload
            target = target or str(payload.get("target") or "")
        seen = set(collected.keys())
        if not explore_lib.all_analysts_reported(seen):
            raise RuntimeError(
                f"DeepDive fan-in incomplete: have {sorted(seen)}, "
                f"need {sorted(explore_lib.REQUIRED_ANALYSTS)}"
            )
        # Stash the FULL packed payload in the module global; the Synthesist
        # reads it from there. Return a COUNT-ONLY message so the Coordinator's
        # OWN audit row carries no target / excerpt / path from pack_deepdive
        # (count-only discipline, like the analysts). The message still drives
        # the Synthesist's _watch trigger.
        _DEEPDIVE["payload"] = explore_lib.pack_deepdive(target or "the target", collected)
        _DEEPDIVE["target"] = target or "the target"
        return f"DeepDiveReady: {len(collected)} analyst(s) reported"


class SynthesizeDeepDive(_MeteredAction):
    """Synthesist: read the DeepDiveReady payload, write the final memo.

    Integration (explore count-only): reads the packed payload from the
    ``_DEEPDIVE`` module global (the Coordinator now returns a count-only
    message), not from the watched message content. The PublishDeepDiveReady
    MESSAGE still drives this role's _watch trigger."""

    name: str = "SynthesizeDeepDive"

    async def run(self, with_messages: list[Message]) -> str:  # noqa: ARG002 — trigger only; payload is read from the module global
        deepdive = explore_lib.unpack_deepdive(str(_DEEPDIVE.get("payload") or ""))
        memo = await self._metered_aask(explore_lib.synthesis_prompt(deepdive))
        _RESULT["memo"] = memo
        _RESULT["target"] = deepdive.get("target", "")
        return memo


# ════════════════════════════════════════════════════════════════════════════
# Roles — per-role LLM assigned BEFORE set_actions (Role._init_action pins the
# current llm onto each Action; assigning after leaves actions on the default).
# ════════════════════════════════════════════════════════════════════════════


class VaultArchaeologistRole(_MeteredRole):
    name: str = "VaultArchaeologist"
    profile: str = "Vault archaeologist"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([ScanVault])
        # No _watch — fires on the kickoff UserRequirement (target=X).


class FinancialAnalystRole(_MeteredRole):
    name: str = "FinancialAnalyst"
    profile: str = "Financial analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        # LLM assigned for symmetry/init but currently UNUSED — AnalyzeFinancials
        # renders the engine figures deterministically (no-llm-maths), so this
        # role burns 0 tokens. (Kept so adding qualitative color later is a
        # one-line change, not a wiring change.)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([AnalyzeFinancials])


class IndustryAnalystRole(_MeteredRole):
    name: str = "IndustryAnalyst"
    profile: str = "Industry analyst"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([AnalyzeIndustry])


class CoordinatorRole(_MeteredRole):
    name: str = "Coordinator"
    profile: str = "Fan-in coordinator"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        # An LLM is assigned for symmetry/init but PublishDeepDiveReady never
        # calls it — the Coordinator does pure routing, so it burns 0 tokens.
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([PublishDeepDiveReady])
        self._watch([ScanVault, AnalyzeFinancials, AnalyzeIndustry])


class SynthesistRole(_MeteredRole):
    name: str = "Synthesist"
    profile: str = "DeepDive synthesist"

    def __init__(self, llm_cfg: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.llm = build_ollama_llm_for_role(self.name, llm_cfg)
        self.set_actions([SynthesizeDeepDive])
        self._watch([PublishDeepDiveReady])   # the DeepDiveReady signal


# ════════════════════════════════════════════════════════════════════════════
# Team run + Layer-2 cost cap
# ════════════════════════════════════════════════════════════════════════════


class CostCapExceeded(RuntimeError):
    """Layer-2 cost-cap trip — surfaces as CrewOutput status="error"."""


def _tokens_so_far() -> int:
    return sum(int(e.get("token_count", 0) or 0) for e in _ROLES_LOG)


def _suggestions_footer(target: str) -> str:
    """The spec's output chips, rendered as a plain-text fallback so the
    operator sees the affordances even before the dashboard renders real chips.
    (Actually wiring the 'Save to Companies' click to the Inbox route is
    dashboard scope — see the build summary.)"""
    name = target or "<target>"
    return (
        "\n\n---\n_Suggested next:_ 📌 Save to `Companies/" + name + ".md` → "
        "Inbox · 🔄 Re-run with a different question · 🔎 Expand each role's "
        "contribution"
    )


async def _build_explore_outcome(synthesist: Any, target: str, memo: str) -> dict[str, Any]:
    """Structured one-line headline (the crew's own conclusion) + the resolved
    capture target, assembled into the deliverable→vault ``outcome``
    (#captures-to-vault-crews). Reuses the Synthesist's local Ollama LLM (same
    lane — no egress), metered into ``_ROLES_LOG``. A headline miss → an empty
    outcome (the route then skips the capture; the chat memo is unaffected)."""
    cfg = getattr(synthesist.llm, "config", None)
    base_url = getattr(cfg, "base_url", None) or "http://127.0.0.1:11434/api"
    model = getattr(cfg, "model", None) or "qwen3:14b"
    ts_start, t0 = _iso_now(), time.monotonic()
    call_ok = True
    try:
        data, tokens = await ollama_structured_chat(
            base_url, model, explore_lib.headline_prompt(target, memo),
            explore_lib.HEADLINE_SCHEMA,
        )
    except Exception:  # noqa: BLE001 — a headline miss must not fail the deepdive
        data, tokens, call_ok = {}, 0, False
    headline = str(data.get("headline") or "").strip()
    _ROLES_LOG.append({
        "role": "Synthesist",
        "action": "ExtractHeadline",
        "ts_start": ts_start,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "token_count": int(tokens or 0),
        "sensitivity": _sensitivity_tier(),
        "status": "ok" if call_ok else "error",   # honest audit on a model miss
        "output_summary": f"headline_set={bool(headline)}"[:200],
    })
    if not (headline and target):
        return {}
    target_note, subject = explore_lib.resolve_capture_target(
        explore_lib.vault_root(), target,
    )
    return {
        "subject": subject,
        "target": target,
        "headline": headline,
        "target_note": target_note,
    }


async def run_team(crew_input: CrewInput) -> CrewOutput:
    """Build the Team, kick off with the target, run round-by-round until idle,
    checking the running token total against the cost cap between rounds."""
    target = str(crew_input.args.get("target", "")).strip()
    if not target:
        raise ValueError("explore crew requires args.target")

    _ROLES_LOG.clear()
    _RESULT.clear()
    _ANALYST_OUTPUTS.clear()
    _DEEPDIVE.clear()
    llm_cfg = crew_input.llm_config
    team = Team()
    # Keep a handle to the Synthesist so its local LLM can be reused for the
    # post-run structured headline (#captures-to-vault-crews) — no extra llm build.
    synthesist = SynthesistRole(llm_cfg=llm_cfg)
    team.hire([
        VaultArchaeologistRole(llm_cfg=llm_cfg),
        FinancialAnalystRole(llm_cfg=llm_cfg),
        IndustryAnalystRole(llm_cfg=llm_cfg),
        CoordinatorRole(llm_cfg=llm_cfg),
        synthesist,
    ])
    team.run_project(target)

    env = team.env
    cost_cap = crew_input.cost_cap_tokens
    running_tokens = 0
    # 10 rounds: fan-out (1) + fan-in (1) + synthesis (1) + headroom. The
    # bridge's wall-clock timeout is the outer bound; this is the inner one.
    for round_i in range(10):
        await env.run()
        running_tokens = _tokens_so_far()
        if running_tokens > cost_cap:
            raise CostCapExceeded(
                f"explore crew exceeded {cost_cap} tokens at round "
                f"{round_i}: used {running_tokens}"
            )
        if env.is_idle:
            break

    if not env.is_idle:
        raise RuntimeError(
            f"crew did not reach idle within 10 rounds "
            f"({len(_ROLES_LOG)} role action(s) completed)"
        )

    memo = str(_RESULT.get("memo") or "").strip()
    resolved_target = str(_RESULT.get("target") or target)
    # Structured CONCLUSION step (#captures-to-vault-crews): a one-line headline
    # (the crew's own takeaway) + the resolved capture target. POST-deliverable +
    # best-effort — a miss yields an empty outcome (the route then skips the
    # capture) and never disturbs the chat memo. Metered into _ROLES_LOG, so it
    # runs BEFORE roles_log is built below; its tokens are in token_count.
    outcome = await _build_explore_outcome(synthesist, resolved_target, memo) if memo else {}

    roles_log = [RoleLogEntry(**r) for r in collect_roles_log(_ROLES_LOG)]
    if not memo:
        # Synthesist never produced a memo — degrade to the last role output
        # rather than an empty bubble (and flag it for the audit).
        memo = roles_log[-1].output_summary if roles_log else "(no synthesis)"
    summary = memo + _suggestions_footer(resolved_target)
    return CrewOutput(
        run_id=crew_input.run_id,
        status="ok",
        summary=summary,
        artefacts=[],            # chat-only; "Save to Companies" is a chip
        outcome=outcome,
        roles_log=roles_log,
        token_count=_tokens_so_far(),
    )


# ════════════════════════════════════════════════════════════════════════════
# Entry point (mirrors hello_world_crew.main)
# ════════════════════════════════════════════════════════════════════════════


def _partial_roles_log() -> list[RoleLogEntry]:
    """Best-effort roles_log for ERROR envelopes — telemetry must survive the
    fault and must never mask it."""
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
    except Exception as e:  # noqa: BLE001 — last-ditch structured envelope
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
