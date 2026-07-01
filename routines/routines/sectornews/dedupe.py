"""Title-similarity dedupe for sector-news articles.

Cheap pass before LLM scoring: same article from different aggregators (a
press-release picked up by 5 outlets) all get one entry, ranked by source
quality / completeness.

Algorithm:
    Normalise titles (lowercase, strip punctuation, collapse whitespace),
    then compute Jaccard overlap on token sets. Threshold ~0.7 catches
    "DemoTelco Buys Out Hutchison" vs "DemoTelco agrees buyout of Hutchison
    stake" while preserving genuinely different stories.

This runs zero-LLM-cost, so we always do it before the relevance pass.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from routines.sectornews.firecrawl_client import FCResult


_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
# Common English stopwords that don't help dedupe
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "to", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "by", "at",
}


def _tokens(title: str) -> set[str]:
    s = _PUNCT.sub(" ", title.lower())
    s = _WS.sub(" ", s).strip()
    return {t for t in s.split() if len(t) > 2 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe(
    items: Iterable[FCResult],
    *,
    threshold: float = 0.7,
) -> list[FCResult]:
    """Return a deduplicated list. When duplicates are found, keep the one
    with the longest snippet (heuristic: more complete article)."""
    items_list = list(items)
    if not items_list:
        return []

    keep: list[FCResult] = []
    keep_tokens: list[set[str]] = []

    # Sort by snippet length descending so longer snippets seed the cluster
    items_list.sort(key=lambda r: len(r.snippet), reverse=True)

    for item in items_list:
        toks = _tokens(item.title)
        if not toks:
            keep.append(item)
            keep_tokens.append(toks)
            continue
        is_dup = False
        for existing_toks in keep_tokens:
            if _jaccard(toks, existing_toks) >= threshold:
                is_dup = True
                break
        if not is_dup:
            keep.append(item)
            keep_tokens.append(toks)

    return keep
