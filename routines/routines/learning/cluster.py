"""Cluster feedback events into themes using BERTopic.

BERTopic pipeline: sentence-transformers embeddings → UMAP reduction →
HDBSCAN density-based clustering → c-TF-IDF topic words. Runs fully
local; only network call is the one-off model download by
sentence-transformers (cached in `~/.cache/huggingface/` thereafter).

Replaces the earlier greedy-cosine clusterer. Reason: HDBSCAN handles
variable-density clusters and flags outliers natively (topic -1) so
noise events stop polluting real clusters. See
agentic_os_implementation_plan_v3.md §6.5 Phase A.
"""

from __future__ import annotations

import logging
from typing import Any

from routines.learning.schema import FeedbackCluster, FeedbackEvent

log = logging.getLogger(__name__)


DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
RANDOM_STATE = 42  # UMAP is non-deterministic without this — pin for reproducibility
SMALL_N_SKIP_UMAP = 30  # Below this many events, run HDBSCAN on raw 384-d embeddings


class _IdentityReducer:
    """No-op dimensionality reducer. Used at small N where UMAP collapses
    semantically distinct points into a single blob, defeating HDBSCAN."""

    def fit(self, X, y=None):
        return self

    def fit_transform(self, X, y=None):
        return X

    def transform(self, X):
        return X


def cluster_events(
    events: list[FeedbackEvent],
    *,
    client: Any = None,
    embed_model: str = DEFAULT_EMBED_MODEL,
    min_cluster_size: int = 2,
    **_legacy: Any,
) -> list[FeedbackCluster]:
    """Embed events with sentence-transformers, cluster via UMAP+HDBSCAN.

    `client` and any other legacy kwargs (e.g. `sim_threshold` from the
    greedy-cosine era) are accepted but ignored — keeps the CLI's
    `cluster_events(events, client=..., min_cluster_size=...)` call site
    working without edits.
    """
    if not events or len(events) < min_cluster_size:
        return []

    from bertopic import BERTopic
    from bertopic.vectorizers import ClassTfidfTransformer
    from sentence_transformers import SentenceTransformer
    from sklearn.feature_extraction.text import CountVectorizer
    import hdbscan
    import umap

    texts = [ev.text for ev in events]
    n = len(events)

    try:
        embedder = SentenceTransformer(embed_model)
        # normalize_embeddings=True makes euclidean distance on the unit
        # sphere a monotone function of cosine distance — HDBSCAN works
        # natively in euclidean, so this is how we get cosine semantics.
        embeddings = embedder.encode(
            texts, show_progress_bar=False, normalize_embeddings=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("learning: sentence-transformers embed failed: %s", e)
        return []

    if n < SMALL_N_SKIP_UMAP:
        umap_model: Any = _IdentityReducer()
    else:
        umap_model = umap.UMAP(
            n_neighbors=max(2, min(15, n - 1)),
            n_components=5,
            min_dist=0.0,
            metric="cosine",
            random_state=RANDOM_STATE,
        )

    hdbscan_min = max(2, min_cluster_size)
    # min_samples=1: HDBSCAN's default of min_samples=min_cluster_size leaves
    # everything as outliers (-1) at small N. Lowering to 1 makes every point
    # a "core point" — truly isolated outliers still go to -1 via mutual
    # reachability, but clear two-cluster structures separate cleanly.
    topic_model = BERTopic(
        embedding_model=embedder,
        umap_model=umap_model,
        hdbscan_model=hdbscan.HDBSCAN(
            min_cluster_size=hdbscan_min,
            min_samples=1,
            metric="euclidean",
            cluster_selection_method="leaf",
            prediction_data=True,
        ),
        vectorizer_model=CountVectorizer(stop_words="english", min_df=1),
        ctfidf_model=ClassTfidfTransformer(reduce_frequent_words=True),
        verbose=False,
    )

    try:
        topic_ids, _probs = topic_model.fit_transform(texts, embeddings)
    except Exception as e:  # noqa: BLE001
        log.warning("learning: BERTopic fit failed: %s", e)
        return []

    by_topic: dict[int, list[int]] = {}
    for i, tid in enumerate(topic_ids):
        tid_int = int(tid)
        if tid_int == -1:
            continue
        by_topic.setdefault(tid_int, []).append(i)

    clusters: list[FeedbackCluster] = []
    for tid, indices in by_topic.items():
        if len(indices) < min_cluster_size:
            continue
        members = [events[i] for i in indices]
        kinds_seen: list[str] = []
        for ev in members:
            if ev.prior_artifact_kind and ev.prior_artifact_kind not in kinds_seen:
                kinds_seen.append(ev.prior_artifact_kind)

        rep_text: str | None = None
        try:
            rep = topic_model.get_representative_docs(tid)
            if rep:
                rep_text = rep[0]
        except Exception:  # noqa: BLE001
            pass

        clusters.append(
            FeedbackCluster(
                theme="(unlabeled)",
                events=members,
                centroid_text=rep_text or members[0].text,
                artifact_kinds=kinds_seen,
            )
        )

    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters
