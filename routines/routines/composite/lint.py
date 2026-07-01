"""Composite-template linter (#steal-kocoro P2) — boot-time + ``lint-composites``.

Validates ANTON's composite orchestration JSONs the way the #61 skill registry
validates ``SKILL.md`` frontmatter: at bridge boot a malformed composite refuses
to BOOT (HARD-FAIL) rather than blowing up on the first ``/pitch`` run, and the
same checks run on demand via the ``lint-composites`` console script /
``python -m routines.composite.lint`` (the ``make lint-composites`` target).

## Why a validator (the Kocoro-lab/Shannon steal)

Kocoro-lab/Shannon validates its declarative workflow templates at load
(per-node budget, tool-allowlist, acyclicity, budget hierarchy). ANTON owns its
orchestration JSONs at the Synapse HTTP boundary (they are **ANTON data**, the
schema is Synapse's — [[composite-skills]] §8 AGPL containment). This module
encodes the documented **Synapse-spike footguns** (``SYNAPSE-SPIKE-RESULTS-
2026-05-26.md``) as machine checks so an author can't reintroduce a trap a future
``/pitch`` run would only surface at runtime:

  1. **Host-agent pinning** (spike Item 2, Finding #3 — "tools invisible to the
     dispatcher"). A ``tool``/``agent`` step with a null ``agent_id`` defaults to
     Synapse's Builder agent[0], whose tool list excludes every custom tool, so
     the tool goes invisible (``"Tool 'X' not found in available tools"``). Every
     such step MUST pin a host ``agent_id``; when the composite declares its host
     agents (``anton.host_agents``) the linter also checks the agent actually
     lists the step's ``forced_tool``.
  2. **EXTRACT_JSON-after-TOOL** (spike Item 2, Finding #2). A ``tool`` step's
     result lands in ``shared_state[output_key]`` as a JSON **string**, not a
     dict. Any typed consumer that field-accesses it (``{state.lbo_result.ftev}``)
     fails — it must first route the string through an ``extract_json`` step and
     read THAT step's parsed ``output_key``.
  3. **DAG acyclicity.** The control-flow graph (``next_step_id`` + conditional /
     parallel branch targets) must be acyclic. (A ``loop`` step iterates its
     ``loop_step_ids`` body INTERNALLY, bounded by ``loop_count`` — that is not a
     graph back-edge, so it is excluded from the cycle check but still
     resolution-checked.)
  4. **Per-step sensitivity ≤ composite max-tier.** A composite routes its whole
     DAG at one tier (``composite tier = max of steps`` — vault ``CLAUDE.md`` §4
     + the lane-taxonomy §12 rule 2; the sensitivity guard fires BEFORE Synapse
     using it). A step declared MORE sensitive than the composite's
     ``anton.max_sensitivity`` would be routed too permissively — a leak — so it
     is rejected at boot.

Plus structural integrity (valid JSON, known step types, unique ids, every edge
+ ``entry_step_id`` resolves, ``id`` matches the filename key) — the table-stakes
that make the four checks meaningful and a malformed file a boot-refusal (the
existing #61 posture).

## The ANTON overlay

A composite JSON is Synapse-native orchestration JSON PLUS one ANTON-overlay
object, ``anton`` (namespaced so the Phase-6 loader can strip it cleanly before
POSTing to Synapse — keeps the "JSONs are ANTON data" boundary)::

    "anton": {
      "max_sensitivity": "confidential",                      # REQUIRED — the tier the whole DAG routes at
      "step_sensitivity": {"lbo": "confidential", ...},       # REQUIRED + COMPLETE — every step's tier (no inheritance)
      "host_agents": {                                        # OPTIONAL — host-agent tool inventory for the cross-check
        "agent_pitch_host": {"tools": ["lbo", "comps", "dcf"]}
      }
    }

Both ``max_sensitivity`` and ``step_sensitivity`` are REQUIRED (parity with #61's
required ``metadata.sensitivity``). ``step_sensitivity`` must cover EVERY step
explicitly — an undeclared step must NOT silently inherit a lower
``max_sensitivity`` and route too permissively (fail-closed, the §5 sensitivity
boundary). An undeclared composite tier can't be routed safely.

## Scope

PURE: this module only reads JSON files and returns error strings — no Synapse
contact, no network, no event-bus / hook registration, no writes. The Phase-6
loader (``routines/composite/registry.py``, POST-to-Synapse) is a separate
concern; it will call :func:`validate_all` before registering. Until Phase 6
authors any ``composites/*.orchestration.json`` the boot check is a no-op (an
absent / empty composites dir validates clean).

Public API (mirrors ``routines.skills.registry``):
  * :func:`validate_composite` — validate one parsed composite dict (pure).
  * :func:`validate_file` / :func:`validate_all` — file + directory sweeps.
  * :func:`validate_or_raise` — the boot gate (raises ``RuntimeError``).
  * :func:`main` — the ``lint-composites`` CLI.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import click

# ─────────────────────────────────────────────────────────────────────────────
# Locations
# ─────────────────────────────────────────────────────────────────────────────
# Composites live at the REPO-ROOT ``composites/`` dir — NOT inside the package
# ([[composite-skills]] §2: ``<repo>\routines\composites\<key>.orchestration.json``).
# This module is ``<repo>/routines/composite/lint.py`` → ``parents[2]`` is the
# repo root (parents[0]=composite, [1]=routines pkg, [2]=repo root). Editable
# installs leave ``__file__`` on the real source tree, so this resolves the
# checked-out repo's composites dir.
_REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSITES_DIR = _REPO_ROOT / "composites"
COMPOSITES_DIR_ENV = "AGENTIC_COMPOSITES_DIR"   # operator/test override (absolute dir)
COMPOSITE_GLOB = "*.orchestration.json"
_ORCH_SUFFIX = ".orchestration.json"

# ─────────────────────────────────────────────────────────────────────────────
# Synapse-native step palette (v1.6.4 — 14 executors; SYNAPSE-SPIKE §Item 4)
# ─────────────────────────────────────────────────────────────────────────────
_STEP_TYPES = (
    "agent", "tool", "evaluator", "parallel", "merge", "loop", "human",
    "transform", "llm", "extract_json", "if_else", "switch", "print", "end",
)
# Steps that dispatch a tool through a host agent. A null ``agent_id`` on one of
# these defaults to Synapse's Builder agent[0], whose tool list excludes every
# custom tool — the tool then goes invisible to the dispatcher (spike Finding #3).
_AGENT_BEARING_TYPES = ("tool", "agent")

# ─────────────────────────────────────────────────────────────────────────────
# ANTON sensitivity tiers (vault CLAUDE.md §4). Ordered low→high; rank = index.
# Mirrors ``routines.skills.registry._SENSITIVITY_TIERS`` (the code's Literal is
# the source of truth the runtime gates against) — re-declared, not imported, to
# match that module's own local-definition pattern.
# ─────────────────────────────────────────────────────────────────────────────
_SENSITIVITY_TIERS = ("public", "internal", "confidential", "MNPI")

# Recognised keys inside the top-level ``anton`` overlay block. Anything else is
# a typo and is rejected (parity with the #61 capability/requires blocks).
_ANTON_KEYS = ("max_sensitivity", "step_sensitivity", "host_agents")

# The EXTRACT_JSON check scans EVERY string value in a step (recursively, into
# nested dicts/lists) for ``{state.K.field}`` references rather than a fixed list
# of template fields — a fixed list risks a false NEGATIVE if Synapse renders a
# field we didn't enumerate (codex SEV-2). The specific regex below keeps the
# false-POSITIVE risk negligible (literal ``{state.x.y}`` in non-template prose is
# vanishingly unlikely). The only field deliberately skipped is ``transform_code``
# — arbitrary Python whose state access is ``state['k']['f']`` (bracket syntax the
# regex never matches anyway), and ANTON routes shaping to bridge ``_compose``
# endpoints rather than Docker-gated TRANSFORM steps (spike operator-decision #2).
_NON_TEMPLATE_SCAN_FIELDS = ("transform_code",)
# ``{state.KEY}`` (group2 None → whole value) vs ``{state.KEY.FIELD...}``
# (group2 = the dotted tail → typed field access).
_STATE_REF_RE = re.compile(
    r"\{state\.([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z_][A-Za-z0-9_]*)+)?\}"
)


def _iter_state_refs(value: Any, path: str):
    """Yield ``(json_path, match)`` for every ``{state.K.field...}`` reference in
    any string nested inside ``value`` (descending into dicts + lists). ``path`` is
    the dotted/indexed JSON path naming the offending field in an error. ITERATIVE
    (explicit stack) so a deeply nested step payload can't raise ``RecursionError``
    and break the "return errors, never crash" contract (codex r3)."""
    stack: list[tuple[Any, str]] = [(value, path)]
    while stack:
        val, p = stack.pop()
        if isinstance(val, str):
            for match in _STATE_REF_RE.finditer(val):
                yield p, match
        elif isinstance(val, dict):
            for key, child in val.items():
                stack.append((child, f"{p}.{key}" if p else str(key)))
        elif isinstance(val, list):
            for i, child in enumerate(val):
                stack.append((child, f"{p}[{i}]"))


# ─────────────────────────────────────────────────────────────────────────────
# Edge helpers
# ─────────────────────────────────────────────────────────────────────────────
# Keys a dict-shaped edge target may carry its step-id under. Observed Synapse
# v1.6.4 targets are plain step-id strings; the dict form is defensive coverage
# for an object branch shape.
_TARGET_ID_KEYS = ("step_id", "id", "target", "to", "next_step_id")


def _resolve_target(v: Any) -> tuple[Optional[str], bool]:
    """Resolve one edge-target slot to ``(step_id | None, malformed)``.

    ``None``/absent → ``(None, False)`` (no edge — the schema default). A non-empty
    string → ``(str, False)``. A dict carrying a non-empty-string id under one of
    :data:`_TARGET_ID_KEYS` → ``(id, False)``. ANYTHING ELSE — an int/bool, an
    empty string, a list, or a dict with no recognised id key — is MALFORMED:
    ``(None, True)``. Malformed targets are reported, never silently dropped as
    "no edge" (codex r3 SEV-2)."""
    if v is None:
        return None, False
    if isinstance(v, str):
        return (v, False) if v else (None, True)  # empty string is malformed
    if isinstance(v, dict):
        for k in _TARGET_ID_KEYS:
            t = v.get(k)
            if isinstance(t, str) and t:
                return t, False
        return None, True
    return None, True  # int / bool / list / other → malformed


def _describe_bad_target(v: Any) -> str:
    """A NON-sensitive description of a malformed edge target — its TYPE/shape,
    NEVER its raw value (security review: a misplaced value in an edge field must
    not be echoed into boot logs). A malformed string slot can only be the empty
    string (a non-empty string is a valid target); a malformed dict has no
    recognised id key; everything else is described by its type name."""
    if isinstance(v, str):
        return "an empty string"
    if isinstance(v, dict):
        return "an object with no recognised step-id key"
    return f"a value of type {type(v).__name__}"


def _collect_edges(step: dict) -> tuple[list[str], list[str], list[str]]:
    """Return ``(successor_ids, loop_body_ids, shape_errors)`` for one step.

    ``successor_ids`` are the control-flow edges for cycle detection. ``loop_body_ids``
    are ``loop_step_ids`` — resolution-checked but EXCLUDED from the cycle graph
    (a Synapse ``loop`` iterates its body INTERNALLY, bounded by ``loop_count``, so
    it is not a graph back-edge). ``shape_errors`` name any MALFORMED edge target
    (field-relative; the caller prefixes the composite + step). Resolved ids may be
    dangling — existence is checked separately against the step-id set."""
    successors: list[str] = []
    loop_body: list[str] = []
    errors: list[str] = []

    def _scalar(field: str) -> None:
        rid, bad = _resolve_target(step.get(field))
        if bad:
            errors.append(
                f"edge field {field!r} has a malformed target: "
                f"{_describe_bad_target(step.get(field))} "
                f"(expected null or a non-empty step-id string)"
            )
        elif rid is not None:
            successors.append(rid)

    def _values(field: str) -> None:
        mapping = step.get(field)
        if mapping is None:
            return
        if not isinstance(mapping, dict):
            errors.append(f"edge field {field!r} must be an object, got {type(mapping).__name__}")
            return
        for label, v in mapping.items():
            rid, bad = _resolve_target(v)
            if bad:
                errors.append(
                    f"edge field {field!r}[{label!r}] has a malformed target: "
                    f"{_describe_bad_target(v)} (expected a non-empty step-id string)"
                )
            elif rid is not None:
                successors.append(rid)

    def _sequence(field: str, sink: list[str]) -> None:
        seq = step.get(field)
        if seq is None:
            return
        if not isinstance(seq, list):
            errors.append(f"edge field {field!r} must be a list, got {type(seq).__name__}")
            return
        for i, v in enumerate(seq):
            rid, bad = _resolve_target(v)
            if bad:
                errors.append(
                    f"edge field {field!r}[{i}] has a malformed target: "
                    f"{_describe_bad_target(v)} (expected a non-empty step-id string)"
                )
            elif rid is not None:
                sink.append(rid)

    _scalar("next_step_id")
    _scalar("if_true_step_id")
    _scalar("if_false_step_id")
    _scalar("switch_default_step_id")
    _values("route_map")
    _values("switch_cases")
    _sequence("parallel_branches", successors)
    _sequence("loop_step_ids", loop_body)  # body: resolution-checked, not a cycle edge
    return successors, loop_body, errors


def _find_cycle(step_ids: list[str], succ: dict[str, list[str]]) -> Optional[list[str]]:
    """Return one cycle as ``[a, b, ..., a]`` (closed), or ``None`` if the graph
    is acyclic. ITERATIVE three-colour DFS (explicit stack) so even a pathological
    thousands-of-steps composite can't raise ``RecursionError`` and break the
    "return errors, never crash" contract (codex SEV-3). ``succ`` is pre-filtered
    to known step ids so a dangling edge (reported separately) never confuses the
    walk."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in step_ids}

    for root in step_ids:
        if color[root] != WHITE:
            continue
        # stack frames: (node, iterator over its successors). ``path`` mirrors the
        # GRAY nodes currently on the stack so a back-edge reconstructs the cycle.
        stack: list[tuple[str, Any]] = [(root, iter(succ.get(root, ())))]
        color[root] = GRAY
        path: list[str] = [root]
        while stack:
            node, it = stack[-1]
            descended = False
            for v in it:
                vcolor = color.get(v)
                if vcolor == GRAY:                      # back-edge → cycle
                    return path[path.index(v):] + [v]
                if vcolor == WHITE:
                    color[v] = GRAY
                    path.append(v)
                    stack.append((v, iter(succ.get(v, ()))))
                    descended = True
                    break
                # BLACK (fully explored) → not part of an open cycle; skip.
            if not descended:                            # node exhausted
                color[node] = BLACK
                stack.pop()
                path.pop()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The four checks
# ─────────────────────────────────────────────────────────────────────────────
def _check_host_agents(
    data: dict, step_map: dict[str, dict], step_ids: list[str], who: str
) -> list[str]:
    """Check 1 — host-agent pinning (spike Item 2, Finding #3).

    Every ``tool``/``agent`` step must pin a non-empty ``agent_id``. When the
    composite declares its host-agent tool inventory (``anton.host_agents``) the
    pinned agent must be declared there AND list the step's ``forced_tool`` — the
    exact ``"Tool 'X' not found in available tools"`` footgun, caught statically."""
    errors: list[str] = []
    anton = data.get("anton")
    host_agents = anton.get("host_agents") if isinstance(anton, dict) else None
    inventory = host_agents if isinstance(host_agents, dict) else None

    for sid in step_ids:
        step = step_map[sid]
        stype = step.get("type")
        if stype not in _AGENT_BEARING_TYPES:
            continue
        agent_id = step.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            errors.append(
                f"{who}: {stype} step {sid!r} must pin a host 'agent_id' — a null "
                f"agent_id defaults to Synapse's Builder agent, whose tool list "
                f"excludes every custom tool, so the tool goes invisible to the "
                f"dispatcher (SYNAPSE-SPIKE Item 2 Finding #3)"
            )
            continue
        if inventory is None:
            continue  # no declared inventory to cross-check against
        agent_def = inventory.get(agent_id)
        if not isinstance(agent_def, dict):
            errors.append(
                f"{who}: step {sid!r} pins agent_id {agent_id!r}, which is not "
                f"declared in anton.host_agents"
            )
            continue
        # Cross-check the forced tool for BOTH tool AND agent steps (codex SEV-2):
        # an `agent` step that forces a tool its host agent can't see hits the same
        # "Tool 'X' not found in available tools" dispatcher failure as a tool step.
        forced = step.get("forced_tool")
        if isinstance(forced, str) and forced:
            tools = agent_def.get("tools")
            tool_list = (
                [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
            )
            if forced not in tool_list:
                errors.append(
                    f"{who}: {stype} step {sid!r} forces tool {forced!r} but its host "
                    f"agent {agent_id!r} does not list it in tools {tool_list} — the "
                    f"dispatcher would report 'Tool {forced!r} not found'"
                )
    return errors


def _check_extract_json_after_tool(
    step_map: dict[str, dict], step_ids: list[str], who: str
) -> list[str]:
    """Check 2 — EXTRACT_JSON-after-TOOL (spike Item 2, Finding #2).

    A ``tool`` step's ``output_key`` holds a JSON STRING. Any step that
    field-accesses it (``{state.K.field}`` in a template) would fail at runtime.
    The canonical fix routes the raw string through an ``extract_json`` step whose
    ``output_key`` is a DISTINCT, parsed key, then field-accesses THAT key (which
    is not a tool output, so it is never flagged). Whole-value references
    (``{state.K}``, e.g. feeding the string into extract_json's ``input_keys``)
    are fine and not flagged.

    The check is deliberately flow-INSENSITIVE: it flags field access on ANY key a
    tool step writes, regardless of ordering. That makes it SOUND — no
    consume-before-parse false negative (``tool→K``, ``{state.K.f}``, then
    ``extract_json→K`` would slip past an "is K ever re-parsed?" test; codex r2).
    The only cost is disallowing the non-idiomatic IN-PLACE re-parse (a tool and
    an extract_json sharing one ``output_key``); the linter steers the author to a
    distinct parsed key, which a static check CAN verify. Proving an in-place parse
    dominates every consumer would need full DAG dataflow analysis — out of
    proportion for a boot gate, and a false negative is the worse failure here."""
    tainted: set[str] = set()
    for sid in step_ids:
        if step_map[sid].get("type") != "tool":
            continue
        out = step_map[sid].get("output_key")
        if isinstance(out, str) and out:
            tainted.add(out)
    if not tainted:
        return []

    errors: list[str] = []
    for sid in step_ids:
        step = step_map[sid]
        for field, val in step.items():
            if field in _NON_TEMPLATE_SCAN_FIELDS:
                continue
            for path, match in _iter_state_refs(val, str(field)):
                base, tail = match.group(1), match.group(2)
                if tail and base in tainted:
                    errors.append(
                        f"{who}: step {sid!r} field {path!r} field-accesses "
                        f"{match.group(0)} — {base!r} is a tool step's output_key "
                        f"(a JSON STRING, not a dict). Route it through an "
                        f"extract_json step and reference that step's parsed "
                        f"output_key instead (SYNAPSE-SPIKE Item 2 Finding #2)"
                    )
    return errors


def _validate_anton(data: dict, who: str, *, step_ids: set[str]) -> list[str]:
    """Check 4 (+ overlay shape) — the required ``anton`` block.

    ``max_sensitivity`` is required and must be a known tier. Every
    ``step_sensitivity`` entry must name a real step and a known tier, and must
    NOT exceed ``max_sensitivity`` (the per-step ≤ composite-max rule — a step
    above the composite tier would be routed too permissively). ``host_agents``
    (used by check 1) is shape-validated here. Unknown ``anton`` keys are typos
    and are rejected.

    ``step_ids`` empty (steps unparseable) → the undefined-step cross-checks are
    skipped (they'd be noise), but the tier/shape checks still run."""
    anton = data.get("anton")
    if anton is None:
        return [
            f"{who}: missing required top-level 'anton' block — a composite must "
            f"declare anton.max_sensitivity (the tier its whole DAG routes at)"
        ]
    if not isinstance(anton, dict):
        return [f"{who}: 'anton' must be an object, got {type(anton).__name__}"]

    errors: list[str] = []
    for key in anton:
        if key not in _ANTON_KEYS:
            errors.append(
                f"{who}: unknown anton key {key!r} (expected one of {_ANTON_KEYS})"
            )

    # ── max_sensitivity (required) ────────────────────────────────────────────
    max_sens = anton.get("max_sensitivity")
    max_rank: Optional[int] = None
    if max_sens is None:
        errors.append(f"{who}: anton.max_sensitivity is required")
    elif max_sens not in _SENSITIVITY_TIERS:
        errors.append(
            f"{who}: anton.max_sensitivity {max_sens!r} not in {_SENSITIVITY_TIERS}"
        )
    else:
        max_rank = _SENSITIVITY_TIERS.index(max_sens)

    # ── step_sensitivity (REQUIRED, complete) — the per-step ≤ max check ──────
    # FAIL-CLOSED (security review SEV-1): EVERY step must declare its own tier. An
    # omitted step must NOT silently inherit a (possibly lower) max_sensitivity — a
    # more-sensitive step left undeclared would then route too permissively. So
    # step_sensitivity is required and must cover every defined step.
    step_sens = anton.get("step_sensitivity")
    if step_sens is None:
        errors.append(
            f"{who}: anton.step_sensitivity is required — every step must declare "
            f"its own sensitivity tier (no silent inheritance of max_sensitivity)"
        )
    elif not isinstance(step_sens, dict):
        errors.append(
            f"{who}: anton.step_sensitivity must be an object mapping step_id -> "
            f"tier, got {type(step_sens).__name__}"
        )
    else:
        for s_id, tier in step_sens.items():
            if step_ids and s_id not in step_ids:
                errors.append(
                    f"{who}: anton.step_sensitivity names undefined step id {s_id!r}"
                )
            if tier not in _SENSITIVITY_TIERS:
                errors.append(
                    f"{who}: anton.step_sensitivity[{s_id!r}] tier {tier!r} not in "
                    f"{_SENSITIVITY_TIERS}"
                )
                continue
            if max_rank is not None and _SENSITIVITY_TIERS.index(tier) > max_rank:
                errors.append(
                    f"{who}: step {s_id!r} sensitivity {tier!r} exceeds composite "
                    f"anton.max_sensitivity {max_sens!r} — the composite routes the "
                    f"whole DAG at its max tier (composite tier = max of steps, vault "
                    f"CLAUDE.md §4), so a step above that max would be routed too "
                    f"permissively"
                )
        # Completeness: every DEFINED step must carry an explicit tier (fail-closed).
        for s_id in step_ids:
            if s_id not in step_sens:
                errors.append(
                    f"{who}: step {s_id!r} has no anton.step_sensitivity tier — every "
                    f"step must declare one explicitly (fail-closed; no inheritance)"
                )

    # ── host_agents (optional) — shape only (semantics in check 1) ────────────
    host_agents = anton.get("host_agents")
    if host_agents is not None and not isinstance(host_agents, dict):
        errors.append(
            f"{who}: anton.host_agents must be an object mapping agent_id -> "
            f"{{tools: [...]}}, got {type(host_agents).__name__}"
        )
    elif isinstance(host_agents, dict):
        for a_id, a_def in host_agents.items():
            if not isinstance(a_def, dict):
                errors.append(
                    f"{who}: anton.host_agents[{a_id!r}] must be an object with a "
                    f"'tools' list"
                )
                continue
            tools = a_def.get("tools")
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                errors.append(
                    f"{who}: anton.host_agents[{a_id!r}].tools must be a list of "
                    f"tool-name strings"
                )
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Composite-level validation
# ─────────────────────────────────────────────────────────────────────────────
def validate_composite(data: Any, who: str, *, key: Optional[str] = None) -> list[str]:
    """Return every validation error for one parsed composite (empty = valid).

    PURE — never mutates anything, never raises on bad input (a malformed shape
    yields error strings, not an exception). ``who`` prefixes every error so a
    boot failure names the offending composite; ``key`` (the filename stem) is
    cross-checked against the composite ``id`` when provided."""
    if not isinstance(data, dict):
        return [f"{who}: composite must be a JSON object, got {type(data).__name__}"]

    errors: list[str] = []

    # ── top-level id ↔ filename key ───────────────────────────────────────────
    cid = data.get("id")
    if not isinstance(cid, str) or not cid.strip():
        errors.append(f"{who}: missing/empty top-level 'id'")
    elif key is not None and cid != key:
        errors.append(
            f"{who}: 'id' {cid!r} must match the filename key {key!r} "
            f"(file <key>{_ORCH_SUFFIX})"
        )

    # ── steps must be a non-empty list ────────────────────────────────────────
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(f"{who}: 'steps' must be a non-empty list")
        # Can't run step-level checks; still validate the anton overlay shape.
        errors.extend(_validate_anton(data, who, step_ids=set()))
        return errors

    # ── build the step map + per-step shape ───────────────────────────────────
    step_map: dict[str, dict] = {}
    step_ids: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(
                f"{who}: step #{i} must be an object, got {type(step).__name__}"
            )
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{who}: step #{i} has a missing/empty 'id'")
            continue
        if sid in step_map:
            errors.append(f"{who}: duplicate step id {sid!r}")
            continue
        stype = step.get("type")
        if stype not in _STEP_TYPES:
            errors.append(
                f"{who}: step {sid!r} has unknown type {stype!r} "
                f"(expected one of {_STEP_TYPES})"
            )
        step_map[sid] = step
        step_ids.append(sid)

    id_set = set(step_ids)

    # ── entry_step_id resolves ────────────────────────────────────────────────
    entry = data.get("entry_step_id")
    if not isinstance(entry, str) or not entry.strip():
        errors.append(f"{who}: missing/empty 'entry_step_id'")
    elif entry not in id_set:
        errors.append(f"{who}: entry_step_id {entry!r} is not a defined step")

    # ── edges: malformed-shape report + resolution + the cycle graph ──────────
    # One pass per step: _collect_edges names any malformed edge target (never
    # silently dropped — codex r3), and the resolved targets are existence-checked
    # and fed into the acyclicity graph (successors only; loop bodies are internal).
    succ: dict[str, list[str]] = {}
    for sid in step_ids:
        successors, loop_body, edge_errors = _collect_edges(step_map[sid])
        for e in edge_errors:
            errors.append(f"{who}: step {sid!r} {e}")
        for ref in (*successors, *loop_body):
            if ref not in id_set:
                errors.append(
                    f"{who}: step {sid!r} references undefined step id {ref!r}"
                )
        succ[sid] = [r for r in successors if r in id_set]

    # ── Check 1 — host-agent pinning ──────────────────────────────────────────
    errors.extend(_check_host_agents(data, step_map, step_ids, who))

    # ── Check 2 — extract_json-after-tool ─────────────────────────────────────
    errors.extend(_check_extract_json_after_tool(step_map, step_ids, who))

    # ── Check 3 — DAG acyclicity (over resolvable edges only) ─────────────────
    cycle = _find_cycle(step_ids, succ)
    if cycle:
        errors.append(f"{who}: step graph has a cycle: {' -> '.join(cycle)}")

    # ── Check 4 (+ overlay shape) — per-step sensitivity ≤ composite max ──────
    errors.extend(_validate_anton(data, who, step_ids=id_set))

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# File + directory sweeps
# ─────────────────────────────────────────────────────────────────────────────
def validate_file(path: Path) -> list[str]:
    """Validate one ``<key>.orchestration.json`` file. A read error, a non-UTF-8
    file, or invalid JSON is a validation error (named), not an exception — a
    malformed file must refuse boot with a structured error, never crash it.
    ``utf-8-sig`` tolerates a Windows-authored BOM (which plain ``json.loads``
    would choke on)."""
    who = f"composite {path.name!r}"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return [f"{who}: unreadable: {e}"]
    # UnicodeDecodeError is a ValueError, NOT an OSError — catch it separately so a
    # non-UTF-8 composite is a named error, not a boot crash (codex SEV-2).
    except UnicodeError as e:
        return [f"{who}: not valid UTF-8: {e}"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [f"{who}: invalid JSON: {e}"]
    key = path.name[: -len(_ORCH_SUFFIX)] if path.name.endswith(_ORCH_SUFFIX) else None
    return validate_composite(data, who, key=key)


def _resolve_dir(explicit: Optional[Path]) -> tuple[Path, bool]:
    """Resolve the composites dir + whether it was explicitly CONFIGURED.

    Precedence: explicit arg > ``AGENTIC_COMPOSITES_DIR`` env > the repo-root
    ``composites/`` default. ``configured`` is ``True`` for the first two — used
    to fail closed when an override points nowhere (security review SEV-2)."""
    if explicit is not None:
        return explicit, True
    env = os.environ.get(COMPOSITES_DIR_ENV)
    if env:
        return Path(env), True
    return COMPOSITES_DIR, False


def iter_composite_files(composites_dir: Optional[Path] = None) -> list[Path]:
    """Sorted ``*.orchestration.json`` files in the composites dir. A missing dir
    yields ``[]`` (the pre-Phase-6 no-op case — boot validates clean). The
    configured-but-missing fail-closed check lives in :func:`validate_all`."""
    directory, _configured = _resolve_dir(composites_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(COMPOSITE_GLOB))


def validate_all(composites_dir: Optional[Path] = None) -> list[str]:
    """Return validation errors across every composite (empty = all valid).

    PURE — safe to point at an arbitrary ``composites_dir`` (synthetic-composite
    tests), mirroring ``routines.skills.registry.validate_all``.

    FAIL-CLOSED on a misconfigured override (security review SEV-2): if a
    composites dir was explicitly configured (``--dir`` / ``AGENTIC_COMPOSITES_DIR``)
    but does not exist or is not a directory, that is a hard error — NOT a silent
    no-op that would disable the boot validator. Only the DEFAULT repo
    ``composites/`` being absent is the legitimate pre-Phase-6 no-op."""
    directory, configured = _resolve_dir(composites_dir)
    if not directory.is_dir():
        if configured:
            return [
                f"composites dir {directory} is configured "
                f"(--dir / {COMPOSITES_DIR_ENV}) but does not exist or is not a "
                f"directory — refusing to validate into a silent no-op"
            ]
        return []  # default repo composites/ absent → pre-Phase-6 no-op
    errors: list[str] = []
    for path in sorted(directory.glob(COMPOSITE_GLOB)):
        errors.extend(validate_file(path))
    return errors


def validate_or_raise(composites_dir: Optional[Path] = None) -> None:
    """Raise ``RuntimeError`` if any composite is malformed. Called at bridge
    startup so a bad orchestration JSON refuses to BOOT rather than failing on
    the first ``/pitch`` run (the #61 fail-fast contract)."""
    errors = validate_all(composites_dir)
    if errors:
        raise RuntimeError(
            "Composite template validation failed at startup:\n  - "
            + "\n  - ".join(errors)
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ``lint-composites`` / ``python -m routines.composite.lint``
# ─────────────────────────────────────────────────────────────────────────────
@click.command()
@click.option(
    "--dir",
    "dir_",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Composites directory to lint (default: repo-root composites/ or "
    "$AGENTIC_COMPOSITES_DIR).",
)
def main(dir_: Optional[Path]) -> None:
    """Lint ANTON composite orchestration JSONs (#steal-kocoro P2).

    Exits 0 when every composite is valid (or none exist), 1 on any error — the
    same checks the bridge runs at boot."""
    composites_dir, _configured = _resolve_dir(dir_)
    # Validate FIRST — validate_all surfaces the configured-but-missing-dir error,
    # which must not be masked by the "no composites found" short-circuit.
    errors = validate_all(dir_)
    if errors:
        click.echo(
            f"lint-composites: {len(errors)} error(s) in {composites_dir}:",
            err=True,
        )
        for err in errors:
            click.echo(f"  - {err}", err=True)
        raise SystemExit(1)
    files = iter_composite_files(dir_)
    if not files:
        click.echo(f"lint-composites: no composites found in {composites_dir}")
        return
    click.echo(f"lint-composites: {len(files)} composite(s) OK ({composites_dir})")


if __name__ == "__main__":  # pragma: no cover — module-run entry
    main()
