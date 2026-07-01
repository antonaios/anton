"""Prompt-injection heuristics (#sec-injection-guard 3a).

Faithful port of protectai/rebuff ``python-sdk/rebuff/detect_pi_heuristics.py``
(Apache-2.0, repo archived 2024-08-07 — **lifted, not depended on**; Rebuff is
dependency-rotten and 3/4 of its layers force cloud egress, see
``evaluations/REBUFF-EVALUATION-2026-06-13.md``). The keyword arrays, the
``verb x adjective x object x preposition`` phrase generation (~8,800 phrases),
and the ``SequenceMatcher`` + positional-word scoring are Rebuff's, retained
verbatim. Pure + dependency-free (stdlib ``re`` + ``difflib``); no I/O, no
network — the audit + never-block orchestration lives in ``scan.py``.

ANTON's modifications (Apache-2.0 §4 — derivative work, changes noted):

  * **Precompute once.** Rebuff regenerated all 8,800 phrases (and re-normalised
    them) on EVERY call. We build the normalised keyword set once at import,
    grouped by word-length so input windows of a given length are generated once.
  * **Semantics-preserving prune.** Rebuff's per-(keyword, window) score is
    ``matched_word_score - similarity * (1/(2*MAX))`` and ``matched_word_score``
    is 0 unless >=1 word matches POSITIONALLY; so a window with no positional
    overlap yields ``adjusted <= 0``, which can never beat the running max
    (seeded at 0.0). We therefore (a) early-exit when the input shares NO word
    with the injection vocabulary, (b) skip a window with no vocab word, and
    (c) skip the O(n^2) ``SequenceMatcher`` whenever ``matched_words_count == 0``.
    The returned score is byte-identical to the naive port — only the wasted
    work is removed, making a per-document (not per-token) scan tractable.
  * **Bounded input.** A pathological input can't make the scan unbounded — only
    the leading ``max_scan_words`` are scanned (overridable); ``truncated`` is
    reported so a caller knows a tail went unscanned.

Tuning (threshold, the ML scorer, graduation to fail-closed block) is 3b.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

# ── Rebuff keyword arrays (Apache-2.0, verbatim) ─────────────────────────────
_VERBS = [
    "Ignore", "Disregard", "Skip", "Forget", "Neglect", "Overlook", "Omit",
    "Bypass", "Pay no attention to", "Do not follow", "Do not obey",
]
_ADJECTIVES = [
    "", "prior", "previous", "preceding", "above", "foregoing", "earlier",
    "initial",
]
_PREPOSITIONS = [
    "", "and start over", "and start anew", "and begin afresh",
    "and start from scratch",
]
_OBJECTS = [
    "content", "text", "instructions", "instruction", "directives", "directive",
    "commands", "command", "context", "conversation", "input", "inputs", "data",
    "message", "messages", "communication", "response", "responses", "request",
    "requests",
]

_MAX_MATCHED_WORDS = 5  # Rebuff's max_matched_words

# Detection threshold — Rebuff's ``max_heuristic_score`` default (0.75).
DEFAULT_THRESHOLD = 0.75
THRESHOLD_ENV = "AGENTIC_INJECTION_SCORE_THRESHOLD"

# Defensive scan cap (leading N words). Even with the prune, the cheap
# positional check is O(words x keywords); bound it so a huge document can't
# stall ingestion. Overridable.
DEFAULT_MAX_SCAN_WORDS = 30_000   # high enough that real documents scan fully;
MAX_SCAN_WORDS_ENV = "AGENTIC_INJECTION_MAX_SCAN_WORDS"  # only huge inputs go bounded

# Hard CHARACTER cap applied BEFORE normalisation / splitting / hashing, so a
# huge input can't force O(len) regex + allocation before the word cap takes
# effect (codex SEV-2 / data-handling P2). Overridable.
DEFAULT_MAX_SCAN_CHARS = 400_000
MAX_SCAN_CHARS_ENV = "AGENTIC_INJECTION_MAX_SCAN_CHARS"

# Per-token character cap — a single pathological token (no whitespace) must not
# be able to blow up a SequenceMatcher call; genuine injection words are short,
# so truncating only affects junk tokens. Internal (not a tuning knob).
_MAX_TOKEN_CHARS = 64

# Hard cap on total SequenceMatcher.ratio() calls per scan — a DoS bound so a
# crafted high-candidate input that never crosses the threshold still terminates
# (a guard must not be slow on the very content it inspects). Overridable.
DEFAULT_MAX_RATIO_CALLS = 200_000
MAX_RATIO_CALLS_ENV = "AGENTIC_INJECTION_MAX_RATIO_CALLS"


def _normalize_string(input_string: str) -> str:
    """Rebuff ``normalize_string``: lowercase, strip punctuation/underscore,
    collapse whitespace."""
    result = input_string.lower()
    result = re.sub(r"[^\w\s]|_", "", result)
    result = re.sub(r"\s+", " ", result)
    return result.strip()


def _generate_injection_keywords() -> list[str]:
    """Rebuff ``generate_injection_keywords``: the verb x adjective x object x
    preposition product."""
    out: list[str] = []
    for verb in _VERBS:
        for adjective in _ADJECTIVES:
            for obj in _OBJECTS:
                for preposition in _PREPOSITIONS:
                    out.append(f"{verb} {adjective} {obj} {preposition}")
    return out


@dataclass(frozen=True)
class _Keyword:
    text: str                  # normalised keyword phrase
    parts: tuple[str, ...]     # its words


def _build_keyword_index() -> dict[int, tuple[_Keyword, ...]]:
    """Normalise + dedupe the generated phrases once, grouped by word count."""
    by_len: dict[int, list[_Keyword]] = {}
    seen: set[str] = set()
    for raw in _generate_injection_keywords():
        norm = _normalize_string(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        parts = tuple(norm.split(" "))
        by_len.setdefault(len(parts), []).append(_Keyword(norm, parts))
    return {length: tuple(kws) for length, kws in by_len.items()}


_KEYWORD_INDEX: dict[int, tuple[_Keyword, ...]] = _build_keyword_index()
# Every word that appears in any keyword position — the whole-input fast-path set.
_KEYWORD_VOCAB: frozenset[str] = frozenset(
    word for kws in _KEYWORD_INDEX.values() for kw in kws for word in kw.parts
)


def _build_pos_index() -> dict[int, dict[tuple[int, str], tuple[_Keyword, ...]]]:
    """Inverted ``(position, word) -> keywords`` index, per keyword-length. A
    window can only score against a keyword it shares >=1 POSITIONAL word with,
    so a scan gathers just those candidates instead of testing all ~8,800
    keywords per window. This is EXACT, not heuristic: a keyword absent from the
    index for a window has ``matched_words_count == 0`` -> ``adjusted <= 0`` ->
    cannot beat the running max (seeded 0.0). Benign windows yield an empty
    candidate set and cost ~nothing, so a per-document scan stays well under a
    second even on adversarial/repetitive input."""
    out: dict[int, dict[tuple[int, str], tuple[_Keyword, ...]]] = {}
    for length, kws in _KEYWORD_INDEX.items():
        idx: dict[tuple[int, str], list[_Keyword]] = {}
        for kw in kws:
            for pos, word in enumerate(kw.parts):
                idx.setdefault((pos, word), []).append(kw)
        out[length] = {key: tuple(val) for key, val in idx.items()}
    return out


_POS_INDEX: dict[int, dict[tuple[int, str], tuple[_Keyword, ...]]] = _build_pos_index()


def _matched_words_score(substring_parts: list[str], keyword_parts: tuple[str, ...]) -> float:
    """Rebuff ``get_matched_words_score``: positional word-equality; 0.5 base +
    0.5 * min(matched / MAX, 1); 0 when no word matches positionally."""
    matched = sum(1 for part, word in zip(keyword_parts, substring_parts) if word == part)
    if matched <= 0:
        return 0.0
    return 0.5 + 0.5 * min(matched / _MAX_MATCHED_WORDS, 1)


def _max_scan_words() -> int:
    raw = os.environ.get(MAX_SCAN_WORDS_ENV)
    if raw is None:
        return DEFAULT_MAX_SCAN_WORDS
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_MAX_SCAN_WORDS
    return val if val > 0 else DEFAULT_MAX_SCAN_WORDS


def _max_ratio_calls() -> int:
    raw = os.environ.get(MAX_RATIO_CALLS_ENV)
    if raw is None:
        return DEFAULT_MAX_RATIO_CALLS
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_MAX_RATIO_CALLS
    return val if val > 0 else DEFAULT_MAX_RATIO_CALLS


def _max_scan_chars() -> int:
    raw = os.environ.get(MAX_SCAN_CHARS_ENV)
    if raw is None:
        return DEFAULT_MAX_SCAN_CHARS
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_MAX_SCAN_CHARS
    return val if val > 0 else DEFAULT_MAX_SCAN_CHARS


def threshold() -> float:
    """Effective detection threshold (env-overridable, clamped to (0, 1])."""
    raw = os.environ.get(THRESHOLD_ENV)
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        val = float(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_THRESHOLD
    return val if 0.0 < val <= 1.0 else DEFAULT_THRESHOLD


def heuristic_score(
    text: str,
    *,
    max_scan_words: Optional[int] = None,
    early_exit_at: Optional[float] = None,
) -> tuple[float, Optional[str], bool]:
    """Return ``(highest_score, matched_keyword, bounded)`` for ``text``.

    Faithful to Rebuff's ``detect_prompt_injection_using_heuristic_on_input``
    (``adjusted = matched_word_score - similarity * (1 / (2 * MAX))``, maximised
    over every keyword x input-window pair), via the inverted index so the
    expensive ``SequenceMatcher`` runs only on positionally-matching candidates.

    Four bounds keep a per-document scan time- AND memory-bounded on ANY input —
    a guard must not be slow on the very content it inspects:
      * a CHARACTER cap (``_max_scan_chars``) applied BEFORE normalisation, so the
        O(len) regex / split / hash can't be forced huge (codex SEV-2);
      * a per-token character cap (``_MAX_TOKEN_CHARS``) — a single giant token
        can't blow up a ``SequenceMatcher`` call;
      * a leading-WORD cap (``max_scan_words``);
      * an ``early_exit_at`` short-circuit + a ``SequenceMatcher`` call budget
        (``_max_ratio_calls``), checked BEFORE each ratio() call (codex SEV-3).
    ``bounded`` is True when ANY of the char / word / budget caps cut the scan
    short. CRUCIAL: a bounded scan that did NOT flag is INCONCLUSIVE, not clean —
    a crossing phrase could lie in the unscanned tail; ``scan.py`` audits the
    bounded case so a coverage gap is never a silent "benign" (codex SEV-2). With
    ``early_exit_at=None`` the scan is the EXACT faithful max for inputs within
    the bounds (the tests stay within them); the production path (``early_exit_at
    = threshold``) skips the costly ratio for any pair whose matched-word score
    is already below the threshold (it can't flag, since ``adjusted <= mw``).
    """
    if not isinstance(text, str) or not text:
        return 0.0, None, False
    char_cap = _max_scan_chars()
    bounded = len(text) > char_cap
    raw = text[:char_cap] if bounded else text

    normalized = _normalize_string(raw)
    if not normalized:
        return 0.0, None, bounded
    words = normalized.split(" ")
    cap = max_scan_words if max_scan_words is not None else _max_scan_words()
    if len(words) > cap:
        words = words[:cap]
        bounded = True
    # Per-token cap: a genuine injection word is short, so truncating only
    # affects junk tokens — but it bounds the size of any ``window_text`` that
    # reaches SequenceMatcher. Truncation alters a token's similarity, so it IS a
    # bound: mark ``bounded`` (an unflagged result is then audited as
    # inconclusive, never a silent benign — codex r3 SEV-2).
    if any(len(word) > _MAX_TOKEN_CHARS for word in words):
        words = [word[:_MAX_TOKEN_CHARS] for word in words]
        bounded = True

    # Fast path: input shares NO word with the injection vocabulary → no
    # positional match is possible for any keyword → score is 0.
    if not any(word in _KEYWORD_VOCAB for word in words):
        return 0.0, None, bounded

    highest = 0.0
    best_keyword: Optional[str] = None
    factor = 1.0 / (_MAX_MATCHED_WORDS * 2)
    n_words = len(words)
    ratio_budget = _max_ratio_calls()
    calls = 0
    for length, pos_index in _POS_INDEX.items():
        window_count = n_words - length + 1
        if window_count <= 0:
            continue
        for i in range(window_count):
            window = words[i : i + length]
            # Candidate keywords = those sharing >=1 POSITIONAL word with this
            # window (via the inverted index). Exact, not approximate: any
            # keyword NOT here has matched_words_count == 0 -> adjusted <= 0 ->
            # can't beat highest (>= 0). Benign windows -> empty set -> ~free.
            candidates: set[_Keyword] = set()
            for pos in range(length):
                hits = pos_index.get((pos, window[pos]))
                if hits:
                    candidates.update(hits)
            if not candidates:
                continue
            window_text = " ".join(window)
            for kw in candidates:
                mw = _matched_words_score(window, kw.parts)  # > 0 by construction
                # The ratio only LOWERS the score (adjusted = mw - sim * factor),
                # so a pair whose matched-word score is already below the flag
                # threshold can never flag (adjusted <= mw < threshold). In
                # production (early_exit_at set) skip its costly ratio: common
                # business words (data / content / request / response / message)
                # are Rebuff injection "objects", so without this gate benign
                # prose burns the budget on guaranteed-sub-threshold windows.
                if early_exit_at is not None and mw < early_exit_at:
                    continue
                if calls >= ratio_budget:
                    # Budget exhausted — checked BEFORE the ratio call (codex
                    # SEV-3); return max-so-far, scan marked bounded so the
                    # caller treats an unflagged result as INCONCLUSIVE.
                    return highest, best_keyword, True
                similarity = SequenceMatcher(None, window_text, kw.text).ratio()
                calls += 1
                adjusted = mw - similarity * factor
                if adjusted > highest:
                    highest = adjusted
                    best_keyword = kw.text
                    if early_exit_at is not None and highest >= early_exit_at:
                        # Detection certain — stop (bounds injection-heavy /
                        # adversarial input to its first flagging window).
                        return highest, best_keyword, bounded
    return highest, best_keyword, bounded


__all__ = [
    "DEFAULT_THRESHOLD",
    "THRESHOLD_ENV",
    "DEFAULT_MAX_SCAN_WORDS",
    "MAX_SCAN_WORDS_ENV",
    "DEFAULT_MAX_SCAN_CHARS",
    "MAX_SCAN_CHARS_ENV",
    "DEFAULT_MAX_RATIO_CALLS",
    "MAX_RATIO_CALLS_ENV",
    "threshold",
    "heuristic_score",
]
