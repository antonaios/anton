"""Stage 3 — cross-doc synthesis for the digest crew (#ingest-digest).

Entity-keyed fusion across the per-doc ``DocAnalysis`` list, contradiction
surfacing, and the wikilink web. The core is DETERMINISTIC + reproducible — no
LLM, no maths ([no-llm-maths]); the only optional LLM touch is a best-effort
cross-doc NARRATIVE via an injectable ``narrate_fn`` (the crew wires it to local
Ollama; tests inject a fake or omit it). stdlib + pydantic ONLY (the package
docstring) so the bridge suite loads it by file path.

Contradiction definition (deterministic): group every fact by
``(subject, field)`` after whitespace/case normalisation; within a group, a
``number`` fact is only comparable to another with the SAME unit+period (so FY24
vs FY25 revenue, or GBP vs USD, are distinct facts, NOT a contradiction). A
comparable sub-group holding ≥2 DISTINCT values is a contradiction — every
divergent value + its provenance is recorded for the reviewer (stage 4) and the
operator. This mirrors the #54 triple recall's detector consumes
(``recall/CONTRADICTION-NOTES.md``); see that note for why the query-time
detector additionally gates on claim DATES and this authoring-time one does not
(deal docs rarely carry per-fact dates; the divergent-value signal is what the
reviewer must see at ingest).

Entity fusion is intentionally CONSERVATIVE — exact match after whitespace/case
normalisation, NEVER fuzzy: a wrong "Acme" ≡ "Acme Holdings Ltd" merge would
silently conflate two real entities (the same speculative-inference hazard
``CONTRADICTION-NOTES.md`` warns against). Alias resolution, if ever wanted, is
a later operator-reviewed pass, not a default.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Awaitable, Callable

from _shared.digest.models import (
    AtomicFact,
    Contradiction,
    ContradictionEntry,
    DocAnalysis,
    FusedEntity,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

# narrate_fn(prompt) -> short cross-doc summary text, async. Injected by the
# crew (local Ollama); None disables the optional narrative.
NarrateFn = Callable[[str], Awaitable[str]]

# Cap what's folded into the NARRATIVE prompt so a large pile can't blow the
# local context window. The deterministic core (below) is uncapped.
_NARRATIVE_MAX_FACTS = 60
_NARRATIVE_MAX_CONTRADICTIONS = 20
_NARRATIVE_MAX_ENTITIES = 30


def norm_key(s: str) -> str:
    """Whitespace-collapsed, NFKC-normalised, case-folded key for grouping/dedup.
    NFKC folds compatibility + composition variants (NFC/NFD, full-width forms,
    and U+00A0 → space) so matching is stable across how a source encoded the
    text — ``str.split()`` alone does not treat a non-breaking space as a
    separator. Display values keep their original form; only the COMPARISON uses
    this."""
    return " ".join(unicodedata.normalize("NFKC", s or "").split()).casefold()


_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove a leading ``<think>…</think>`` reasoning block (qwen3 emits one
    when thinking-mode is ON). The crew also prepends ``/no_think`` to the
    narrative prompt, but strip defensively so the stored narrative is clean
    regardless of how ``narrate_fn`` is wired or whether the model honoured it."""
    return _THINK_RE.sub("", text or "", count=1).strip()


def fuse_entities(analyses: list[DocAnalysis]) -> list[FusedEntity]:
    """Conservative deterministic entity fusion (exact match after
    whitespace/case normalisation — never fuzzy). Canonical name = first-seen
    original casing; ``mentions`` = number of DISTINCT docs naming it. Input
    order is preserved for reproducibility."""
    order: list[str] = []
    canon: dict[str, str] = {}
    docs: dict[str, list[str]] = {}
    for a in analyses:
        seen_here: set[str] = set()
        for raw in a.entities:
            name = " ".join((raw or "").split())
            if not name:
                continue
            key = norm_key(name)
            if key in seen_here:
                continue  # one doc counts at most once toward mentions
            seen_here.add(key)
            if key not in canon:
                canon[key] = name
                docs[key] = []
                order.append(key)
            if a.path not in docs[key]:
                docs[key].append(a.path)
    return [
        FusedEntity(
            name=canon[k],
            wikilink=f"[[{canon[k]}]]",
            mentions=len(docs[k]),
            doc_paths=list(docs[k]),
        )
        for k in order
    ]


def _all_facts(analyses: list[DocAnalysis]) -> list[AtomicFact]:
    facts: list[AtomicFact] = []
    for a in analyses:
        facts.extend(a.facts)
    return facts


def find_contradictions(facts: list[AtomicFact]) -> list[Contradiction]:
    """Deterministic same-subject+field, divergent-value detection. ``number``
    facts only contradict another ``number`` at the SAME unit+period (a different
    period/unit is a distinct fact); facts of DIFFERENT kinds (number / claim /
    date / entity) on the same (subject, field) never cross-compare. Order-stable
    for reproducibility.

    Value distinctness is VERBATIM-STRING (after whitespace/case/NFKC norm) with
    NO numeric coercion — by design ([no-llm-maths]): "142" vs "142.0" are
    transcribed differently, so they surface as a (conservative) contradiction
    for the reviewer to clear, never silently reconciled. Over-flagging is safe;
    numeric reconciliation by code or LLM is not. All source mentions in a
    contradicting group are retained as ``entries`` (agreeing ones too) so the
    reviewer sees corroboration alongside divergence."""
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[AtomicFact]] = {}
    for f in facts:
        k = (norm_key(f.subject), norm_key(f.field))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(f)

    out: list[Contradiction] = []
    for k in order:
        members = groups[k]
        if len(members) < 2:
            continue
        # Sub-partition so numbers only compare within identical unit+period;
        # everything else compares within the whole (subject, field) group.
        sub_order: list[tuple[str, str, str]] = []
        sub: dict[tuple[str, str, str], list[AtomicFact]] = {}
        for m in members:
            # Bucket by KIND: a number contradicts only another number at the
            # SAME unit+period; facts of different kinds (number / claim / date /
            # entity) on the same (subject, field) never cross-compare — even a
            # unit-less number, which would otherwise collide with non-numbers in
            # a shared empty-context bucket (codex + clean-room review).
            if m.kind == "number":
                ctx = (m.kind, norm_key(m.unit), norm_key(m.period))
            else:
                ctx = (m.kind, "", "")
            if ctx not in sub:
                sub[ctx] = []
                sub_order.append(ctx)
            sub[ctx].append(m)
        for ctx in sub_order:
            items = sub[ctx]
            if len({norm_key(i.value) for i in items}) < 2:
                continue  # all agree (or a single value) — not a contradiction
            first = items[0]
            is_num = first.kind == "number"
            out.append(Contradiction(
                subject=first.subject,
                field=first.field,
                unit=first.unit if is_num else "",
                period=first.period if is_num else "",
                entries=[
                    ContradictionEntry(
                        value=i.value, unit=i.unit, period=i.period,
                        provenance=i.provenance,
                    )
                    for i in items
                ],
            ))
    return out


def _narrative_prompt(result: SynthesisResult, n_docs: int) -> str:
    ents = ", ".join(e.name for e in result.entities[:_NARRATIVE_MAX_ENTITIES]) \
        or "(none extracted)"
    fact_lines = []
    for f in result.facts[:_NARRATIVE_MAX_FACTS]:
        seg = f"- {f.subject} · {f.field} = {f.value}"
        if f.unit:
            seg += f" {f.unit}"
        if f.period:
            seg += f" ({f.period})"
        fact_lines.append(seg)
    facts_block = "\n".join(fact_lines) or "(no atomic facts extracted)"
    con_lines = []
    for c in result.contradictions[:_NARRATIVE_MAX_CONTRADICTIONS]:
        vals = " vs ".join(e.value for e in c.entries)
        con_lines.append(f"- {c.subject} · {c.field}: {vals}")
    con_block = "\n".join(con_lines) or "(none)"
    return (
        "You are consolidating atomic facts already extracted from a pile of "
        f"related deal documents ({n_docs} doc(s)). Write a SHORT factual "
        "synthesis (plain prose, <= 200 words) of what the documents collectively "
        "say. Do NOT invent facts, sources, or numbers; do NOT calculate; only "
        "summarise the facts below, and call out the listed contradictions "
        "plainly.\n\n"
        f"Entities: {ents}\n\n"
        f"Atomic facts:\n{facts_block}\n\n"
        f"Contradictions (same attribute, different value):\n{con_block}\n\n"
        "Synthesis:"
    )


async def synthesize_facts(
    analyses: list[DocAnalysis],
    *,
    project: str = "",
    narrate_fn: NarrateFn | None = None,
) -> SynthesisResult:
    """Stage 3: fuse entities, surface contradictions, build the wikilink web
    (all deterministic), then OPTIONALLY add a local-Ollama narrative.

    Only ``ok``/``skipped`` analyses contribute facts/entities — an ``error``
    doc never ran enrichment, so it carries nothing to fuse. Never raises on the
    narrative: a narrate failure leaves ``narrative=''`` and the deterministic
    result stands (best-effort, like the analyzer's per-doc enrichment)."""
    usable = [a for a in analyses if a.status != "error"]
    facts = _all_facts(usable)
    result = SynthesisResult(
        project=project,
        entities=fuse_entities(usable),
        facts=facts,
        contradictions=find_contradictions(facts),
    )
    if narrate_fn is not None and (result.facts or result.entities):
        try:
            text = await narrate_fn(_narrative_prompt(result, len(usable)))
            result.narrative = _strip_think(text)
        except Exception as e:  # noqa: BLE001 — narrative is best-effort
            logger.warning(
                "digest synthesize: narrative failed: %s", type(e).__name__
            )
            result.narrative = ""
    return result


__all__ = [
    "NarrateFn",
    "norm_key",
    "fuse_entities",
    "find_contradictions",
    "synthesize_facts",
]
