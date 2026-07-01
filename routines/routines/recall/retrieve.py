"""Retrieve from the embedding index.

Pipeline (#54-rrf — Reciprocal Rank Fusion, superseding the #54b
hand-tuned weighted sum; frontmatter triad contract locked at
CLAUDE.md §3 rule 12 by #54a):

    1. Embed the query.
    2. Vector lane: cosine-rank chunks against the query, keep best
       per file (long docs get fair representation since each chunk
       competes individually). Falls back to whole-file note
       embeddings when the chunks table is empty (legacy index).
    3. FTS5 lane: BM25-rank ``recall_fts`` matches against the same
       query, take the top ``3 × limit`` candidates.
    4. Apply frontmatter filter (Python-side WHERE) to both lanes via
       the shared ``notes_by_path`` candidate set.
    5. Merge by path → fuse with **Reciprocal Rank Fusion** (RRF):

         rrf(doc) = Σ_channel 1 / (k + rank_channel(doc))      [k = 60]

       Each channel contributes ``1/(k+rank)`` where ``rank`` is the
       doc's 1-based position *within that channel's own ranking*
       (best = rank 1). A doc absent from a channel simply contributes
       nothing from that channel. RRF needs no per-channel weight
       tuning and is robust to score-scale mismatch between cosine
       (bounded [0,1]) and BM25 (unbounded, sign-flipped) — the reason
       both agentmemory and obsidian-hybrid-search use it. ``k=60`` is
       the canonical TREC value carried by both external systems.

    6. ``importance``, ``expires`` decay and ``source_tier`` are a
       **post-fusion multiplier** on the RRF score, NOT fused channels:

         final = rrf(doc)
                 × importance_multiplier(importance)   # 1–5 → ×(imp/3)
                 × expires_decay                        # ×0.5 if expired
                 × source_tier_multiplier(tier)         # provenance tier
                 × contradiction_penalty                # #54-contradiction

       ``importance`` (1–5, default 3 for unset) maps to a multiplier
       ``importance / 3`` so the operator-neutral midpoint (3) is ×1.0,
       a flagged-important note (5) is ×1.67, and a deprioritised note
       (1) is ×0.33. ``expires`` decay halves the score for past-dated
       claims. The source-tier multiplier (gbrain-pattern adoption,
       2026-06-10 eval) weights by PROVENANCE QUALITY — explicit
       ``source_tier`` frontmatter (1 primary / 2 internal / 3 scraped
       news) wins, else the sector-news intake path defaults to tier 3
       and everything else to the ×1.0-neutral tier 2, so untagged
       non-news vaults score byte-identically (see
       ``resolve_source_tier``). The contradiction penalty
       (#54-contradiction) is a further multiplier applied to the
       *older* of two same-subject / same-field facts that disagree
       (default ×0.85; see ``apply_contradiction_penalty``).
    7. Sort by final score DESC, truncate to ``limit``.

The component fields on the ``NoteHit`` (``vector_score`` /
``fts_score`` / ``importance`` / ``expires_decay``) are PRESERVED for
the explainability contract (recall-query Iron Law clause 1, the
``/api/recall`` component breakdown, ``recall query --explain``):
``vector_score`` / ``fts_score`` still carry each channel's *raw*
normalised score in [0, 1] (cosine, and FTS5 rank min-max normalised),
so the operator can still sanity-check *why* a doc surfaced. The RRF
contribution per channel (the actual fusion input) is additionally
exposed on ``vector_rrf`` / ``fts_rrf`` / ``rrf_score``.

If the ``recall_fts`` sidecar is absent or empty (e.g. the operator
hasn't re-indexed since upgrade), the FTS channel is simply empty and
RRF degrades to the vector channel alone (× the importance/expires
multiplier). The ranking still differs from pure-vector when notes
carry frontmatter ``importance`` — that's intentional; the triad
becomes useful the moment it's set, with or without FTS.

#45 / vault-graph — the wikilink graph is now wired as an OPT-IN **3rd
RRF channel** (vector + fts5 + graph), agentmemory's BM25+vector+graph
triple-stream reached via Anton's governed path. Design (v1,
"graph-proximity-to-seed"): the vector+fts candidate set is left
UNCHANGED (the graph adds no new candidates). Seeds = the top
``_GRAPH_SEED_TOP_K`` candidates by the *preliminary* vector+fts RRF,
plus any candidate whose path-stem/title carries an exact query token
(the "the query *is* an entity" structural anchor). The candidate set is
then ranked by minimum undirected hop-distance to a seed (cap
``_GRAPH_HOPS``) and that ranking is fused in as the 3rd leg:

    graph_rrf(doc) = 1 / (k + graph_rank)   graph_rank = distance + 1

(the seed sits at distance 0 → rank 1; its 1-hop neighbours → rank 2; …).
The leg is **additive**: a candidate absent from the graph, or beyond the
hop cap, contributes nothing — exactly like a vector/FTS-absent doc — so
the fusion formula is unchanged, just one more ``1/(k+rank)`` term. **OFF
by default** behind the ``graph`` flag; when off, /recall is byte-
identical to the two-leg pipeline. See ``_graph_channel``.

#45-expansion — the graph-EXPANSION leg is the deliberate follow-up the
proximity v1 left out: it INJECTS the seed neighbours as NEW candidates
(who-touched-style relational recall) rather than only re-ranking the
existing set. It is a **4th, separate RRF leg** parallel to the proximity
leg (NOT a replacement): the same seed set (top vector+fts RRF candidates +
query-token anchors) is expanded ≤``graph_expand_hops`` over the vault
graph; every neighbour that is (a) NOT already a candidate AND (b) present
in ``notes_by_path`` (i.e. it passed the SAME frontmatter / sensitivity
gate as every candidate — #no-mnpi-to-cloud (was cited as §5.4), never
smuggle a higher-sensitivity note in via expansion) is injected with a
graph-derived rank:

    graph_expand_rrf(doc) = 1 / (k + expand_rank)   expand_rank = distance + 1

(a 1-hop neighbour → distance 1 → rank 2 → ``1/(k+2)``). Because the seed
itself is always an existing candidate, an injected node sits at distance
≥1. The leg is **additive** and **OFF by default** behind the separate
``graph_expand`` flag; when off the candidate universe is UNCHANGED, the
graph/networkx layer is NEVER imported, and /recall is byte-identical.
Best-effort: a graph build/query fault degrades to the non-expanded result
without raising. ``graph`` (proximity) and ``graph_expand`` (injection) are
independent — either, both, or neither. See ``_graph_expansion_channel``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from routines.recall.index import (
    DEFAULT_INDEX_DB,
    DEFAULT_INDEX_DIR,
    parse_expires_iso,
    parse_importance,
    unpack_embedding,
)
from routines.shared.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


# Oversampling factor on each lane before fusion. 3× the requested limit
# per lane so the merge has enough room to re-rank.
_OVERSAMPLE = 3

# Reciprocal Rank Fusion constant (#54-rrf). k=60 is the canonical TREC
# value carried by both agentmemory and obsidian-hybrid-search. Larger k
# flattens the contribution curve (later ranks matter more); smaller k
# sharpens it (rank-1 dominates). 60 is the well-validated default.
_RRF_K = 60

# Importance → post-fusion multiplier. importance is 1–5 (default 3 for
# unset). The neutral midpoint (3) maps to ×1.0; the multiplier is
# ``importance / 3``. This is a POST-FUSION multiplier on the RRF score,
# NOT a fused channel (#54-rrf locked decision).
_IMPORTANCE_PIVOT = 3.0

# Expires decay factor for past-dated claims. ×0.5 after expiry.
_EXPIRED_DECAY = 0.5

# Contradiction penalty (#54-contradiction). When two notes carry
# different values for the same (subject, field) on different dates, the
# OLDER one takes this extra multiplier. Conservative default — a modest
# nudge, not an eviction. See ``apply_contradiction_penalty``.
_CONTRADICTION_PENALTY = 0.85

# Source-tier → post-fusion multiplier (gbrain-pattern adoption — see
# evaluations/GBRAIN-UNDERSTAND-ANYTHING-EVALUATION-2026-06-10.md in the
# umbrella repo). Tier encodes PROVENANCE QUALITY: 1 = primary (filings,
# transcripts, operator meeting notes), 2 = internal analysis / broker
# work, 3 = machine-scraped web/news intake. A POST-FUSION multiplier on
# the RRF score, NOT a fused channel (same locked shape as importance /
# expires, #54-rrf). Resolution (``resolve_source_tier``): explicit
# ``source_tier`` frontmatter wins; absent it, a sector-news intake note
# (``Sectors/<sector>/sources/…``) defaults to tier 3, everything else
# to tier 2 (×1.0) — an untagged, non-news vault scores byte-identically
# to pre-tier code. Deliberately modest (±15%) so tier nudges, never
# evicts; values are v1 knobs the operator A/B-tunes.
_TIER_MULTIPLIERS = {1: 1.15, 2: 1.0, 3: 0.85}
_TIER_DEFAULT = 2

# Graph channel (#45 / vault-graph). Seeds for the proximity ranking =
# the top-``_GRAPH_SEED_TOP_K`` candidates by the preliminary vector+fts
# RRF (plus token-anchor candidates — see ``_graph_channel``). Candidates
# are ranked by minimum undirected hop-distance to a seed, capped at
# ``_GRAPH_HOPS`` — beyond ~2 hops the relational signal is weak and the
# in-memory BFS stays cheap. Both are v1 tuning knobs (overridable per
# query) the operator A/B's before any default-on promotion.
_GRAPH_SEED_TOP_K = 3
_GRAPH_HOPS = 2

# Graph-EXPANSION channel (#45-expansion). Same seed selection as the
# proximity leg, but the seeds' ≤``_GRAPH_EXPAND_HOPS`` neighbours are
# INJECTED as new candidates (rather than re-ranking the existing set).
# Mirrors the proximity defaults but kept as SEPARATE knobs: expansion
# changes the candidate universe (noisier), so the operator A/B-tunes it
# independently of proximity before any default-on promotion. Both are
# overridable per query.
_GRAPH_EXPAND_SEED_TOP_K = 3
_GRAPH_EXPAND_HOPS = 2

# Blast-radius caps for BOTH graph legs (Codex SEV-2 — unbounded expansion).
# A hub vault (a node with thousands of links, an unbounded token-anchor set,
# or a deep hop request) could otherwise flood the candidate universe. Each
# cap is a generous safety ceiling — far above normal operation, so it only
# bites pathological inputs — and every truncation is deterministic so the
# top-``limit`` cut never varies run to run.
#
#   _GRAPH_MAX_HOPS        upper clamp on the per-query hop request (proximity
#                          AND expansion). Beyond ~2 hops the relational signal
#                          is weak and the BFS frontier grows fast.
#   _GRAPH_MAX_SEEDS       cap on the total seed set (prelim ∪ token-anchors).
#                          The token-anchor pass is otherwise unbounded (every
#                          candidate sharing a query token seeds).
#   _GRAPH_BFS_MAX_NODES   cap on the reachable-node set retained by the multi-
#                          source BFS (closest-first), bounding the universe
#                          both legs rank/inject over.
#   _GRAPH_EXPAND_MAX_INJECT  cap on candidates the expansion leg INJECTS
#                          (closest-first), bounding the candidate-set growth.
_GRAPH_MAX_HOPS = 3
_GRAPH_MAX_SEEDS = 32
_GRAPH_BFS_MAX_NODES = 512
_GRAPH_EXPAND_MAX_INJECT = 64


def importance_multiplier(importance: int) -> float:
    """Map a 1–5 importance to a post-fusion RRF multiplier.

    Neutral midpoint (3) → ×1.0; 5 → ×1.667; 1 → ×0.333. Linear in
    ``importance`` so the operator's manual weight scales the fused
    score predictably without re-tuning RRF itself.
    """
    return importance / _IMPORTANCE_PIVOT


def parse_source_tier(value: Any) -> int | None:
    """Coerce a frontmatter ``source_tier`` to 1–3, or ``None`` when unset.

    Mirrors ``parse_importance``'s tolerance: ints / numeric strings pass
    (clamped into 1–3); malformed values fall back to ``None`` (= no
    explicit tier, the path default applies) rather than raising.
    """
    if value is None or value == "":
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(1, min(3, n))


def resolve_source_tier(path: str, meta: dict[str, Any]) -> int:
    """Resolve a note's source tier: explicit frontmatter, else path default.

    The only path default is the sector-news intake
    (``Sectors/<sector>/sources/…`` — machine-scraped web/news) → tier 3;
    everything else → ``_TIER_DEFAULT`` (2, the ×1.0 neutral). Index paths
    are posix-normalised at index time (``index.py`` stores
    ``relative_to(vault_root).as_posix()``), so the forward-slash check is
    safe on Windows.
    """
    explicit = parse_source_tier(meta.get("source_tier"))
    if explicit is not None:
        return explicit
    # Exact-shape match: ``sources`` must be the THIRD path segment
    # (``Sectors/<sector>/sources/…`` — the sectornews intake convention).
    # A loose substring match would also catch e.g.
    # ``Sectors/<sector>/research/sources/…`` and silently down-weight
    # notes outside the news intake, breaking the "everything else is
    # tier 2" byte-identity contract (codex SEV-2, 2026-06-10 review).
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == "Sectors" and parts[2] == "sources":
        return 3
    return _TIER_DEFAULT


def source_tier_multiplier(tier: int) -> float:
    """Map a 1–3 source tier to its post-fusion RRF multiplier.

    Unknown tiers map to ×1.0 (neutral) — defensive only;
    ``resolve_source_tier`` always returns 1–3.
    """
    return _TIER_MULTIPLIERS.get(tier, 1.0)


@dataclass
class NoteHit:
    """One ranked note from a retrieval query.

    ``score`` is the final hybrid score (post-``expires`` decay) — the
    field consumers used to read for pure-cosine ranking now reads the
    hybrid final, so existing code paths keep working unchanged.

    The component fields below are additive. They explain how the final
    score decomposed; the API surfaces them per-result so the dashboard
    can later answer "why was this surfaced?" and operator debugging
    (``recall query --explain``) can print a per-result breakdown.

    ``best_chunk_text`` is populated when the hit came from the chunks
    table (Phase 2 retrieval) — it's the chunk that scored highest for
    this file. Empty when falling back to whole-file note embeddings or
    when the file only matched on FTS5 (no vector hit).
    """

    path: str
    score: float           # final RRF score, post importance/expires/tier/contradiction multipliers
    tldr: str
    body_excerpt: str
    metadata: dict[str, Any] = field(default_factory=dict)
    best_chunk_text: str = ""
    best_chunk_idx: int = -1
    # #54b component fields (all optional / additive). PRESERVED under
    # #54-rrf: ``vector_score`` / ``fts_score`` carry each channel's RAW
    # normalised score in [0,1] (cosine; FTS5 rank min-max normalised) so
    # the explainability contract (recall-query Iron Law, --explain) is
    # unchanged. ``importance`` / ``expires_decay`` are the post-fusion
    # multiplier inputs.
    vector_score: float | None = None
    fts_score: float | None = None
    importance: int | None = None
    expires_decay: float | None = None
    # #54-rrf fusion fields (additive). ``vector_rrf`` / ``fts_rrf`` are
    # the per-channel RRF contributions 1/(k+rank) that actually summed
    # into the fusion; ``rrf_score`` is their sum (pre multipliers).
    # ``contradiction_penalty`` is the #54-contradiction multiplier
    # (1.0 = no penalty applied).
    vector_rrf: float | None = None
    fts_rrf: float | None = None
    rrf_score: float | None = None
    contradiction_penalty: float | None = None
    # #45 graph channel (additive). ``graph_distance`` is the RAW minimum
    # undirected hop-distance from this note to a retrieval seed (0 = the
    # note IS a seed, 1 = a 1-hop neighbour, …) — the explainability signal,
    # sibling to ``vector_score`` / ``fts_score``. ``graph_rrf`` is the
    # per-channel RRF contribution ``1/(k+graph_rank)`` that summed into the
    # fusion, sibling to ``vector_rrf`` / ``fts_rrf``. BOTH None when the
    # graph channel was OFF (default) or the note was unreachable within the
    # hop cap — a None contributes nothing to ``rrf_score``, exactly like a
    # vector/FTS-absent doc.
    graph_distance: int | None = None
    graph_rrf: float | None = None
    # #45-expansion graph leg (additive). Populated ONLY on a note that the
    # opt-in expansion leg INJECTED as a NEW candidate (it was not in the
    # vector/FTS set). ``graph_expand_distance`` is the RAW min hop-distance
    # from the injected neighbour to a seed (≥1 — a seed / existing candidate
    # is never injected); ``graph_expand_rrf`` is the per-channel RRF
    # contribution ``1/(k + graph_expand_distance + 1)`` summed into the
    # fusion, sibling to ``graph_rrf``. BOTH None when the note was not
    # injected (expansion off — the default — or the note was already a
    # candidate / unreachable / filtered) → a None contributes nothing,
    # exactly like a vector/FTS/proximity-absent doc. Every injected neighbour
    # passed the SAME frontmatter/sensitivity gate as every candidate
    # (#no-mnpi-to-cloud — was cited as §5.4).
    graph_expand_distance: int | None = None
    graph_expand_rrf: float | None = None
    # #54-rerank — cross-encoder relevance score (bge-reranker-v2-m3),
    # populated only when the opt-in rerank stage ran AND the reranker was
    # available. ``None`` when rerank was off or the lib/model was absent
    # (graceful skip). When set, ``score`` reflects the rerank ordering.
    rerank_score: float | None = None
    # Source-tier provenance multiplier (gbrain-pattern adoption).
    # ``source_tier`` is the RESOLVED tier (explicit frontmatter, else the
    # path default — see ``resolve_source_tier``); ``tier_multiplier`` is
    # the post-fusion multiplier it mapped to. Always populated by
    # ``query()`` (tier 2 / ×1.0 neutral for untagged non-news notes);
    # ``None`` only on NoteHits built by older callers / tests that never
    # passed them — a None reads as "no tier measurement", sibling to the
    # other additive component fields.
    source_tier: int | None = None
    tier_multiplier: float | None = None


@dataclass
class Filter:
    """Frontmatter filter — applied before semantic ranking."""

    types: list[str] | None = None              # e.g. ["meeting-note", "company-profile"]
    sensitivity_max: str | None = None          # exclude anything more sensitive than this
    project: str | None = None                   # match `project: [[Projects/<X>]]` substring
    sectors: list[str] | None = None             # match any in `tags:` or `sector:`
    modified_after: str | None = None            # ISO date
    modified_before: str | None = None
    path_prefix: str | None = None               # e.g. "Companies/" / "Projects/DemoTarget/"
    exclude_path_prefix: str | None = None       # e.g. "Inbox/HiNotes/processed/"


# Order: less-sensitive < more-sensitive
_SENSITIVITY_ORDER = ["public", "internal", "confidential", "MNPI"]
# Case-insensitive lookup token → rank (0=public … 3=MNPI). Built once so a
# correctly-spelled-but-mis-cased value ("Confidential" / "mnpi") still resolves
# to the right rank instead of silently failing to the wrong default (F-33).
_SENSITIVITY_RANK = {s.lower(): i for i, s in enumerate(_SENSITIVITY_ORDER)}
_MOST_RESTRICTIVE_RANK = 0                       # public — the ceiling fail-closed value
_MOST_SENSITIVE_RANK = len(_SENSITIVITY_ORDER) - 1   # MNPI — the note fail-closed value


def query(
    query_text: str,
    *,
    vault_root: Path,
    client: OllamaClient,
    embed_model: str = "nomic-embed-text",
    filter_: Filter | None = None,
    limit: int = 15,
    db_path: Path | None = None,
    today: _dt.date | None = None,
    rerank: bool = False,
    rerank_top_k: int | None = None,
    graph: bool = False,
    graph_hops: int = _GRAPH_HOPS,
    graph_seed_top_k: int = _GRAPH_SEED_TOP_K,
    graph_expand: bool = False,
    graph_expand_hops: int = _GRAPH_EXPAND_HOPS,
    graph_expand_top_k: int = _GRAPH_EXPAND_SEED_TOP_K,
    graph_obj: Any = None,
) -> list[NoteHit]:
    """Retrieve top-`limit` notes for `query_text` after applying `filter_`.

    Scoring uses #54-rrf Reciprocal Rank Fusion + the post-fusion
    importance/expires/source-tier/contradiction multipliers. ``today``
    overrides the date used for ``expires`` decay (test seam — production
    callers omit it and we read ``date.today()``).

    ``rerank`` (default OFF) opts into the #54-rerank cross-encoder
    second stage: after RRF fusion + multipliers, the top ``rerank_top_k``
    (default = ``limit``) fused hits are re-ordered by a local
    ``bge-reranker-v2-m3`` cross-encoder. The reranker lib + model are an
    OPTIONAL dependency (``pip install -e .[recall]``); if absent, the
    stage LOGS and SKIPS gracefully (fused RRF order is returned
    unchanged). Opt-in per query because it adds ~1-3s latency — off for
    latency-sensitive paths.

    ``graph`` (default OFF) opts into the #45 graph leg — a 3rd RRF channel
    that ranks the EXISTING candidate set by graph-proximity to retrieval
    seeds (see ``_graph_channel`` + the module docstring). ``graph_hops``
    (default 2) is the proximity cap; ``graph_seed_top_k`` (default 3) is
    how many top vector+fts candidates seed the proximity ranking;
    ``graph_obj`` injects a pre-built ``networkx`` graph (test / caching
    seam) instead of rebuilding from ``vault_root``. When ``graph`` is OFF
    the leg never runs, never imports ``networkx``, and the fusion is byte-
    identical to the two-leg pipeline. The leg is best-effort: a graph
    build/query failure LOGS and degrades to vector+fts, never raising.

    ``graph_expand`` (default OFF, INDEPENDENT of ``graph``) opts into the
    #45-expansion leg — a 4th RRF channel that INJECTS the seeds' ≤``
    graph_expand_hops`` graph-neighbours as NEW candidates (who-touched-style
    relational recall) rather than re-ranking the existing set. ``
    graph_expand_top_k`` (default 3) is how many top vector+fts candidates
    seed the expansion. An injected neighbour is added ONLY when it is not
    already a candidate AND it passed the SAME frontmatter/sensitivity gate as
    every candidate (it is present in the filtered ``notes_by_path``) — a
    more-sensitive note is NEVER smuggled past the gate via expansion
    (#no-mnpi-to-cloud — was cited as §5.4).
    Like the proximity leg it is additive + OFF-by-default (candidate universe
    UNCHANGED, ``networkx`` never imported, /recall byte-identical when off)
    and best-effort (a graph fault degrades to the non-expanded result). See
    ``_graph_expansion_channel``.
    """
    if db_path is None:
        db_path = vault_root / DEFAULT_INDEX_DIR / DEFAULT_INDEX_DB

    if not db_path.exists():
        raise FileNotFoundError(
            f"Index not found at {db_path}. Run `recall index` first."
        )

    # 1. Embed the query
    qvec = client.embed(model=embed_model, text=query_text)
    qmag = _magnitude(qvec)
    if qmag == 0:
        return []

    oversample = max(limit * _OVERSAMPLE, limit)
    conn = sqlite3.connect(str(db_path))

    # 2. Build a path -> (meta, tldr, body_excerpt) lookup, pre-filtered.
    #    The same filter applies to both lanes — FTS5 hits whose paths
    #    aren't in ``notes_by_path`` are dropped during the merge.
    note_rows = conn.execute(
        "SELECT path, frontmatter_json, tldr, body_excerpt, modified_at, embedding FROM notes"
    ).fetchall()
    notes_by_path: dict[str, dict[str, Any]] = {}
    note_fallback_embeddings: dict[str, list[float]] = {}
    for path, fm_json, tldr, body_excerpt, modified_at, emb_blob in note_rows:
        try:
            meta = json.loads(fm_json) if fm_json else {}
        except json.JSONDecodeError:
            meta = {}
        if filter_ is not None and not _passes_filter(path, meta, modified_at, filter_):
            continue
        notes_by_path[path] = {
            "meta": meta,
            "tldr": tldr or "",
            "body_excerpt": body_excerpt or "",
        }
        if emb_blob:
            note_fallback_embeddings[path] = unpack_embedding(emb_blob)

    # 3. Vector lane — chunk-level cosine ranking (Phase 2). Fall back
    #    to whole-file embedding when the chunks table is empty (legacy
    #    index). Keep best chunk per path; oversample to ``limit × 3``.
    has_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0

    best_by_path: dict[str, tuple[float, int, str]] = {}  # path -> (score, chunk_idx, chunk_text)

    if has_chunks:
        for cpath, chunk_idx, chunk_text, emb_blob in conn.execute(
            "SELECT path, chunk_idx, chunk_text, embedding FROM chunks"
        ):
            if cpath not in notes_by_path:
                continue
            emb = unpack_embedding(emb_blob)
            emag = _magnitude(emb)
            if emag == 0:
                continue
            dot = sum(a * b for a, b in zip(qvec, emb))
            score = dot / (qmag * emag)
            current = best_by_path.get(cpath)
            if current is None or score > current[0]:
                best_by_path[cpath] = (score, chunk_idx, chunk_text)
    else:
        for path, emb in note_fallback_embeddings.items():
            emag = _magnitude(emb)
            if emag == 0:
                continue
            dot = sum(a * b for a, b in zip(qvec, emb))
            score = dot / (qmag * emag)
            best_by_path[path] = (score, -1, "")

    # Trim vector lane to oversample × limit (keep best chunks per path).
    # ``vector_sorted`` is best-first, so its index IS the channel rank.
    vector_sorted = sorted(best_by_path.items(), key=lambda kv: kv[1][0], reverse=True)
    vector_top = dict(vector_sorted[:oversample])
    # path -> 1-based rank within the vector channel (best = 1).
    vector_rank = {path: i + 1 for i, (path, _) in enumerate(vector_sorted[:oversample])}

    # 4. FTS5 lane — BM25-ranked keyword match. Degrades gracefully when
    #    the sidecar table is absent (older index) or empty (just-built
    #    index awaiting --rebuild).
    #
    #    ``fts_top`` keeps the RAW normalised score in [0, 1] for the
    #    explainability surface (``fts_score`` on the NoteHit). ``fts_rank``
    #    keeps the 1-based channel rank — that's what RRF actually fuses.
    fts_top: dict[str, float] = {}
    fts_rank: dict[str, int] = {}
    fts_query = _fts_query_escape(query_text)
    if fts_query and _fts_table_present(conn):
        try:
            fts_rows = conn.execute(
                "SELECT path, rank FROM recall_fts "
                "WHERE recall_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, oversample),
            ).fetchall()
        except sqlite3.OperationalError as e:
            # FTS5 syntax / availability issue — log + degrade to vector-only.
            logger.warning("FTS5 query failed (%s); falling back to vector-only", e)
            fts_rows = []
        # Filter to the candidate set (same frontmatter filter applies).
        fts_rows = [(p, r) for p, r in fts_rows if p in notes_by_path]
        if fts_rows:
            # ``fts_rows`` is already ORDER BY rank (best first), so its
            # index is the channel rank.
            for i, (path, _rk) in enumerate(fts_rows):
                fts_rank[path] = i + 1
            ranks = [r for _, r in fts_rows]
            rmin, rmax = min(ranks), max(ranks)
            span = rmax - rmin
            for path, rk in fts_rows:
                if span > 0:
                    # FTS5 rank is negative; lower (more negative) = better.
                    # Map best → 1.0, worst → 0.0 (raw explainability score).
                    fts_top[path] = (rmax - rk) / span
                else:
                    fts_top[path] = 1.0

    conn.close()

    # 5. Merge by path → fuse with Reciprocal Rank Fusion (RRF).
    #
    #    rrf(doc) = Σ_channel 1 / (k + rank_channel(doc))
    #
    #    A channel contributes ``1/(k+rank)`` only when the doc appears in
    #    that channel; absence contributes nothing. The fusion sums one
    #    ``1/(k+rank)`` term per channel the doc appears in — vector, fts,
    #    and (when ``graph`` is on) the #45 graph leg appended below.
    today_iso = (today or _dt.date.today()).isoformat()
    candidate_paths = set(vector_top.keys()) | set(fts_top.keys())
    # #no-mnpi-to-cloud (was cited as §5.4) — pin the candidate/seed universe to
    # gate-passing notes ONLY for the graph legs. Both lanes already drop
    # non-``notes_by_path`` paths, so this is
    # a content NO-OP — but applying it only when a graph leg runs keeps the
    # graph-off ``candidate_paths`` set OBJECT byte-identical to today (no
    # intersection rebuild that could perturb equal-score tie iteration on the
    # off path) — Codex SEV-1 byte-identity. With the leg on it provably seeds /
    # ranks / traverses a gate-passing universe (a filtered note can't re-seed).
    if graph or graph_expand:
        candidate_paths &= set(notes_by_path)

    # 5a. Build the vault graph ONCE, shared by both graph legs (Codex SEV-3 —
    #     the proximity + expansion legs would otherwise each rebuild it when
    #     both are on and no ``graph_obj`` was injected). Lazy + gated on the
    #     on-path so the default (both legs off) NEVER imports ``networkx`` and
    #     /recall stays byte-identical. Best-effort: a build fault disables BOTH
    #     legs (degrade to vector+fts) rather than raising.
    if graph_obj is None and (graph or graph_expand):
        try:
            from routines.vault_graph.graph import build_from_vault

            graph_obj = build_from_vault(vault_root)
        except Exception:  # noqa: BLE001 — both graph legs are additive + best-effort
            logger.warning(
                "graph build failed; degrading to vector+fts (graph legs off)",
                exc_info=True,
            )
            graph_obj = None
            graph = False
            graph_expand = False

    # 5b. Graph channel (#45) — OPT-IN 3rd RRF leg. ``graph_rank`` maps a
    #     candidate path → its 1-based graph rank (= hop-distance-to-seed + 1)
    #     and is EMPTY when the channel is off → the fusion below is then the
    #     byte-identical two-leg sum. The leg re-ranks only EXISTING
    #     candidates (no candidate-universe change) and is best-effort: any
    #     graph build/query fault degrades to vector+fts rather than raising.
    #     NOTE: the proximity leg always ranks the ORIGINAL vector+fts
    #     candidate set (computed BEFORE any expansion injection below), so
    #     enabling ``graph_expand`` never perturbs the proximity ranking.
    graph_rank: dict[str, int] = {}
    if graph:
        try:
            graph_rank = _graph_channel(
                candidate_paths=candidate_paths,
                vector_rank=vector_rank,
                fts_rank=fts_rank,
                query_text=query_text,
                notes_by_path=notes_by_path,
                vault_root=vault_root,
                graph_obj=graph_obj,
                hops=graph_hops,
                seed_top_k=graph_seed_top_k,
            )
        except Exception:  # noqa: BLE001 — the graph leg is additive + best-effort
            logger.warning(
                "graph channel failed; degrading to vector+fts", exc_info=True
            )
            graph_rank = {}

    # 5c. Graph-EXPANSION channel (#45-expansion) — OPT-IN 4th RRF leg,
    #     INDEPENDENT of the proximity leg. Returns ``path -> expand_rank`` for
    #     graph-neighbours of the seeds that are NOT already candidates AND that
    #     passed the candidate gate (present in ``notes_by_path`` — the
    #     #no-mnpi-to-cloud sensitivity guarantee, was cited as §5.4). EMPTY
    #     when the flag is off → no new candidates,
    #     no ``networkx`` import, byte-identical fusion. Best-effort: a fault
    #     degrades to the non-expanded result. The injected paths are unioned
    #     into ``candidate_paths`` AFTER the proximity leg ran (above) so
    #     proximity stays computed over the original set only.
    graph_expand_rank: dict[str, int] = {}
    if graph_expand:
        try:
            graph_expand_rank = _graph_expansion_channel(
                candidate_paths=candidate_paths,
                vector_rank=vector_rank,
                fts_rank=fts_rank,
                query_text=query_text,
                notes_by_path=notes_by_path,
                vault_root=vault_root,
                graph_obj=graph_obj,
                hops=graph_expand_hops,
                seed_top_k=graph_expand_top_k,
            )
        except Exception:  # noqa: BLE001 — the expansion leg is additive + best-effort
            logger.warning(
                "graph expansion channel failed; degrading to non-expanded result",
                exc_info=True,
            )
            graph_expand_rank = {}
        # Inject the surviving neighbours as NEW candidates for the scoring loop.
        candidate_paths = candidate_paths | set(graph_expand_rank.keys())

    results: list[NoteHit] = []
    for path in candidate_paths:
        n = notes_by_path.get(path)
        if n is None:
            # Defensive — fts row pointed at a path the notes scan rejected.
            continue
        meta = n["meta"]

        # Vector — clamp cosine to [0, 1] for the raw explainability score.
        v_raw, chunk_idx, chunk_text = vector_top.get(path, (0.0, -1, ""))
        v = max(0.0, min(1.0, float(v_raw)))

        # FTS — already normalised. Missing = 0 (raw explainability score).
        f = float(fts_top.get(path, 0.0))

        # --- RRF fusion: sum 1/(k+rank) over channels the doc appears in.
        v_rank = vector_rank.get(path)
        f_rank = fts_rank.get(path)
        g_rank = graph_rank.get(path)  # 1-based graph rank (= distance + 1)
        ge_rank = graph_expand_rank.get(path)  # 1-based expansion rank (= distance + 1)
        v_rrf = (1.0 / (_RRF_K + v_rank)) if v_rank is not None else 0.0
        f_rrf = (1.0 / (_RRF_K + f_rank)) if f_rank is not None else 0.0
        g_rrf = (1.0 / (_RRF_K + g_rank)) if g_rank is not None else 0.0
        ge_rrf = (1.0 / (_RRF_K + ge_rank)) if ge_rank is not None else 0.0
        rrf = v_rrf + f_rrf + g_rrf + ge_rrf

        # Importance — from frontmatter (single source; FTS5 row mirrors it).
        importance = parse_importance(meta.get("importance"))

        # 6. Post-fusion multipliers: importance × expires decay × source
        #    tier. (Contradiction penalty is a separate pass below — it
        #    needs the whole candidate set to detect same-subject/field
        #    disagreement.)
        expires_iso = parse_expires_iso(meta.get("expires"))
        if expires_iso and expires_iso < today_iso:
            decay = _EXPIRED_DECAY
        else:
            decay = 1.0

        tier = resolve_source_tier(path, meta)
        tier_mult = source_tier_multiplier(tier)

        final = rrf * importance_multiplier(importance) * decay * tier_mult

        results.append(NoteHit(
            path=path,
            score=final,
            tldr=n["tldr"],
            body_excerpt=n["body_excerpt"],
            metadata=meta,
            best_chunk_text=chunk_text,
            best_chunk_idx=chunk_idx,
            # None (NOT 0.0) when the doc never appeared in the vector channel:
            # a real "no vector measurement", distinct from "cosine 0.0". This
            # lets synthesise's cosine floor skip FTS-only hits instead of
            # dropping them as below-threshold (Codex SEV-2).
            vector_score=(v if v_rank is not None else None),
            fts_score=f,
            importance=importance,
            expires_decay=decay,
            source_tier=tier,
            tier_multiplier=tier_mult,
            vector_rrf=v_rrf,
            fts_rrf=f_rrf,
            # None (NOT 0.0) when the doc never entered the graph channel —
            # a real "no graph measurement" (channel off, or unreachable
            # within the hop cap), distinct from a measured distance.
            graph_distance=((g_rank - 1) if g_rank is not None else None),
            graph_rrf=(g_rrf if g_rank is not None else None),
            # None (NOT 0.0) unless the expansion leg INJECTED this note as a
            # new candidate — a real "graph-expansion measurement", distinct
            # from a note that was already a candidate (ge_rank None → no leg).
            graph_expand_distance=((ge_rank - 1) if ge_rank is not None else None),
            graph_expand_rrf=(ge_rrf if ge_rank is not None else None),
            rrf_score=rrf,
            contradiction_penalty=1.0,
        ))

    # 6b. Contradiction-detection penalty (#54-contradiction). Modest decay
    #     on the OLDER of two same-(subject, field) facts that disagree.
    #     Mutates the ``score`` + ``contradiction_penalty`` in place.
    apply_contradiction_penalty(results)

    # 7. Sort by final score DESC.
    #
    #    The graph legs introduce equal-score ties (several equidistant injected
    #    neighbours share an identical ``1/(k+rank)``; the proximity leg gives
    #    equidistant candidates identical boosts), so when EITHER leg ran we add
    #    an explicit ``path`` ASC tiebreak — the candidate set iterates in
    #    str-hash-randomised order, so a score-only key would leave the
    #    top-``limit`` cut varying run to run (Codex SEV-2 — tie determinism).
    #
    #    We gate the tiebreak on whether a graph leg ACTUALLY contributed ranks
    #    (``graph_rank``/``graph_expand_rank`` non-empty), NOT merely on the flag:
    #    a graph query that DEGRADED to no contribution (build/query fault) then
    #    sorts EXACTLY like graph-off (score-only ``reverse=True``), so the fault
    #    path is ordering-identical to the off path and graph-off /recall stays
    #    byte-identical to today (Codex SEV-1 byte-identity, #no-mnpi-to-cloud
    #    (was cited as §5.4) off-path guarantee). Both ranks are empty when the
    #    legs are off OR degraded.
    if graph_rank or graph_expand_rank:
        results.sort(key=lambda h: (-h.score, h.path))
    else:
        results.sort(key=lambda h: h.score, reverse=True)

    # 8. #54-rerank — OPTIONAL cross-encoder second stage over the fused
    #    top-k. Off by default; graceful no-op when the reranker lib/model
    #    is absent. Lazy-imported so the hot path never pays the import.
    if rerank:
        from routines.recall.rerank import rerank_hits

        top_k = rerank_top_k if rerank_top_k is not None else limit
        results = rerank_hits(query_text, results, top_k=top_k)

    # 9. Truncate to limit.
    return results[:limit]


# ============================================================ contradiction


def _claim_date(meta: dict[str, Any]) -> str:
    """The date a note's claim is anchored to, as a YYYY-MM-DD string.

    Used only by the contradiction detector to decide which of two
    disagreeing facts is *older*. Preference order:

      1. ``asof`` / ``as_of`` — explicit "this value is true as of <date>"
         (the operator's deliberate dated-claim marker).
      2. ``date`` — generic note date (meeting-notes, daily logs).
      3. ``expires`` — last resort; an expiry implies the claim was made
         before it.

    Returns "" when no usable date is present (the note then can't be
    ordered against another and is left untouched).
    """
    for key in ("asof", "as_of", "date"):
        iso = parse_expires_iso(meta.get(key))  # same YYYY-MM-DD coercion
        if iso:
            return iso
    return parse_expires_iso(meta.get("expires"))


def apply_contradiction_penalty(
    hits: list[NoteHit],
    *,
    penalty: float = _CONTRADICTION_PENALTY,
) -> None:
    """#54-contradiction — modest decay on the OLDER of two disagreeing facts.

    NARROW + CONSERVATIVE by design. The general "same subject + field,
    different value" contradiction detector depends on #41's structured
    ``memory_kind`` labelling (only ~50% done) — see
    ``CONTRADICTION-NOTES.md``. Until that lands, the recall index is
    note-level with no structured (subject, field, value) decomposition,
    so this detector fires ONLY on notes the operator has *deliberately*
    marked as discrete dated claims via three opt-in frontmatter fields:

        subject:  <entity the claim is about>   e.g. "Companies/Acme"
        field:    <attribute>                   e.g. "ev_ebitda_multiple"
        value:    <the asserted value>          e.g. "11.5x"

    plus a claim date (``asof`` / ``as_of`` / ``date`` / ``expires``).

    Rule: within the candidate set, group by (subject, field). If a group
    holds ≥2 notes whose ``value`` differs AND whose claim dates differ,
    every note in that group EXCEPT the most-recently-dated one takes the
    ``penalty`` multiplier (default 0.85) on its final score. The newest
    value wins; stale contradicted values decay but are NOT evicted —
    recall is read-only (CLAUDE.md §5.7), eviction stays operator-gated.

    No-op (zero behavioural change) when notes lack these fields — i.e.
    on every note in the vault today. The detector is forward-compatible:
    the moment the operator (or a future #41 labelling pass) starts
    emitting ``subject``/``field``/``value``, it activates with no code
    change. Mutates ``hits`` in place (sets ``contradiction_penalty`` and
    re-scales ``score``).
    """
    # Bucket candidates that carry the full opt-in claim triple + a date.
    groups: dict[tuple[str, str], list[NoteHit]] = {}
    for h in hits:
        meta = h.metadata or {}
        subject = str(meta.get("subject", "")).strip()
        fieldname = str(meta.get("field", "")).strip()
        value = meta.get("value")
        date = _claim_date(meta)
        # Require subject + field + a value + a usable date to be a "claim".
        if not subject or not fieldname or value is None or not date:
            continue
        groups.setdefault((subject, fieldname), []).append(h)

    for members in groups.values():
        if len(members) < 2:
            continue
        # Distinct asserted values AND distinct dates are both required for
        # a genuine contradiction (same value = agreement; same date =
        # can't order them, so don't penalise).
        values = {str((m.metadata or {}).get("value")).strip() for m in members}
        dates = {_claim_date(m.metadata or {}) for m in members}
        if len(values) < 2 or len(dates) < 2:
            continue
        # The winner is the most-recently-dated claim. Everyone older with a
        # *different* value takes the penalty.
        newest_date = max(_claim_date(m.metadata or {}) for m in members)
        newest_value = next(
            str((m.metadata or {}).get("value")).strip()
            for m in members
            if _claim_date(m.metadata or {}) == newest_date
        )
        for m in members:
            m_value = str((m.metadata or {}).get("value")).strip()
            if _claim_date(m.metadata or {}) < newest_date and m_value != newest_value:
                m.contradiction_penalty = penalty
                m.score *= penalty


# ============================================================ graph channel


def _node_of_path(path: str) -> str:
    """Graph node id for a candidate path.

    Candidate paths are canonical vault-relative paths WITH ``.md``; the graph
    node id is the same path WITHOUT ``.md`` (parser convention), so a plain
    suffix strip is the exact id — no fuzzy resolve needed.
    """
    return path[:-3] if path.endswith(".md") else path


def _path_of_node(node: str) -> str:
    """Candidate path for a graph node id — the EXACT inverse of ``_node_of_path``.

    Graph node ids for real notes are vault-relative paths WITHOUT ``.md``
    (parser convention); candidate paths / ``notes_by_path`` keys carry the
    ``.md`` suffix. Since ``_node_of_path(p) = p[:-3]`` strips exactly one
    ``.md``, the inverse ALWAYS appends exactly one ``.md`` — so the pair is a
    strict bijection on the ``.md`` domain (``"A/Foo" → "A/Foo.md"``;
    ``"A/Foo.md" → "A/Foo.md.md"`` round-trips ``"A/Foo.md.md"`` too).

    Appending UNCONDITIONALLY (not "skip when the node already ends in ``.md``")
    is the gate-integrity fix (Codex r3 SEV-1): a real-note node is extensionless,
    so the only nodes ending in ``.md`` are pathological dangling artifacts (e.g. a
    ``[[A/Foo.md]]`` wikilink target). The old conditional mapped such a node to
    ``"A/Foo.md"`` verbatim, which could ALIAS onto a DIFFERENT, gate-passing real
    note ``A/Foo.md`` and inject a note that was never the reached neighbour.
    Always appending sends it to ``"A/Foo.md.md"`` instead — absent from
    ``notes_by_path`` (no real ``Foo.md.md`` file) → not injected. The mapping can
    no longer mis-identify the reached node as a different allowed note.
    """
    return node + ".md"


def _gate_passing_subgraph(g: Any, notes_by_path: dict[str, dict[str, Any]]) -> Any:
    """Restrict ``g`` to ONLY the gate-passing notes — the #no-mnpi-to-cloud
    absolute (was cited as §5.4; Codex SEV-1).

    The vault graph is built over the WHOLE vault (sensitivity-blind), so a
    confidential/MNPI note the frontmatter/sensitivity gate dropped is still a
    node — and the OLD code traversed the full graph, gating only the injected
    *endpoint*. That let a path ``Allowed → hidden → Allowed`` boost/inject the
    far note THROUGH the hidden hop, and a filtered note could even be reached
    as a seed's neighbour. Inducing the subgraph on the gate-passing node set
    (``notes_by_path``) removes every hidden node BEFORE any BFS, so a hidden
    note can never be a seed, an intermediate hop, or a ranked/injected result —
    the only paths that survive are wholly within the gate-passing universe.

    Returns a read-only node-induced subgraph VIEW (no copy, ``vault_graph``
    stays untouched). ``g.subgraph`` silently ignores allowed ids absent from
    ``g`` (notes with no wikilink node), so the view is exactly
    ``allowed_nodes ∩ g.nodes``.
    """
    allowed_nodes = {_node_of_path(p) for p in notes_by_path}
    return g.subgraph(allowed_nodes)


def _select_seed_paths(
    *,
    candidate_paths: set[str],
    vector_rank: dict[str, int],
    fts_rank: dict[str, int],
    query_text: str,
    notes_by_path: dict[str, dict[str, Any]],
    seed_top_k: int,
) -> set[str]:
    """Shared #45 retrieval-seed selection (the proximity AND expansion legs).

        seeds = the top-``seed_top_k`` candidates by the preliminary
                vector+fts RRF (the docs the lexical/semantic legs are most
                confident about)
                ∪  any candidate whose path-stem / frontmatter title shares
                   an exact query token (the "the query *is* an entity"
                   structural anchor — e.g. query "DemoTelco" anchors on
                   ``Companies/DemoTelco Group plc``).

    Deterministic: break preliminary-RRF ties on the path string — ``sorted``
    over a ``set`` is otherwise at the mercy of str-hash-randomised iteration
    order, so tied candidates could seed differently across processes (the rest
    of recall + the graph queries are deterministic; this stays so). ``max(0,
    …)`` guards a negative override from slicing off the tail instead of
    selecting no prelim seeds. Every returned seed is ⊆ ``candidate_paths`` by
    construction.

    Total seeds are capped at ``_GRAPH_MAX_SEEDS`` (Codex SEV-2 — the
    token-anchor pass is otherwise unbounded: every candidate sharing a query
    token would seed, and a multi-source BFS from thousands of seeds floods a
    hub vault). The cap keeps prelim seeds first (highest-confidence) then the
    lowest-path token anchors, so the truncation is deterministic.
    """
    # (a) top-``seed_top_k`` candidates by the preliminary vector+fts RRF.
    def _prelim(path: str) -> float:
        vr = vector_rank.get(path)
        fr = fts_rank.get(path)
        return (
            (1.0 / (_RRF_K + vr) if vr is not None else 0.0)
            + (1.0 / (_RRF_K + fr) if fr is not None else 0.0)
        )

    by_prelim = sorted(candidate_paths, key=lambda p: (-_prelim(p), p))
    prelim_seeds = by_prelim[: max(0, seed_top_k)]

    # (b) token-anchor seeds — a candidate whose NAME (path stem or
    #     frontmatter title) carries an exact query token. Iterate in sorted
    #     path order so the SEV-2 seed cap below truncates deterministically.
    q_tokens = {t.lower() for t in _FTS_TOKEN_RE.findall(query_text or "")}
    anchor_seeds: list[str] = []
    if q_tokens:
        for path in sorted(candidate_paths):
            stem = path.rsplit("/", 1)[-1]
            if stem.endswith(".md"):
                stem = stem[:-3]
            meta = (notes_by_path.get(path) or {}).get("meta") or {}
            name = f"{stem} {meta.get('title', '')}"
            name_tokens = {t.lower() for t in _FTS_TOKEN_RE.findall(name)}
            if name_tokens & q_tokens:
                anchor_seeds.append(path)
                # Stop accumulating once we can fill the cap from anchors alone
                # (prelim seeds are prepended below + the final cap keeps the
                # first ``_GRAPH_MAX_SEEDS``), so a hub vault never builds an
                # unbounded anchor list (Codex SEV-2). Identical result to
                # accumulating all then truncating — just bounded work.
                if len(anchor_seeds) >= _GRAPH_MAX_SEEDS:
                    break

    # Deterministic union (prelim first, then anchors), capped at
    # ``_GRAPH_MAX_SEEDS`` to bound the BFS fan-out (Codex SEV-2).
    seed_paths: set[str] = set()
    ordered: list[str] = []
    for path in (*prelim_seeds, *anchor_seeds):
        if path not in seed_paths:
            seed_paths.add(path)
            ordered.append(path)
    if len(ordered) > _GRAPH_MAX_SEEDS:
        logger.debug(
            "graph seed set capped: %d → %d (hub vault / broad query)",
            len(ordered), _GRAPH_MAX_SEEDS,
        )
    return set(ordered[:_GRAPH_MAX_SEEDS])


def _seed_min_distances(
    g: Any,
    seed_nodes: list[str],
    hops: int,
    *,
    max_nodes: int = _GRAPH_BFS_MAX_NODES,
) -> dict[str, int]:
    """Min undirected hop-distance from ANY seed to every node reachable within
    ``hops`` (the seed itself sits at distance 0).

    ``who_touched`` traverses the directed graph UNDIRECTED (queries.py), so
    backlink-reachable neighbours count and distances are symmetric — a Sector
    reached *from* the Projects that link to it is 1 hop, not ∞. Lazy-imports
    ``who_touched`` so neither graph leg's off-path ever pays the import (and so
    the fault-degradation tests can monkeypatch ``queries.who_touched``).

    ``hops`` is clamped to ``[0, _GRAPH_MAX_HOPS]``. Each per-seed walk is
    FRONTIER-BOUNDED at ``max_nodes`` *during traversal* (``who_touched(...,
    max_nodes=…)`` stops expanding a hub rather than walking it to completion),
    and the merged multi-source set is trimmed to the closest ``max_nodes``
    afterwards as a backstop — together a bounded multi-source BFS (Codex
    SEV-2). Seeds (distance 0) sort first, so they always survive the trim.
    """
    from routines.vault_graph.queries import who_touched

    hops = max(0, min(hops, _GRAPH_MAX_HOPS))

    dist: dict[str, int] = {}

    def _relax(node: str, d: int) -> None:
        cur = dist.get(node)
        if cur is None or d < cur:
            dist[node] = d

    for seed in seed_nodes:
        _relax(seed, 0)  # who_touched excludes the topic, so seed the 0 here
        # Frontier cap DURING the walk (not just post-hoc) so a hub seed can't
        # blow up the traversal itself (Codex SEV-2).
        for tr in who_touched(g, seed, hops=hops, max_nodes=max_nodes):
            _relax(tr.node, tr.distance)

    # Blast-radius cap (Codex SEV-2): keep only the closest ``max_nodes`` nodes,
    # deterministically by ``(distance, node)``, so a hub frontier can't explode
    # the candidate universe. No-op on a normal vault (frontier ≪ cap). Logged
    # (not silent) so an operator can see a hub vault hit the bound.
    if len(dist) > max_nodes:
        logger.debug(
            "graph BFS frontier capped: %d reachable → closest %d (hub vault)",
            len(dist), max_nodes,
        )
        dist = dict(
            sorted(dist.items(), key=lambda kv: (kv[1], kv[0]))[:max_nodes]
        )
    return dist


def _graph_channel(
    *,
    candidate_paths: set[str],
    vector_rank: dict[str, int],
    fts_rank: dict[str, int],
    query_text: str,
    notes_by_path: dict[str, dict[str, Any]],
    vault_root: Path,
    graph_obj: Any,
    hops: int,
    seed_top_k: int,
) -> dict[str, int]:
    """#45 — graph-proximity-to-seed leg. Returns ``path -> graph_rank``.

    Ranks the EXISTING candidate set (never expands it) by minimum
    undirected hop-distance to a retrieval *seed* (see ``_select_seed_paths``).

    ``graph_rank = min_distance_to_any_seed + 1`` (the seed itself sits at
    distance 0 → rank 1; its 1-hop neighbours → rank 2; …). Dense-by-
    distance, so every equidistant candidate gets an identical leg
    contribution — no arbitrary alphabetical tiebreak. Candidates absent
    from the graph, or further than ``hops`` from every seed, get NO entry
    (the caller treats a missing rank as a 0 contribution, exactly like a
    vector/FTS-absent doc).

    Reads the vault graph through the #45 public surface
    (``build_from_vault`` + ``who_touched``) — ``vault_graph`` stays
    read-only. Lazy-imported so the default (graph-off) hot path never pays
    the ``networkx`` import.

    #no-mnpi-to-cloud (was cited as §5.4) — traversal is restricted to the
    gate-passing universe (``_gate_passing_subgraph``) BEFORE any BFS, so a
    confidential/MNPI-hidden note can never be a seed, an intermediate hop, OR
    boost a candidate it sits between (Codex SEV-1).
    """
    from routines.vault_graph.graph import build_from_vault

    g = graph_obj if graph_obj is not None else build_from_vault(vault_root)
    g = _gate_passing_subgraph(g, notes_by_path)

    seed_paths = _select_seed_paths(
        candidate_paths=candidate_paths,
        vector_rank=vector_rank,
        fts_rank=fts_rank,
        query_text=query_text,
        notes_by_path=notes_by_path,
        seed_top_k=seed_top_k,
    )
    # ``sorted`` for a process-stable seed order. The final ``dist`` is already
    # order-independent (min over seeds is commutative), but a deterministic
    # seed list keeps the leg reproducible should a future cap / short-circuit /
    # order-dependent log ever make seed order observable.
    seed_nodes = [n for n in (_node_of_path(p) for p in sorted(seed_paths)) if n in g]
    if not seed_nodes:
        return {}

    dist = _seed_min_distances(g, seed_nodes, hops)

    # --- rank EXISTING candidates by proximity (graph_rank = dist + 1) ---
    graph_rank: dict[str, int] = {}
    for path in candidate_paths:
        d = dist.get(_node_of_path(path))
        if d is not None:
            graph_rank[path] = d + 1
    return graph_rank


def _graph_expansion_channel(
    *,
    candidate_paths: set[str],
    vector_rank: dict[str, int],
    fts_rank: dict[str, int],
    query_text: str,
    notes_by_path: dict[str, dict[str, Any]],
    vault_root: Path,
    graph_obj: Any,
    hops: int,
    seed_top_k: int,
) -> dict[str, int]:
    """#45-expansion — INJECT seed neighbours as NEW candidates. Returns
    ``path -> expand_rank`` for every graph-neighbour of a seed that is BOTH:

      * NOT already a candidate — an existing candidate is scored on its own
        legs (and re-ranked by the proximity leg when ``graph`` is on);
        expansion only ADDS to the candidate universe, and
      * present in ``notes_by_path`` — i.e. it passed the SAME frontmatter /
        sensitivity gate as every candidate. This is the #no-mnpi-to-cloud
        guarantee (was cited as §5.4): the graph is built over the WHOLE vault
        (sensitivity-blind), so a neighbour node may map to a more-sensitive
        note; gating on ``notes_by_path`` membership ensures such a note is
        NEVER smuggled past the gate via expansion.

    ``expand_rank = distance + 1`` (a 1-hop neighbour → distance 1 → rank 2 →
    ``1/(k+2)``). The seed sits at distance 0 but is always an existing
    candidate, so it is never injected → an injected node is always at distance
    ≥1.

    Seed selection is shared with the proximity leg (``_select_seed_paths``).
    Reads the vault graph through the #45 read-only surface
    (``build_from_vault`` + ``who_touched``); lazy-imported so the default
    (expansion-off) hot path never pays the ``networkx`` import.

    #no-mnpi-to-cloud (was cited as §5.4) — traversal is restricted to the
    gate-passing universe (``_gate_passing_subgraph``) BEFORE any BFS, so a
    confidential/MNPI-hidden note can never be a seed, an intermediate hop, or
    be reached for injection THROUGH a hidden hop (Codex SEV-1). The
    per-endpoint ``notes_by_path`` check below is kept as belt-and-suspenders.
    """
    from routines.vault_graph.graph import build_from_vault

    g = graph_obj if graph_obj is not None else build_from_vault(vault_root)
    g = _gate_passing_subgraph(g, notes_by_path)

    seed_paths = _select_seed_paths(
        candidate_paths=candidate_paths,
        vector_rank=vector_rank,
        fts_rank=fts_rank,
        query_text=query_text,
        notes_by_path=notes_by_path,
        seed_top_k=seed_top_k,
    )
    seed_nodes = [n for n in (_node_of_path(p) for p in sorted(seed_paths)) if n in g]
    if not seed_nodes:
        return {}

    dist = _seed_min_distances(g, seed_nodes, hops)

    # --- inject neighbours that pass the gate + aren't already candidates ---
    # Iterate in ``(distance, path)`` order so the injection is deterministic
    # AND the ``_GRAPH_EXPAND_MAX_INJECT`` cap keeps the CLOSEST neighbours
    # (Codex SEV-2 — tie-ordering + bounded injection: equidistant injected
    # share an identical ``1/(k+rank)``, so an unordered/uncapped inject could
    # both flood a hub vault and vary the top-``limit`` cut run to run).
    expand_rank: dict[str, int] = {}
    for node, d in sorted(dist.items(), key=lambda kv: (kv[1], _path_of_node(kv[0]))):
        if d == 0:
            continue  # the seed itself — always an existing candidate
        path = _path_of_node(node)
        if path in candidate_paths:
            continue  # already a candidate — the proximity leg's job, not expansion
        if path not in notes_by_path:
            # Either the node isn't indexed OR it was filtered out by the
            # candidate gate (sensitivity / frontmatter). NEVER inject it —
            # this is the #no-mnpi-to-cloud absolute (was cited as §5.4): no
            # expansion past the gate. (With the
            # gate-passing subgraph above, a hidden node is already absent; this
            # stays as defence-in-depth.)
            continue
        expand_rank[path] = d + 1
        if len(expand_rank) >= _GRAPH_EXPAND_MAX_INJECT:
            logger.debug(
                "graph expansion injection capped at %d candidates (hub vault)",
                _GRAPH_EXPAND_MAX_INJECT,
            )
            break
    return expand_rank


# ============================================================ fts helpers


# Token regex for query escape. Splits on anything that isn't a unicode
# word char so callers can paste raw operator queries (with quotes,
# colons, parens, deal-name punctuation) without breaking FTS5 syntax.
_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fts_query_escape(query_text: str) -> str:
    """Convert an operator query into an FTS5-safe MATCH expression.

    Splits on word boundaries, double-quotes each token (literal match),
    then ORs them. Wider net than AND — we're oversampling per lane and
    re-ranking; the blend already rewards notes that hit on multiple
    terms via the FTS5 internal BM25.

    Returns "" when the query has no usable tokens (e.g. all punctuation).
    Caller treats "" as "skip the FTS lane".
    """
    tokens = _FTS_TOKEN_RE.findall(query_text or "")
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens)


def _fts_table_present(conn: sqlite3.Connection) -> bool:
    """True iff the ``recall_fts`` virtual table exists in this DB.

    Older index files (pre-#54b) won't have it. We never raise — the
    caller falls back to the vector + importance contribution.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'recall_fts'"
    ).fetchone()
    return row is not None


# ============================================================ filter


def _passes_filter(
    path: str,
    meta: dict[str, Any],
    modified_at: str | None,
    f: Filter,
) -> bool:
    if f.types:
        if str(meta.get("type", "")) not in f.types:
            return False
    if f.sensitivity_max:
        # F-33 (RECALL-2): an unknown / mis-cased CEILING used to resolve to
        # ``len-1`` (= MNPI = the MOST PERMISSIVE ceiling) → fail-OPEN: a typo
        # in the ceiling surfaced confidential/MNPI notes. Fail CLOSED instead —
        # an unrecognised ceiling caps at the MOST RESTRICTIVE tier (public).
        # Case-insensitive so a correctly-spelled value in any case still works.
        max_idx = _SENSITIVITY_RANK.get(
            str(f.sensitivity_max).strip().lower(), _MOST_RESTRICTIVE_RANK
        )
        # An unknown / mis-cased NOTE sensitivity is treated as the MOST
        # SENSITIVE tier (MNPI) so it is excluded unless the ceiling is MNPI —
        # the safe direction for the note side (unchanged intent, now via the
        # shared case-insensitive rank).
        note_sensitivity = str(meta.get("sensitivity", "internal")).strip().lower()
        note_idx = _SENSITIVITY_RANK.get(note_sensitivity, _MOST_SENSITIVE_RANK)
        if note_idx > max_idx:
            return False
    if f.project:
        proj_field = str(meta.get("project", ""))
        if f.project.lower() not in proj_field.lower():
            return False
    if f.sectors:
        # Match any sector against tags or sector field
        tags = meta.get("tags", []) if isinstance(meta.get("tags"), list) else []
        sector = str(meta.get("sector", ""))
        all_lc = [str(t).lower() for t in tags] + [sector.lower()]
        if not any(s.lower() in t for s in f.sectors for t in all_lc):
            return False
    if f.modified_after and modified_at:
        if modified_at < f.modified_after:
            return False
    if f.modified_before and modified_at:
        if modified_at > f.modified_before:
            return False
    if f.path_prefix and not path.startswith(f.path_prefix):
        return False
    if f.exclude_path_prefix and path.startswith(f.exclude_path_prefix):
        return False
    return True


# ============================================================ math


def _magnitude(vec: list[float]) -> float:
    return sum(x * x for x in vec) ** 0.5
