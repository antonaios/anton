"""#54-rerank — optional cross-encoder rerank stage over recall's fused top-k.

A second-stage cross-encoder (``bge-reranker-v2-m3``, the local sibling of
the nomic embedder) re-scores the fused RRF top-k by judging the (query,
document) pair *jointly* — strictly more precise than the bi-encoder
cosine that produced the first-stage ranking, at the cost of ~1-3s
latency. Hence OPT-IN per query (default OFF; see ``retrieve.query``'s
``rerank`` flag) and excluded from latency-sensitive paths.

Design constraints (per the #54-rerank brief):

  * **Opt-in + lazy.** Nothing here is imported on the recall hot path
    unless ``rerank=True`` — ``retrieve.query`` lazy-imports this module,
    and this module lazy-imports the heavy reranker lib only inside
    ``load_reranker``.
  * **Optional dependency.** The reranker lib (FlagEmbedding /
    sentence-transformers) + the ~560MB model are an OPTIONAL extra
    (``pip install -e .[recall]``). We NEVER force-download the model.
  * **Graceful skip.** If the lib isn't installed OR the model can't be
    loaded (not downloaded, offline, OOM, …), we LOG a warning and return
    the fused hits UNCHANGED. Recall must never crash because the
    optional reranker is missing.

The model is loaded once and cached process-wide (``_RERANKER_CACHE``) so
repeated reranked queries don't re-pay the model-load cost.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from routines.recall.retrieve import NoteHit

logger = logging.getLogger(__name__)


# Default model — the cross-encoder reranker named in the eval + brief.
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# How many of a hit's text fields to feed the cross-encoder, in priority
# order. We pick the first non-empty — the chunk that actually matched is
# the most informative input; tldr / body_excerpt are fallbacks.
_DOC_TEXT_FIELDS = ("best_chunk_text", "tldr", "body_excerpt")

# Process-wide cache: model name -> scorer callable (or None if a prior
# load attempt failed, so we don't retry a doomed import on every query).
# A sentinel distinguishes "never tried" from "tried and failed".
_NOT_LOADED = object()
_RERANKER_CACHE: dict[str, Any] = {}


# A scorer is any callable mapping a list of (query, doc) pairs to a list
# of float relevance scores (higher = more relevant). Both FlagReranker
# and CrossEncoder expose exactly this via ``.compute_score`` / ``.predict``.
Scorer = Callable[[list[tuple[str, str]]], list[float]]


def load_reranker(model_name: str = DEFAULT_RERANK_MODEL) -> Optional[Scorer]:
    """Lazily load a cross-encoder scorer; return ``None`` if unavailable.

    Tries ``FlagEmbedding.FlagReranker`` first (the reference impl for
    bge-reranker-v2-m3), then ``sentence_transformers.CrossEncoder`` as a
    fallback. Both the import AND the model load are guarded — a missing
    lib, an undownloaded model, or any load error returns ``None`` (logged
    once). The result is cached so a failed load isn't retried per query.

    We never force a model download here: if the weights aren't already
    on disk, the underlying lib will try to fetch them; in an offline /
    no-extra environment that raises, and we treat it as "unavailable".
    """
    cached = _RERANKER_CACHE.get(model_name, _NOT_LOADED)
    if cached is not _NOT_LOADED:
        return cached

    scorer: Optional[Scorer] = None

    # --- Attempt 1: FlagEmbedding (reference impl for bge-reranker-v2-m3).
    try:
        from FlagEmbedding import FlagReranker  # type: ignore

        reranker = FlagReranker(model_name, use_fp16=True)

        def _flag_scorer(pairs: list[tuple[str, str]]) -> list[float]:
            raw = reranker.compute_score([list(p) for p in pairs], normalize=True)
            # compute_score returns a scalar for a single pair, list otherwise.
            return [float(raw)] if isinstance(raw, (int, float)) else [float(x) for x in raw]

        scorer = _flag_scorer
        logger.info("rerank: loaded FlagReranker(%s)", model_name)
    except ImportError:
        logger.info(
            "rerank: FlagEmbedding not installed; trying sentence-transformers "
            "(install the optional reranker via `pip install -e .[recall]`)"
        )
    except Exception as e:  # noqa: BLE001 — any load failure → graceful skip
        logger.warning("rerank: FlagReranker(%s) load failed (%s); skipping", model_name, e)

    # --- Attempt 2: sentence-transformers CrossEncoder fallback.
    if scorer is None:
        try:
            from sentence_transformers import CrossEncoder  # type: ignore

            ce = CrossEncoder(model_name)

            def _ce_scorer(pairs: list[tuple[str, str]]) -> list[float]:
                raw = ce.predict([list(p) for p in pairs])
                return [float(x) for x in raw]

            scorer = _ce_scorer
            logger.info("rerank: loaded CrossEncoder(%s)", model_name)
        except ImportError:
            logger.warning(
                "rerank: no reranker lib available (FlagEmbedding / "
                "sentence-transformers). Skipping rerank; returning fused "
                "RRF order. Install via `pip install -e .[recall]`."
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("rerank: CrossEncoder(%s) load failed (%s); skipping", model_name, e)

    _RERANKER_CACHE[model_name] = scorer
    return scorer


def _hit_text(hit: "NoteHit") -> str:
    """Best available text for the cross-encoder, first non-empty of
    best_chunk_text / tldr / body_excerpt; falls back to the path so the
    pair is never empty (an empty doc would score meaninglessly)."""
    for fieldname in _DOC_TEXT_FIELDS:
        val = getattr(hit, fieldname, "") or ""
        if val.strip():
            return val
    return hit.path


def rerank_hits(
    query_text: str,
    hits: list["NoteHit"],
    *,
    top_k: int,
    model_name: str = DEFAULT_RERANK_MODEL,
    scorer: Optional[Scorer] = None,
) -> list["NoteHit"]:
    """Re-order the top ``top_k`` of ``hits`` with a cross-encoder.

    Only the first ``top_k`` (already in fused-score order) are reranked;
    the tail is appended unchanged after them. Each reranked hit gets its
    ``rerank_score`` set and its ``score`` overwritten with the
    cross-encoder score, so the final sort reflects the rerank.

    ``scorer`` is an injection seam for tests (a mock that reorders
    deterministically). In production it's ``None`` and we
    ``load_reranker`` — which returns ``None`` (→ unchanged hits, logged)
    when the optional dependency is absent.

    Returns the (possibly) re-ordered list. NEVER raises on a missing /
    failing reranker — the worst case is the input order, unchanged.
    """
    if not hits:
        return hits

    if scorer is None:
        scorer = load_reranker(model_name)
    if scorer is None:
        # Lib/model unavailable — graceful skip (already logged in loader).
        return hits

    head = hits[: max(0, top_k)]
    tail = hits[max(0, top_k):]
    if not head:
        return hits

    pairs = [(query_text, _hit_text(h)) for h in head]
    try:
        scores = scorer(pairs)
    except Exception as e:  # noqa: BLE001 — scoring failure → graceful skip
        logger.warning("rerank: scoring failed (%s); returning fused order", e)
        return hits

    if len(scores) != len(head):
        logger.warning(
            "rerank: scorer returned %d scores for %d pairs; returning fused order",
            len(scores), len(head),
        )
        return hits

    for h, s in zip(head, scores):
        h.rerank_score = float(s)
        h.score = float(s)

    head.sort(key=lambda h: h.score, reverse=True)
    return head + tail
