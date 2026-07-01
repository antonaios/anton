"""#24 — guardrail registry + retry-with-feedback runtime (CrewAI §2.5 pattern).

The schema half of #24 shipped with #21/#61: every SKILL.md declares
``metadata.guardrails`` (named output contracts) + ``metadata.guardrail_max_retries``
and the registry parses them onto :class:`~routines.skills.registry.SkillMetadata`.
This module is the RUNTIME half:

  1. **A registry of named guardrail functions.** Every name declared by a
     shipped SKILL.md resolves here — either to a real output CHECKER (a
     callable run against the skill's output) or to a documented DECLARATIVE
     entry (an invariant enforced at source — an operator gate, an engine
     validation, a construction property — that is recognised but not
     re-checked at the output boundary). An UNKNOWN declared name is a
     STARTUP validation error (``routines.skills.registry._validate_frontmatter``
     calls :func:`validate_guardrail_names`), never a silent no-op.

  2. **The retry-with-feedback loop** — :func:`llm_with_guardrails` wraps the
     governed ``llm()`` gateway: on a guardrail fail the failure message is
     appended to the LLM context and the call retried, bounded by
     ``min(guardrail_max_retries, sensitivity-tier budget)``. MNPI gets a
     budget of ZERO — the first failure degrades immediately, no extra LLM
     call is ever made on the MNPI lane. Every retry and the final verdict
     land as structured audit rows (``@safe_audit`` — telemetry never crashes
     the skill).

  3. **Output-boundary validation** — :func:`record_output_guardrails` is
     called by the ``@anton_skill`` wrapper after the body returns: the
     skill's structured result is evaluated against its declared guardrails,
     the verdicts are stamped on the tool-side audit context and written as a
     structured audit row. This phase is ADVISORY (verdict + audit, never a
     block): the body has already run — possibly with side effects (vault
     writes, Excel appends) — so a re-run here would double-fire them; the
     retry teeth live around the LLM step (#2 above), where a retry is just
     one more governed call.

Degrade-don't-burn: when retries exhaust, :class:`GuardrailRetriesExhausted`
is raised — a structured-fields ``RuntimeError`` in the same family as #67's
``LLMCallsCapExceeded`` / #74.5's ``ToolCallsCapExceeded`` (the route layer /
app-level handler renders an honest refusal naming WHICH guardrail failed and
WHY; the exception carries ``last_result`` so a route that wants to degrade to
a manual path — lbo-intake-agent style — can do so explicitly, eyes open).
A fabricated pass is never produced.

Sensitivity-tier retry budgets (the #24 spec's MNPI=0 / public=3 bounds, with
a monotonic mapping for the intermediate tiers — data, not scattered ifs)::

    public        → 3 retries
    internal      → 2 retries
    confidential  → 1 retry
    MNPI          → 0 retries   (no extra LLM calls, ever)
    <unknown>     → 0 retries   (fail-closed)

Each retry is one more call through the FULL governed ``llm()`` path, so the
#no-mnpi-to-cloud sensitivity lane (was cited as §5.4), the #57 budget gate
and the #67 per-skill llm-call cap
all still bound the loop independently (defence in depth — a generous tier
budget can never out-spend a declared ``cost_ceiling_llm_calls``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from routines.shared import audit
from routines.skills._runtime import llm_gateway

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Sensitivity-tier retry budgets (#24 — data, not scattered ifs)
# ─────────────────────────────────────────────────────────────────────────────

# Keyed lowercase; lookups normalise. Monotonic: stricter tier → smaller
# budget. The spec pins the bounds (MNPI=0, public=3); internal/confidential
# interpolate monotonically between them. Unknown tier → 0 (fail-closed: an
# unrecognised tier must never be granted extra LLM calls).
TIER_RETRY_BUDGETS: dict[str, int] = {
    "public": 3,
    "internal": 2,
    "confidential": 1,
    "mnpi": 0,
}


def tier_retry_budget(sensitivity: Any) -> int:
    """Retry budget for a sensitivity tier. Case-insensitive and whitespace-
    tolerant (codex SEV-3: ``"MNPI "`` must normalise, not silently become an
    unknown tier); unknown/None tiers fail closed to 0 (treated like MNPI —
    no extra LLM calls)."""
    return TIER_RETRY_BUDGETS.get(str(sensitivity).strip().lower(), 0)


def effective_retry_budget(declared_max_retries: Any, sensitivity: Any) -> int:
    """``min(guardrail_max_retries, tier budget)``, floored at 0.

    The skill's declared ``guardrail_max_retries`` is an UPPER BOUND it opts
    into; the tier budget is the platform ceiling. Neither can raise the
    other."""
    try:
        declared = int(declared_max_retries)
    except (TypeError, ValueError):
        declared = 0
    return max(0, min(declared, tier_retry_budget(sensitivity)))


# ─────────────────────────────────────────────────────────────────────────────
# Verdicts + the named-guardrail registry
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardrailVerdict:
    """The outcome of evaluating ONE named guardrail against an output.

    ``checked=False`` marks a guardrail that was recognised but not executed
    at this boundary — a declarative invariant enforced at source, or a
    checker that does not apply to this output kind (a dict-shape checker
    against raw LLM text). An unchecked verdict is NEVER a fabricated pass:
    ``message`` says exactly why it wasn't run, and the audit row records the
    distinction."""

    name: str
    ok: bool
    checked: bool = True
    message: str = ""


# A checker takes the output (str for the LLM-text boundary, dict for the
# structured-result boundary) and returns (ok, message). ``message`` on a
# fail is the feedback appended to the LLM context on retry — write it for
# the model: name what is missing and what a passing output looks like.
GuardrailChecker = Callable[[Any], "tuple[bool, str]"]


@dataclass(frozen=True)
class _CheckerEntry:
    fn: GuardrailChecker
    modes: frozenset  # subset of {"text", "dict"}


_CHECKERS: dict[str, _CheckerEntry] = {}


def register_guardrail(name: str, *, modes: Iterable[str] = ("dict",)):
    """Register a named guardrail checker (decorator).

    ``modes`` declares which output kinds the checker understands:
    ``"text"`` (raw LLM output — the retry-loop boundary) and/or ``"dict"``
    (the skill's structured result — the output boundary)."""

    mode_set = frozenset(modes)
    if not mode_set <= {"text", "dict"}:
        raise ValueError(f"guardrail {name!r}: modes must be within {{'text','dict'}}")

    def decorate(fn: GuardrailChecker) -> GuardrailChecker:
        if name in _CHECKERS or name in DECLARATIVE_GUARDRAILS:
            raise ValueError(f"guardrail {name!r} registered twice")
        _CHECKERS[name] = _CheckerEntry(fn=fn, modes=mode_set)
        return fn

    return decorate


# Declarative guardrails: named invariants DECLARED in SKILL.md frontmatter
# whose enforcement lives at source — an operator approval gate, an engine
# validation, a parse contract, or a construction property of the response
# model. They are registered here so (a) the name is KNOWN at startup (an
# unknown name still hard-fails the boot validation) and (b) the output-
# boundary audit row honestly records "enforced at source: <where>" instead
# of a fabricated pass. name → where the enforcement lives.
DECLARATIVE_GUARDRAILS: dict[str, str] = {
    # comps — operator approval gates (suspend loop) + capture discipline
    "operator_approves_subsectors": "comps Stage-0 operator gate (suspend/resume loop)",
    "operator_approves_peers_and_deals": "comps Stage-1 operator gate (suspend/resume loop)",
    "lfy1_approved_unless_connector": "comps Stage-2 operator gate (suspend/resume loop)",
    "section_append_not_overwrite": "#76 capture loop — append-only proposal writer (capture.py)",
    # deal-tracker — extractor + routine contracts
    "target_company_extracted": "deal-tracker routine — surfaces the 'no target extracted' warning in result.warnings",
    "no_computed_multiples": "deal-tracker extractor SYSTEM_PROMPT + parse contract (never infers a multiple)",
    # equity-research — note-write path contracts
    "analyst_commentary_preserved": "equity-research append-only dated-section write path",
    "no_fabricated_commentary": "equity-research routine — commentary bullets stay empty by construction",
    # morning-brief — pipeline-internal ordering/branches
    "context_gathered_before_synthesis": "morning-brief pipeline — synthesis only runs after gather_context succeeds",
    "ollama_state_surfaced": "morning-brief response model — ollama reachability is a required response field",
    "data_frontmatter_complete": "morning-brief file writer — frontmatter data: carries brief.model_dump()",
    "no_synthesis_without_inputs": "morning-brief pipeline — empty inputs branch writes the explicit empty-brief artefact",
    # lbo — engine-side numeric validation
    "engine_validation_passes": "LBO engine validation (invalid inputs re-suspend, never silently pass)",
    "sources_and_uses_ties": "LBO engine sources-and-uses tie check",
    # lbo-intake-agent — transcribe-only parse contract
    "transcribe_only_no_llm_maths": "lbo-intake-agent parse_judgment — unsourced/computed values are demoted to open questions",
    # lessons-suggest / recall-query — construction properties + echo contracts
    "matched_context_echoed": "lessons-suggest response model — matched_context is a required echo field",
    "no_synthesis": "response-model construction — no narrative/summary field exists to fabricate",
    "filter_applied": "recall-query route — filter_applied echo contract (pydantic-required)",
    # ticker-multiples — provider-null + firewall contracts
    "never_invent_a_figure": "ticker-multiples provider chain — a missing figure stays null at source",
    "firewalled_from_valuation": "workspace-write policy + skill construction — never stamps valuation folders",
    # sector-news — routine-internal dedupe
    "dedupe_applied": "sector-news routine — dedupe pass runs inside the newsletter pipeline",
}


def known_guardrail_names() -> frozenset[str]:
    """Every guardrail name the runtime recognises (checkers + declarative)."""
    return frozenset(_CHECKERS) | frozenset(DECLARATIVE_GUARDRAILS)


def validate_guardrail_names(names: Iterable[Any], who: str) -> list[str]:
    """Startup-validation helper: one error per UNKNOWN declared guardrail name.

    Called from ``routines.skills.registry._validate_frontmatter`` so a typo'd
    or unimplemented guardrail name refuses to BOOT (#61 fail-fast contract)
    instead of becoming a silent no-op at runtime."""
    known = known_guardrail_names()
    errors: list[str] = []
    for n in names:
        if not isinstance(n, str) or not n.strip():
            errors.append(f"{who}: guardrails entry {n!r} is not a non-empty string")
        elif n not in known:
            errors.append(
                f"{who}: unknown guardrail {n!r} — not registered in "
                f"routines.skills._runtime.guardrails (known: add a checker via "
                f"register_guardrail or a DECLARATIVE_GUARDRAILS entry)"
            )
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Checker implementations (REAL — grounded in the shipped result shapes)
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+", re.IGNORECASE)

# Keys that count as a citation/source on a unit (mapping) for the
# cite-presence family. Matched case-insensitively against mapping keys.
_CITE_KEYS = (
    "source", "sources", "source_url", "citation", "citations", "cited",
    "cited_from", "provider", "provenance", "location", "quote", "url",
)


def _as_text(output: Any) -> str:
    """Searchable text form of an output (str passes through; anything else is
    JSON-serialised best-effort)."""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, default=str)
    except Exception:  # noqa: BLE001 — a search surface, not load-bearing
        return str(output)


def _has_cite(mapping: dict) -> bool:
    """True when a mapping carries at least one non-empty cite-ish key."""
    for k, v in mapping.items():
        if str(k).lower() in _CITE_KEYS and v not in (None, "", [], {}):
            return True
    return False


def _container_units(container: Any) -> "tuple[list[dict], int]":
    """``(unit mappings, malformed-entry count)`` for one container value
    (codex fix-round SEV-1: a PRESENT but malformed container —
    ``{"citations": ["CIM p.12"]}``, ``{"assumptions": "CIM p.12"}`` — must be
    distinguishable from a genuinely empty valid one, never a vacuous pass).

    list → its elements are the units; non-dict elements are MALFORMED.
    dict → nested mapping values are the units (prefill ``{box: {...}}``
           shape) and they are ALWAYS checked individually — a container-
           level cite-ish key never collapses a mapping-of-units into one
           unit (codex round-3 SEV-1: that masked uncited nested rows), it
           is treated as container metadata and ignored. Only a LEAF mapping
           (no nested mapping values) that itself carries a cite-ish key is
           the single unit; a leaf mapping without one is wholly malformed.
    anything else (scalar/string container) → no units, wholly malformed."""
    if isinstance(container, list):
        units = [u for u in container if isinstance(u, dict)]
        return units, len(container) - len(units)
    if isinstance(container, dict):
        units = [v for v in container.values() if isinstance(v, dict)]
        if units:
            # Non-mapping values are malformed UNLESS they are container-level
            # cite-ish metadata (e.g. {"source": "screen export", box: {...}}).
            malformed = sum(
                1 for k, v in container.items()
                if not isinstance(v, dict) and str(k).lower() not in _CITE_KEYS
            )
            return units, malformed
        if _has_cite(container):
            return [container], 0
        return [], len(container)
    return [], 1


def _find_containers(output: Any, keys: tuple[str, ...]) -> "list[tuple[str, Any]]":
    """All ``(key, value)`` pairs anywhere in the dict tree whose key is in
    ``keys`` — the unit containers a cite-presence checker walks."""
    found: list[tuple[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in keys:
                    found.append((str(k), v))
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(output)
    return found


def _cited_units_check(
    output: Any, unit_keys: tuple[str, ...], what: str,
    *, container_required: bool = True,
) -> "tuple[bool, str]":
    """Shared engine for the cite-presence family: every unit mapping under a
    recognised container key must carry a non-empty cite-ish field.

    No recognised container in the output → a real FAIL by default (codex
    SEV-1: a renamed/malformed container — ``assumptionz`` — must not pass a
    checker guardrail). ``container_required=False`` is the per-checker
    escape hatch ONLY for shapes where the declaring skill's schema makes the
    container genuinely optional (stage-dependent results); that branch passes
    WITH a disclosure message recorded verbatim in the audit row, never a
    silent fabricated pass."""
    containers = _find_containers(output, unit_keys) if isinstance(output, (dict, list)) else []
    if not containers:
        if container_required:
            return False, (
                f"no {'/'.join(unit_keys)} container found in the output — the "
                f"result must include one of these containers with one entry per "
                f"{what.rstrip('s')}, each naming its source (a renamed or "
                f"malformed container does not pass)"
            )
        return True, f"no {'/'.join(unit_keys)} container found in output — nothing to check"
    uncited: list[str] = []
    total = 0
    malformed = 0
    for key, container in containers:
        units, bad = _container_units(container)
        malformed += bad
        for unit in units:
            total += 1
            if not _has_cite(unit):
                label = str(
                    unit.get("name") or unit.get("title") or unit.get("key")
                    or unit.get("box") or f"{key}[{total - 1}]"
                )
                uncited.append(label)
    if malformed:
        return False, (
            f"{malformed} malformed entr{'y' if malformed == 1 else 'ies'} in the "
            f"{'/'.join(unit_keys)} container(s) — every {what.rstrip('s')} must be "
            f"a mapping carrying a source/citation field (a bare string or scalar "
            f"does not pass)"
        )
    if uncited:
        shown = ", ".join(uncited[:5]) + ("…" if len(uncited) > 5 else "")
        return False, (
            f"{len(uncited)} of {total} {what} carry no source/citation field "
            f"({shown}) — every {what.rstrip('s')} must name where it came from "
            f"(one of: {', '.join(_CITE_KEYS[:6])}, …)"
        )
    return True, f"all {total} {what} carry a source/citation field"


def _status_ok_check(output: Any, what: str) -> "tuple[bool, str]":
    """Shared engine for the status-contract family: ``status == 'ok'`` and an
    empty ``error`` field on the structured result."""
    if not isinstance(output, dict):
        return False, f"{what}: output is not a structured result (no status field)"
    status = output.get("status")
    err = output.get("error")
    if status != "ok":
        return False, f"{what}: status={status!r} (expected 'ok'){f' — error: {err}' if err else ''}"
    if err:
        return False, f"{what}: status is 'ok' but error is set: {err}"
    return True, f"{what}: status ok"


# ── cite-presence family (the #24 deliverable's "cite-presence enforced") ───


# A claim unit in TEXT output: a bulleted (-, *, •) or numbered (1. / 1))
# line. A block runs from its marker to the next marker (or end of text) so
# a URL on a bullet's continuation line still counts for that bullet.
_CLAIM_BLOCK_RE = re.compile(r"^[ \t]*(?:[-*•]|\d{1,3}[.)])[ \t]+", re.MULTILINE)

# Container keys whose unit mappings count as claims in DICT output.
_CLAIM_KEYS = ("items", "stories", "articles", "headlines", "claims", "bullets")


def _claim_blocks(text: str) -> list[str]:
    """Split text into per-claim blocks at bullet/numbered-list markers.
    No markers → empty list (the caller degrades to a presence check)."""
    starts = [m.start() for m in _CLAIM_BLOCK_RE.finditer(text)]
    if not starts:
        return []
    bounds = starts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]] for i in range(len(starts))]


@register_guardrail("sources_cited", modes=("text", "dict"))
def _sources_cited(output: Any) -> "tuple[bool, str]":
    """sector-news Iron Law: EVERY claim links to a source URL — per-claim,
    not presence-only (codex SEV-1: one cited claim must not wave through
    many uncited ones).

    text → every bullet/numbered claim block must carry an http(s) URL.
    dict → every unit mapping under a claim-ish container (items/stories/…)
    must serialise with at least one URL.
    No claim structure recognised (unstructured prose, no known container) →
    degrade to the presence check with the weaker scope DISCLOSED in the
    message (recorded verbatim in the audit row)."""
    if isinstance(output, str):
        blocks = _claim_blocks(output)
        if blocks:
            uncited = [i + 1 for i, b in enumerate(blocks) if not _URL_RE.search(b)]
            if uncited:
                shown = ", ".join(f"claim {i}" for i in uncited[:5]) + ("…" if len(uncited) > 5 else "")
                return False, (
                    f"{len(uncited)} of {len(blocks)} claim bullet(s) carry no "
                    f"source URL ({shown}) — EVERY claim must link the http(s) "
                    f"source it came from, next to the claim"
                )
            return True, f"all {len(blocks)} claim bullet(s) carry a source URL"
    else:
        containers = _find_containers(output, _CLAIM_KEYS) if isinstance(output, (dict, list)) else []
        # Every element/value of a claim container is a unit — dicts AND bare
        # strings both count (a string claim carrying its URL inline is a
        # legitimate shape here, unlike the structured cite-key family).
        units: list[Any] = []
        for _, c in containers:
            if isinstance(c, list):
                units.extend(c)
            elif isinstance(c, dict):
                units.extend(c.values())
            else:
                units.append(c)
        if units:
            uncited_n = sum(1 for u in units if not _URL_RE.search(_as_text(u)))
            if uncited_n:
                return False, (
                    f"{uncited_n} of {len(units)} claim item(s) carry no source "
                    f"URL — every item must carry the http(s) link it came from"
                )
            return True, f"all {len(units)} claim item(s) carry a source URL"
    # No per-claim structure recognised — presence check, weaker scope disclosed.
    if _URL_RE.search(_as_text(output)):
        return True, (
            "no per-claim structure recognised — degraded to a presence check: "
            "at least one source URL present"
        )
    return False, (
        "no source URL (http/https link) found in the output — every claim "
        "must cite the source it came from; attach the URL next to each claim"
    )


@register_guardrail("every_assumption_cited")
def _every_assumption_cited(output: Any) -> "tuple[bool, str]":
    """lbo: the shipped ``LBOOutput`` binds assumptions to sources via its
    ``citations`` list (always present — pydantic field), so the container is
    REQUIRED: a result that loses/renames it fails for real."""
    return _cited_units_check(output, ("assumptions", "citations"), "assumptions")


@register_guardrail("every_prefilled_box_cited")
def _every_prefilled_box_cited(output: Any) -> "tuple[bool, str]":
    """lbo-intake-agent: ``prefill``/``boxes`` live on the judgment/suspension
    surface; the COMPLETED resume delegates to the LBO engine output, which
    carries neither — the container is genuinely stage-dependent, so missing
    passes with disclosure (the codex-sanctioned optional branch)."""
    return _cited_units_check(
        output, ("prefill", "boxes"), "prefilled boxes", container_required=False,
    )


# The only comps stage value that legitimately carries no figure containers
# (StageResult.stage is Literal["approval_pending", "complete"]). Matched
# normalised-exact; anything else requires containers.
_FIGURELESS_COMPS_STAGES = frozenset({"approval_pending"})


@register_guardrail("every_figure_sourced")
def _every_figure_sourced(output: Any) -> "tuple[bool, str]":
    """ticker-multiples (``snapshots[].rows``) + comps (``blocks[].coco_rows``/
    ``cotrans_rows``). Optionality is CONDITIONAL (codex fix-round SEV-1 —
    unconditional optionality let a COMPLETE Stage-3 missing its row
    containers pass): only an output that declares itself an INTERMEDIATE
    comps stage (``stage`` present and != "complete") may lack figure
    containers — those stages carry none by design. Everything else (a
    complete Stage 3, a ticker-multiples result, an unknown shape) REQUIRES
    a figure container."""
    stage = output.get("stage") if isinstance(output, dict) else None
    # Normalised ALLOWLIST, not a != check (codex round-3 SEV-1: any junk /
    # case-variant / non-string stage value must REQUIRE containers, or an
    # attacker-shaped output dodges the check by self-declaring a stage).
    # "approval_pending" is the only figureless value of comps'
    # StageResult.stage Literal["approval_pending", "complete"].
    intermediate = (
        isinstance(stage, str)
        and stage.strip().lower() in _FIGURELESS_COMPS_STAGES
    )
    return _cited_units_check(
        output,
        ("figures", "multiples", "metrics", "comps", "rows",
         "coco_rows", "cotrans_rows"),
        "figures",
        container_required=not intermediate,
    )


@register_guardrail("every_block_has_provider")
def _every_block_has_provider(output: Any) -> "tuple[bool, str]":
    """equity-research Iron Law clause 1: each top-level result block
    (snapshot / fundamentals / comps / news) carries a provider tag —
    block-level or on every nested entry."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    blocks = [k for k in ("snapshot", "fundamentals", "comps", "news") if output.get(k)]
    if not blocks:
        return True, "no snapshot/fundamentals/comps/news block present — nothing to check"
    missing = []
    for k in blocks:
        if "provider" not in _as_text(output[k]):
            missing.append(k)
    if missing:
        return False, (
            f"result block(s) {', '.join(missing)} carry no provider tag — "
            f"every top-level block must name the data provider it came from"
        )
    return True, f"all {len(blocks)} present block(s) carry a provider tag"


# ── status-contract family ───────────────────────────────────────────────────


@register_guardrail("report_written")
def _report_written(output: Any) -> "tuple[bool, str]":
    """vault-health: status='ok' (+ output_path set whenever the sweep wrote a
    report — the route only sets status='ok' on a completed sweep)."""
    return _status_ok_check(output, "report_written")


@register_guardrail("sweep_completed")
def _sweep_completed(output: Any) -> "tuple[bool, str]":
    return _status_ok_check(output, "sweep_completed")


@register_guardrail("scan_completed")
def _scan_completed(output: Any) -> "tuple[bool, str]":
    return _status_ok_check(output, "scan_completed")


# ── shape-contract checkers (grounded in the shipped response models) ────────


@register_guardrail("thresholds_documented")
def _thresholds_documented(output: Any) -> "tuple[bool, str]":
    """bd-decay: the active DECAY_THRESHOLDS are surfaced on the result
    (``active_thresholds``; actions-decay's cousin field is
    ``thresholds_applied``)."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    block = output.get("active_thresholds") or output.get("thresholds_applied")
    if isinstance(block, dict) and block:
        return True, f"thresholds surfaced ({len(block)} entries)"
    return False, (
        "active_thresholds/thresholds_applied is missing or empty — the "
        "response must surface the thresholds the sweep actually applied"
    )


@register_guardrail("roots_surfaced")
def _roots_surfaced(output: Any) -> "tuple[bool, str]":
    """actions-decay: ``roots_resolved`` enumerates every path the walker
    visited (always at least the vault Projects root)."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    roots = output.get("roots_resolved")
    if isinstance(roots, list) and roots:
        return True, f"{len(roots)} root(s) surfaced"
    return False, (
        "roots_resolved is missing or empty — the response must enumerate "
        "every root the walker visited"
    )


@register_guardrail("provenance_complete")
def _provenance_complete(output: Any) -> "tuple[bool, str]":
    """actions-decay: every surfaced overdue/stale action carries
    project + source_file + source_line + task_hash."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    bad = 0
    total = 0
    for bucket in ("overdue", "stale"):
        for entry in output.get(bucket) or []:
            if not isinstance(entry, dict):
                continue
            total += 1
            if not (
                entry.get("project")
                and entry.get("source_file")
                and isinstance(entry.get("source_line"), int)
                and entry.get("task_hash")
            ):
                bad += 1
    if bad:
        return False, (
            f"{bad} of {total} surfaced action(s) are missing provenance fields "
            f"(project/source_file/source_line/task_hash)"
        )
    return True, f"provenance complete on all {total} surfaced action(s)"


@register_guardrail("sticky_states_excluded")
def _sticky_states_excluded(output: Any) -> "tuple[bool, str]":
    """actions-decay: 'done' rows never surface in the overdue/stale buckets."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    leaked = sum(
        1
        for bucket in ("overdue", "stale")
        for entry in (output.get(bucket) or [])
        if isinstance(entry, dict) and str(entry.get("status", "")).lower() == "done"
    )
    if leaked:
        return False, f"{leaked} 'done' row(s) leaked into the overdue/stale buckets"
    return True, "no 'done' rows in the overdue/stale buckets"


@register_guardrail("dedupe_checked")
def _dedupe_checked(output: Any) -> "tuple[bool, str]":
    """deal-tracker: the routine's dedupe ran — status ∈ {appended,
    skipped_duplicate}. A dry_run passes with a disclosure (dedupe is
    deliberately not exercised on a dry run)."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    status = output.get("status")
    if status in ("appended", "skipped_duplicate"):
        return True, f"dedupe ran (status={status})"
    if status == "dry_run":
        return True, "dry_run — append (and therefore dedupe) deliberately not exercised"
    return False, (
        f"status={status!r} does not confirm the dedupe ran "
        f"(expected appended/skipped_duplicate)"
    )


@register_guardrail("score_greater_than_zero")
def _score_greater_than_zero(output: Any) -> "tuple[bool, str]":
    """lessons-suggest Iron Law: no zero-score suggestion in the surfaced list."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    bad = [
        s for s in (output.get("suggestions") or [])
        if isinstance(s, dict) and not (
            isinstance(s.get("score"), (int, float)) and s["score"] > 0
        )
    ]
    if bad:
        return False, f"{len(bad)} surfaced suggestion(s) have score <= 0 (or no score)"
    return True, "every surfaced suggestion has score > 0"


@register_guardrail("reason_populated")
def _reason_populated(output: Any) -> "tuple[bool, str]":
    """lessons-suggest: every Suggestion carries a human-readable reason."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    bad = [
        s for s in (output.get("suggestions") or [])
        if isinstance(s, dict) and not str(s.get("reason") or "").strip()
    ]
    if bad:
        return False, f"{len(bad)} suggestion(s) carry no reason — the operator cannot sanity-check rank"
    return True, "every suggestion carries a reason"


_HIT_SCORE_FIELDS = ("vector_score", "fts_score", "importance", "expires_decay", "final_score")


@register_guardrail("every_hit_scored")
def _every_hit_scored(output: Any) -> "tuple[bool, str]":
    """recall-query Iron Law clause 1: every hit carries the FULL score
    decomposition (vector/fts/importance/expires_decay/final), not just a
    final score."""
    if not isinstance(output, dict):
        return False, "output is not a structured result"
    bad = 0
    hits = output.get("hits") or []
    for h in hits:
        if isinstance(h, dict) and any(h.get(f) is None for f in _HIT_SCORE_FIELDS):
            bad += 1
    if bad:
        return False, (
            f"{bad} of {len(hits)} hit(s) are missing score-decomposition fields "
            f"({'/'.join(_HIT_SCORE_FIELDS)})"
        )
    return True, f"all {len(hits)} hit(s) carry the full score decomposition"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_guardrails(
    names: Sequence[str], output: Any, *, mode: str,
) -> list[GuardrailVerdict]:
    """Evaluate the named guardrails against ``output``.

    ``mode`` is the output kind at this boundary: ``"text"`` (raw LLM output,
    the retry loop) or ``"dict"`` (the skill's structured result, the output
    boundary). For each name:

      * a registered checker applicable to ``mode`` runs — a checker that
        RAISES is fail-closed (``ok=False`` with the error named, matching the
        house rule that a broken gate blocks rather than waves through);
      * a checker not applicable to ``mode`` → ``checked=False`` (honest skip);
      * a declarative name → ``checked=False`` with its enforced-at-source
        pointer;
      * an unknown name (possible only via an explicit caller list — declared
        frontmatter names are boot-validated) → fail-closed ``ok=False``.
    """
    verdicts: list[GuardrailVerdict] = []
    for name in names:
        entry = _CHECKERS.get(name)
        if entry is not None:
            if mode not in entry.modes:
                verdicts.append(GuardrailVerdict(
                    name=name, ok=True, checked=False,
                    message=f"not applicable to {mode} output",
                ))
                continue
            try:
                ok, message = entry.fn(output)
            except Exception as e:  # noqa: BLE001 — a broken checker fails closed
                ok, message = False, f"guardrail checker errored (fail-closed): {type(e).__name__}: {e}"
            verdicts.append(GuardrailVerdict(name=name, ok=bool(ok), message=str(message)))
        elif name in DECLARATIVE_GUARDRAILS:
            verdicts.append(GuardrailVerdict(
                name=name, ok=True, checked=False,
                message=f"declarative — enforced at source: {DECLARATIVE_GUARDRAILS[name]}",
            ))
        else:
            verdicts.append(GuardrailVerdict(
                name=name, ok=False,
                message="unknown guardrail name (not registered) — fail-closed",
            ))
    return verdicts


def _failures(verdicts: Iterable[GuardrailVerdict]) -> list[GuardrailVerdict]:
    return [v for v in verdicts if v.checked and not v.ok]


# ─────────────────────────────────────────────────────────────────────────────
# Audit (best-effort — telemetry never crashes the skill)
# ─────────────────────────────────────────────────────────────────────────────


def _audit_dir() -> Path:
    """Audit JSONL lives at ``<routines-repo>/runs/`` (mirror of
    ``central_guards._audit_dir`` — this module sits one package deeper)."""
    return Path(__file__).resolve().parents[3] / "runs"


def _verdict_payload(verdicts: Sequence[GuardrailVerdict]) -> list[dict]:
    return [
        {"name": v.name, "ok": v.ok, "checked": v.checked, "message": v.message[:300]}
        for v in verdicts
    ]


@audit.safe_audit
def _write_guardrail_audit(
    *,
    skill: str,
    run_id: Optional[str],
    action: str,           # "guardrail_retry" | "guardrail_verdict"
    status: str,           # "retry" | "ok" | "fail"
    phase: str,            # "llm" | "output"
    sensitivity: str,
    attempt: int,
    budget: int,
    verdicts: Sequence[GuardrailVerdict],
) -> None:
    """One structured audit row per retry and per final verdict (#24).

    ``@safe_audit`` — a failed write logs a warning + a failure record and
    returns None; the guardrail loop itself is never broken by telemetry."""
    audit.write_structured(
        actor={"type": "agent", "id": skill or "unknown"},
        entity_type="skill_run",
        entity_id=skill or "unknown",
        action=action,
        run_id=run_id or "",
        routine=f"skill.{skill or 'unknown'}",
        audit_dir=_audit_dir(),
        status=status,
        inputs={
            "phase": phase,
            "sensitivity": sensitivity,
            "attempt": attempt,
            "retry_budget": budget,
        },
        outputs={"verdicts": _verdict_payload(verdicts)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Degrade shape
# ─────────────────────────────────────────────────────────────────────────────


class GuardrailRetriesExhausted(RuntimeError):
    """Raised when a skill's LLM output still fails its declared guardrails
    after the retry budget is spent.

    Carries structured fields so the route layer / app handler can surface an
    HONEST refusal — which guardrail failed, what the failure message was, how
    many attempts ran against what budget — and so the audit row records all
    of it. Sits in the same exception family as #67's ``LLMCallsCapExceeded``
    and #74.5's ``ToolCallsCapExceeded``; the app-level handler maps it to a
    502 (the model could not produce a compliant output — an upstream-output
    failure, not a caller error). ``last_result`` carries the final
    NON-COMPLIANT :class:`~routines.skills._runtime.llm_gateway.LLMResult` so
    a route that degrades to a manual path (lbo-intake-agent style) can do so
    explicitly — never as a silent pass."""

    def __init__(
        self,
        *,
        skill_name: str,
        run_id: Optional[str],
        sensitivity: str,
        attempts: int,
        budget: int,
        failures: Sequence[GuardrailVerdict],
        last_result: Any = None,
    ) -> None:
        self.skill_name = skill_name
        self.run_id = run_id
        self.sensitivity = sensitivity
        self.attempts = attempts
        self.budget = budget
        self.failures = tuple(failures)
        self.last_result = last_result
        failed_names = ", ".join(v.name for v in self.failures) or "(none recorded)"
        reason = (
            f"skill {skill_name!r} output failed guardrail(s) [{failed_names}] "
            f"after {attempts} attempt(s) (retry budget {budget}, "
            f"sensitivity={sensitivity}; run_id={run_id})"
        )
        self.reason = reason
        super().__init__(reason)


# ─────────────────────────────────────────────────────────────────────────────
# The retry-with-feedback loop (#24 — the dispatcher loop)
# ─────────────────────────────────────────────────────────────────────────────


# Tiers whose retry prompt may carry an excerpt of the prior (failing)
# response. Codex SEV-2: re-sending prior model output is never NEEDED to
# identify the failure (the guardrail messages name it), so confidential
# retries drop the excerpt — defence-in-depth even though the retry rides the
# same governed lane. MNPI never reaches a retry at all (budget 0).
_PRIOR_EXCERPT_TIERS = frozenset({"public", "internal"})


def _feedback_prompt(
    base_prompt: str, failures: Sequence[GuardrailVerdict], prior_text: str, attempt: int,
    *, sensitivity: Any = None,
) -> str:
    """The retried prompt: the ORIGINAL prompt + the latest failure feedback +
    (public/internal lanes only) a bounded excerpt of the failing response.
    Rebuilt from the base each retry (latest feedback only) so the context
    doesn't compound feedback-on-feedback across attempts."""
    lines = [
        base_prompt,
        "",
        f"[guardrail feedback — retry {attempt}]",
        "Your previous response failed these output guardrails:",
        *(f"  - {v.name}: {v.message}" for v in failures),
        "",
    ]
    if str(sensitivity).strip().lower() in _PRIOR_EXCERPT_TIERS:
        lines += [
            "Previous response (excerpt):",
            prior_text[:2000],
            "",
        ]
    lines.append("Produce a corrected response that satisfies every guardrail above.")
    return "\n".join(lines)


def llm_with_guardrails(
    prompt: str,
    *,
    system: Optional[str] = None,
    task_type: str = "synthesis",
    guardrails: Optional[Sequence[str]] = None,
) -> "llm_gateway.LLMResult":
    """A governed ``llm()`` call validated against the skill's guardrails,
    retried with failure feedback (#24 — the CrewAI §2.5 retry contract).

    Resolution:
      * ``guardrails`` — explicit names, or (default) the skill's DECLARED
        ``metadata.guardrails`` from the registry. Only TEXT-applicable
        checkers run at this boundary (dict-shape checkers run at the
        ``@anton_skill`` output boundary instead).
      * retry budget = ``min(guardrail_max_retries, tier budget)`` —
        see :data:`TIER_RETRY_BUDGETS`. MNPI ⇒ 0: the first failure raises
        immediately, no second LLM call is ever made.

    With no text-applicable guardrails resolved this is EXACTLY one governed
    ``llm()`` call — zero added overhead, no audit noise (pass-through).

    Raises :class:`GuardrailRetriesExhausted` when the budget is spent and the
    output still fails — the degrade-don't-burn shape (structured fields, app
    handler → 502, ``last_result`` attached for explicit manual-path
    degrades). Each attempt goes through the FULL governed ``llm()`` path, so
    the #no-mnpi-to-cloud lane (was cited as §5.4), #57 budget gate and #67
    llm-call cap all still apply per-call."""
    sctx = llm_gateway.current_skill_llm_context()
    if sctx is None:
        # Same contract as llm(): outside an @anton_skill body this is a
        # mis-wiring, not a runtime condition. Delegate so the error message
        # (and any future relaxation) lives in ONE place.
        return llm_gateway.llm(prompt, system=system, task_type=task_type)

    declared_retries = 0
    names: Sequence[str]
    if guardrails is not None:
        names = tuple(guardrails)
    else:
        names = ()
    try:
        from routines.skills.registry import load_skill_metadata  # lazy — no import cycle

        meta = load_skill_metadata(sctx.skill)
        declared_retries = meta.guardrail_max_retries
        if guardrails is None:
            names = meta.guardrails
    except KeyError:
        # Unregistered skill (direct unit-test call) — no declared guardrails
        # to enforce unless the caller passed an explicit list; budget stays 0.
        pass

    # Only text-applicable names matter at this boundary. Unknown names (an
    # explicit caller list only — declared names are boot-validated) stay in
    # so they fail closed inside the loop.
    active = [
        n for n in names
        if (n in _CHECKERS and "text" in _CHECKERS[n].modes)
        or n not in known_guardrail_names()
    ]
    if not active:
        return llm_gateway.llm(prompt, system=system, task_type=task_type)

    budget = effective_retry_budget(declared_retries, sctx.sensitivity)
    attempt_prompt = prompt
    out = None
    failures: list[GuardrailVerdict] = []
    attempts = 0

    for attempt in range(budget + 1):
        attempts = attempt + 1
        out = llm_gateway.llm(attempt_prompt, system=system, task_type=task_type)
        verdicts = evaluate_guardrails(active, out.text, mode="text")
        failures = _failures(verdicts)
        if not failures:
            _write_guardrail_audit(
                skill=sctx.skill, run_id=sctx.run_id, action="guardrail_verdict",
                status="ok", phase="llm", sensitivity=sctx.sensitivity,
                attempt=attempts, budget=budget, verdicts=verdicts,
            )
            return out
        if attempt < budget:
            logger.warning(
                "guardrail fail (skill=%s attempt=%d/%d): %s — retrying with feedback",
                sctx.skill, attempts, budget + 1,
                "; ".join(f"{v.name}: {v.message}" for v in failures),
            )
            _write_guardrail_audit(
                skill=sctx.skill, run_id=sctx.run_id, action="guardrail_retry",
                status="retry", phase="llm", sensitivity=sctx.sensitivity,
                attempt=attempts, budget=budget, verdicts=verdicts,
            )
            attempt_prompt = _feedback_prompt(
                prompt, failures, out.text, attempts, sensitivity=sctx.sensitivity,
            )

    # Budget spent, still failing → degrade honestly (never a fabricated pass).
    _write_guardrail_audit(
        skill=sctx.skill, run_id=sctx.run_id, action="guardrail_verdict",
        status="fail", phase="llm", sensitivity=sctx.sensitivity,
        attempt=attempts, budget=budget, verdicts=failures,
    )
    logger.warning(
        "guardrail retries exhausted: skill=%s attempts=%d budget=%d failed=[%s] run_id=%s",
        sctx.skill, attempts, budget,
        ", ".join(v.name for v in failures), sctx.run_id,
    )
    raise GuardrailRetriesExhausted(
        skill_name=sctx.skill,
        run_id=sctx.run_id,
        sensitivity=sctx.sensitivity,
        attempts=attempts,
        budget=budget,
        failures=failures,
        last_result=out,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Output-boundary validation (called by the @anton_skill wrapper)
# ─────────────────────────────────────────────────────────────────────────────


def record_output_guardrails(
    skill: str,
    guardrail_names: Sequence[str],
    *,
    result: Any,
    usage: Any,
    run_id: Optional[str],
    sensitivity: str,
) -> None:
    """Evaluate the skill's declared guardrails against its structured RESULT
    and record the verdicts (#24 — "every skill output validated against its
    declared guardrails").

    Advisory by design: the body already ran (side effects included), so a
    fail here is stamped on the tool-side audit context (``usage`` — it lands
    on the after-hook audit row), written as its own structured audit row and
    logged loudly — but the completed result is still returned. The blocking
    + retry teeth live at the LLM boundary (:func:`llm_with_guardrails`),
    where a retry is side-effect-free. NEVER raises — a guardrail-runtime bug
    must not take down a completed run (same boundary rule as #76 capture)."""
    try:
        if not guardrail_names:
            return
        verdicts = evaluate_guardrails(tuple(guardrail_names), result, mode="dict")
        failed = _failures(verdicts)
        status = "fail" if failed else "ok"
        if isinstance(usage, dict):
            usage["guardrails"] = {
                "status": status,
                "checked": sum(1 for v in verdicts if v.checked),
                "declarative_or_skipped": sum(1 for v in verdicts if not v.checked),
                "failed": [v.name for v in failed],
            }
        _write_guardrail_audit(
            skill=skill, run_id=run_id, action="guardrail_verdict",
            status=status, phase="output", sensitivity=sensitivity,
            attempt=1, budget=0, verdicts=verdicts,
        )
        if failed:
            logger.warning(
                "output guardrail fail (skill=%s run_id=%s): %s",
                skill, run_id,
                "; ".join(f"{v.name}: {v.message}" for v in failed),
            )
    except Exception as e:  # noqa: BLE001 — observability never breaks a completed run
        logger.warning(
            "record_output_guardrails(%s) failed (non-fatal): %s", skill, e,
        )


__all__ = [
    "TIER_RETRY_BUDGETS",
    "GuardrailRetriesExhausted",
    "GuardrailVerdict",
    "effective_retry_budget",
    "evaluate_guardrails",
    "known_guardrail_names",
    "llm_with_guardrails",
    "record_output_guardrails",
    "register_guardrail",
    "tier_retry_budget",
    "validate_guardrail_names",
]
