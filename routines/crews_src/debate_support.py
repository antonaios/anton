"""Pure, metagpt-free debate logic for the ``/debate`` crew (#36).

Why this module exists separately from ``debate_crew.py``: the crew module
defines MetaGPT ``Action`` / ``Role`` subclasses at import time, so it cannot
be imported without the crew venv's metagpt install. This module holds the
debate's *pure* logic — round resolution, prompt assembly, transcript
formatting — with ZERO third-party imports (stdlib only), exactly like
``_shared/boundary.py``. That keeps it loadable by file path from the bridge
venv, so the bug-prone bits (clamping, prompt assembly, the [[wikilink]]
citation discipline) get real unit coverage WITHOUT a metagpt install
(``tests/crew/test_debate_crew.py`` loads it that way). ``debate_crew.py``
imports everything here.

Deployed to the crew dir alongside ``debate_crew.py`` by
``routines/crew/install/install_metagpt.py``.
"""

from __future__ import annotations

import re
from typing import Any

# Shared vault-scan (integration: operator decision 2 — wire /debate vault
# access NOW). Stdlib-only, so debate_support stays file-path-loadable for the
# bridge test suite. The same primitives /explore uses.
from _shared.vault_scan import scan_vault_for_target, vault_root

# ── Round bounds ─────────────────────────────────────────────────────────────
# Default 3 per [[autonomous-crews]] §4. ``--rounds=N`` is configurable per §7
# ("lean yes, low priority"); the crew reads it from ``args.rounds``. MAX is the
# §7 example (5) — NOT a substitute for the real bound, which is the crew's
# 60k-token + 300s-wall-clock caps (the proxy kills an overrun as ``timeout``).
# The clamp only stops an absurd ``args.rounds`` from STARTING a runaway loop.
# NOTE (flagged): on qwen3:14b a 5-round debate is ~17 sequential LLM calls and
# may approach the 300s wall clock incl. cold model load — measure on the first
# real smoke and bump ``cost_cap_seconds`` if needed (the #31 hello_world
# 60→300 precedent). Default 3 (~11 calls) fits comfortably.
DEFAULT_ROUNDS = 3
MIN_ROUNDS = 1
MAX_ROUNDS = 5

# Defensive cap on caller-supplied evidence text (only ever hits the
# ContextLoader prompt once; the Bull/Bear see the *brief*, not raw evidence).
MAX_EVIDENCE_CHARS = 6000

ROLES = ["ContextLoader", "Bull", "Bear", "Moderator", "Synthesist"]


# ── Input resolution ─────────────────────────────────────────────────────────


def resolve_thesis(args: Any) -> str:
    """The thesis under debate. Required — an empty (or absent, or JSON-null)
    thesis is a hard error. The ``None`` check precedes ``str(...)`` so a JSON
    ``null`` is not coerced into the literal string ``"None"`` (which would slip
    past a naive non-empty guard and launch a meaningless debate)."""
    raw = (args or {}).get("thesis")
    if raw is None:
        raise ValueError("debate crew requires a non-empty args.thesis")
    thesis = str(raw).strip()
    if not thesis:
        raise ValueError("debate crew requires a non-empty args.thesis")
    return thesis


def resolve_rounds(args: Any) -> int:
    """Number of Bull↔Bear rounds. Reads ``args.rounds`` (``--rounds=N``);
    default 3; coerces a stringy int; clamps to [MIN_ROUNDS, MAX_ROUNDS].
    Invalid / missing → default, never a crash."""
    raw = (args or {}).get("rounds", DEFAULT_ROUNDS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    return max(MIN_ROUNDS, min(MAX_ROUNDS, n))


def resolve_evidence(args: Any) -> str:
    """Operator-supplied vault evidence OVERRIDE the BULL/BEAR may cite.

    ``args.evidence`` is an explicit override: when the caller pre-loads
    evidence text, the crew uses it verbatim and does NOT scan the vault. Capped
    defensively to bound prompt tokens; truncation is marked so the model knows
    the brief is partial. Returns ``""`` when no override was supplied — the
    signal :func:`load_vault_evidence` uses to fall back to a real vault scan."""
    ev = str((args or {}).get("evidence", "") or "").strip()
    if len(ev) > MAX_EVIDENCE_CHARS:
        ev = ev[:MAX_EVIDENCE_CHARS].rstrip() + "\n…[evidence truncated]"
    return ev


# Thesis tokens worth scanning the vault for: proper-noun-ish words (initial
# cap, ≥3 chars) + any "Quoted Phrase". Stopwords drop generic verbs/articles so
# a thesis like "DemoTelco should sell its B2B unit" scans for DemoTelco/B2B, not
# "Should"/"Sell". Heuristic, not NLP — the LLM still reasons over the hits.
_THESIS_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "should", "would", "could", "will", "shall", "may", "might", "must",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "into", "over",
    "and", "or", "but", "if", "then", "than", "as", "it", "its", "this", "that",
    "these", "those", "we", "they", "i", "you", "he", "she", "do", "does",
    "sell", "buy", "hold", "keep", "exit", "acquire", "divest", "spin", "off",
    "unit", "business", "company", "deal", "thesis", "not", "no", "yes",
})
_TERM_RE = re.compile(r'"([^"]{2,40})"|\b([A-Z][A-Za-z0-9&.\-]{2,})\b')
_MAX_EVIDENCE_HITS = 8


def thesis_search_terms(thesis: str) -> list[str]:
    """Salient terms to scan the vault for, drawn from a thesis sentence.

    Picks quoted phrases verbatim, then capitalised non-stopword tokens (the
    proper-noun candidates: companies, sectors, codenames). De-duplicated,
    order-preserving. Empty list → nothing scan-worthy (the caller then loads no
    auto-evidence and the roles argue from first principles)."""
    terms: list[str] = []
    seen: set[str] = set()
    for quoted, word in _TERM_RE.findall(thesis or ""):
        cand = (quoted or word).strip()
        if not cand or cand.lower() in _THESIS_STOPWORDS:
            continue
        key = cand.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(cand)
    return terms


def format_vault_evidence(hits: list[dict[str, Any]]) -> str:
    """Render scanned vault hits into a citable evidence block: one bullet per
    note with its ``[[wikilink]]`` and a short excerpt. Empty hits → ``""``."""
    lines: list[str] = []
    for h in hits:
        wl = h.get("wikilink") or ""
        ex = (h.get("excerpt") or "").strip()
        if not wl:
            continue
        lines.append(f"- {wl}: {ex}" if ex else f"- {wl}")
    return "\n".join(lines)


def load_vault_evidence(thesis: str, args: Any, *, root: Any = None) -> str:
    """Evidence brief for the debate, citing REAL vault notes ([[wikilinks]]).

    Resolution (operator decision 2 — wire /debate vault access NOW):
      1. ``args.evidence`` OVERRIDE — if the caller pre-loaded evidence, use it
         verbatim (the boundary-respecting escape hatch is preserved).
      2. Else SCAN the vault (the shared :func:`scan_vault_for_target`, the same
         layer /explore uses) for the thesis's salient terms, gather the top
         hits across terms, and format them with their wikilinks + excerpts.
      3. Else (no override, no hits) → ``""`` — the ContextLoader/Bull/Bear then
         argue from first principles and cite no wikilinks (no-invented-sources;
         the prompt rule already forbids inventing a [[link]]).

    Pure stdlib + filesystem; never raises (a missing/locked vault yields ``""``,
    degrading to first-principles). ``root`` is injectable for tests."""
    override = resolve_evidence(args)
    if override:
        return override
    vroot = root if root is not None else vault_root()
    terms = thesis_search_terms(thesis)
    if not terms:
        return ""
    collected: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    try:
        for term in terms:
            scan = scan_vault_for_target(vroot, term)
            for hit in scan.get("hits") or []:
                wl = hit.get("wikilink") or ""
                if wl and wl not in seen_links:
                    seen_links.add(wl)
                    collected.append(hit)
                if len(collected) >= _MAX_EVIDENCE_HITS:
                    break
            if len(collected) >= _MAX_EVIDENCE_HITS:
                break
    except OSError:
        return ""
    evidence = format_vault_evidence(collected)
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[:MAX_EVIDENCE_CHARS].rstrip() + "\n…[evidence truncated]"
    return evidence


# ── Prompt assembly ──────────────────────────────────────────────────────────

# The no-invented-sources discipline ([[CLAUDE]] §5.4, in spirit): a debate is
# only useful if its citations are real. Roles cite ONLY notes that appear in
# the brief; with no evidence loaded they argue from first principles and add
# NO wikilinks (rather than hallucinating plausible-looking [[Note]] names).
_CITE_RULE = (
    "Citation rule: cite vault notes ONLY as [[Note Title]] wikilinks, and ONLY "
    "for notes that appear in the brief above. Never invent a [[wikilink]] or "
    "cite a source not in the brief. If the brief has no evidence, argue from "
    "first principles and add NO wikilink citations."
)


def _block(title: str, text: str) -> str:
    """An optional, titled prompt section — empty text drops the whole block."""
    text = (text or "").strip()
    return f"{title}:\n{text}\n\n" if text else ""


def context_prompt(thesis: str, evidence: str) -> str:
    if evidence:
        ev = (
            "Vault evidence provided (the ONLY notes you may cite as "
            f"[[wikilinks]]):\n{evidence}\n\n"
        )
    else:
        ev = "No vault evidence was loaded for this debate.\n\n"
    return (
        "You are a neutral context loader preparing a balanced brief for a "
        "structured debate.\n\n"
        f"THESIS: {thesis}\n\n"
        f"{ev}"
        "Produce a SHORT, even-handed brief (max ~150 words) that BOTH a "
        "supporter and a challenger could draw on:\n"
        "  - 3-5 key facts or considerations bearing on the thesis\n"
        "  - for each, note which way it cuts (for / against / neutral)\n"
        "Stay strictly neutral — do not take a side. " + _CITE_RULE
    )


def bull_prompt(
    thesis: str, brief: str, prior_state: str, prior_bear: str,
    round_no: int, rounds: int,
) -> str:
    return (
        f"You are the BULL in round {round_no} of {rounds}. Argue FOR this "
        "thesis.\n\n"
        f"THESIS: {thesis}\n\n"
        + _block("BALANCED BRIEF", brief)
        + _block("POSITION SO FAR (moderator)", prior_state)
        + _block("BEAR'S LAST ARGUMENT (rebut its weakest point)", prior_bear)
        + "Make the STRONGEST case FOR the thesis this round: 3-6 crisp points, "
        "max ~150 words. Do not repeat earlier points verbatim — advance the "
        "argument. " + _CITE_RULE
    )


def bear_prompt(
    thesis: str, brief: str, prior_state: str, current_bull: str,
    round_no: int, rounds: int,
) -> str:
    return (
        f"You are the BEAR in round {round_no} of {rounds}. Argue AGAINST this "
        "thesis.\n\n"
        f"THESIS: {thesis}\n\n"
        + _block("BALANCED BRIEF", brief)
        + _block("POSITION SO FAR (moderator)", prior_state)
        + _block("BULL'S ARGUMENT THIS ROUND (rebut it directly)", current_bull)
        + "Make the STRONGEST case AGAINST the thesis this round: 3-6 crisp "
        "points, max ~150 words. Attack the Bull's specific claims, not a straw "
        "man. " + _CITE_RULE
    )


def moderator_prompt(
    thesis: str, round_no: int, rounds: int, bull: str, bear: str,
    prior_state: str,
) -> str:
    return (
        f"You are the neutral MODERATOR after round {round_no} of {rounds} of a "
        "debate.\n\n"
        f"THESIS: {thesis}\n\n"
        + _block("BULL (for)", bull)
        + _block("BEAR (against)", bear)
        + _block("POSITION BEFORE THIS ROUND", prior_state)
        + "In 2-4 sentences, summarise how positions CHANGED this round: which "
        "new points landed, where the sides moved closer or further apart, and "
        "what remains contested. Stay strictly neutral — do NOT declare a winner."
    )


def synthesis_prompt(thesis: str, brief: str, transcript: str) -> str:
    return (
        "You are the SYNTHESIST closing a structured debate.\n\n"
        f"THESIS: {thesis}\n\n"
        + _block("BALANCED BRIEF", brief)
        + f"FULL DEBATE TRANSCRIPT:\n{transcript}\n\n"
        + "Write the final note with EXACTLY these three markdown sections:\n"
        "## Consensus\nWhere the Bull and Bear actually converged (may be "
        "\"none\").\n"
        "## Open disagreement\nWhere they still diverge, and why it matters.\n"
        "## Recommended action\nOne concrete, decision-useful recommendation "
        "given the balance of argument.\n\n"
        "Be concise and decision-useful (max ~200 words total). " + _CITE_RULE
    )


# ── Verdict classification (#captures-to-vault-crews) ─────────────────────────
# The crew's own structured CONCLUSION step — a verdict + recommended action it
# can capture to the vault. Produced INSIDE the run (a legitimate analytical
# finding, like the Moderator's per-round read), NOT re-derived at capture time
# ([[no-invented-sources]] in spirit). Grammar-constrained via
# ``ollama_structured_chat``'s ``format`` schema — a plain JSON ask is unreliable.

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "refuted", "mixed"]},
        "recommended_action": {"type": "string"},
    },
    "required": ["verdict", "recommended_action"],
}

VERDICT_VALUES = ("supported", "refuted", "mixed")


def verdict_prompt(thesis: str, synthesis: str, transcript: str) -> str:
    """Classify the debate outcome from the synthesis + transcript ONLY."""
    return (
        "You are the SYNTHESIST classifying the OUTCOME of a structured debate. "
        "Judge ONLY on the arguments actually made below — do not introduce new "
        "facts or cite anything.\n\n"
        f"THESIS: {thesis}\n\n"
        + _block("SYNTHESIS", synthesis)
        + _block("FULL TRANSCRIPT", transcript)
        + "Return two fields:\n"
        "  - verdict: \"supported\" if the bull case prevailed, \"refuted\" if "
        "the bear case prevailed, or \"mixed\" if it is genuinely balanced / "
        "unresolved.\n"
        "  - recommended_action: ONE concise, decision-useful next step "
        "(max ~20 words)."
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def thesis_slug(thesis: str, *, max_len: int = 60) -> str:
    """Filesystem-safe, readable slug for a thesis note filename
    (``Topics/Theses/<slug>.md``). Lowercased, dashed, truncated."""
    s = _SLUG_RE.sub("-", str(thesis or "").strip().lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "thesis"


# ── Transcript formatting ────────────────────────────────────────────────────


def round_tag(round_no: int) -> str:
    """Short marker prefixed onto a role's audit ``output_summary`` so repeated
    roles (Bull/Bear/Moderator recur every round) stay legible in the log."""
    return f"(round {round_no})"


def format_round(round_no: int, bull: str, bear: str, summary: str) -> str:
    return (
        f"### Round {round_no}\n"
        f"BULL: {(bull or '').strip()}\n"
        f"BEAR: {(bear or '').strip()}\n"
        f"MODERATOR: {(summary or '').strip()}\n"
    )


def format_transcript(rounds_data: list[tuple[int, str, str, str]]) -> str:
    """``rounds_data`` is a list of ``(round_no, bull, bear, summary)`` tuples."""
    return "\n".join(format_round(*r) for r in rounds_data).strip()


__all__ = [
    "DEFAULT_ROUNDS",
    "MIN_ROUNDS",
    "MAX_ROUNDS",
    "MAX_EVIDENCE_CHARS",
    "ROLES",
    "resolve_thesis",
    "resolve_rounds",
    "resolve_evidence",
    "thesis_search_terms",
    "format_vault_evidence",
    "load_vault_evidence",
    "context_prompt",
    "bull_prompt",
    "bear_prompt",
    "moderator_prompt",
    "synthesis_prompt",
    "verdict_prompt",
    "VERDICT_SCHEMA",
    "VERDICT_VALUES",
    "thesis_slug",
    "round_tag",
    "format_round",
    "format_transcript",
]
