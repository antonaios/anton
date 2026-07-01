"""Bridge route for the vault-graph Stage-1 graph layer (#45).

``GET /api/vault/graph/who-touched?topic=<X>&hops=2`` — rebuilds the
wikilink graph from the vault (Stage 1 — no persistence) and returns the
nodes within ``hops`` of ``topic``, each with the shortest path traced.
This is the dashboard-integration surface for the headline #45 query.

Companion read-only endpoints (same rebuild-on-call contract):
  * ``GET /api/vault/graph/path?a=<A>&b=<B>``        — shortest path
  * ``GET /api/vault/graph/related?node=<X>&depth=2`` — distance-tagged BFS

The graph build is pure-CPU SQLite-free vault walking; sub-2s on a ~1k-note
vault (asserted by the module's perf test), so we build it in-process per
request like ``recall_query`` does — no subprocess, no cache.

Sensitivity note: this route exposes only *structural* relationships (which
notes link to which) and node kinds derived from path prefixes — never note
bodies or frontmatter values. It returns vault-relative paths (the same
information Obsidian's backlinks pane shows), so it carries no higher
sensitivity than the existing ``GET /api/vault-pulse`` mtime endpoint.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.vault_graph import graph as gmod
from routines.vault_graph import queries as q

router = APIRouter(prefix="/vault/graph", tags=["vault"])
log = logging.getLogger(__name__)


# ── response models ───────────────────────────────────────────────────────────


class TouchHit(BaseModel):
    node: str
    kind: str
    distance: int
    path: list[str] = Field(default_factory=list)


class WhoTouchedResponse(BaseModel):
    status: str = "ok"
    topic: str                       # the requested topic string
    resolved_node: Optional[str] = None   # the node id it resolved to (None if not found)
    hops: int
    count: int
    results: list[TouchHit] = Field(default_factory=list)
    duration_ms: int = 0
    warning: Optional[str] = None


class PathResponse(BaseModel):
    status: str = "ok"
    a: str
    b: str
    resolved_a: Optional[str] = None
    resolved_b: Optional[str] = None
    path: Optional[list[str]] = None
    duration_ms: int = 0
    warning: Optional[str] = None


class RelatedHit(BaseModel):
    node: str
    kind: str
    distance: int                    # signed: -=backlink +=forward


class RelatedResponse(BaseModel):
    status: str = "ok"
    node: str
    resolved_node: Optional[str] = None
    depth: int
    count: int
    results: list[RelatedHit] = Field(default_factory=list)
    duration_ms: int = 0
    warning: Optional[str] = None


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/who-touched", response_model=WhoTouchedResponse)
def who_touched(
    topic: str = Query(..., min_length=1, description="Topic node (path or bare name)."),
    hops: int = Query(default=2, ge=1, le=6),
) -> WhoTouchedResponse:
    """Nodes within ``hops`` of ``topic`` with shortest paths traced."""
    t0 = time.monotonic()
    if not VAULT.exists():
        return WhoTouchedResponse(
            status="error", topic=topic, hops=hops, count=0,
            warning=f"Vault not found at {VAULT}",
        )
    g = gmod.build_from_vault(VAULT)
    node = gmod.resolve_node(g, topic)
    if node is None:
        return WhoTouchedResponse(
            status="ok", topic=topic, resolved_node=None, hops=hops, count=0,
            duration_ms=int((time.monotonic() - t0) * 1000),
            warning=f"Topic not found (or ambiguous): {topic!r}",
        )
    results = q.who_touched(g, node, hops=hops)
    hits = [
        TouchHit(node=r.node, kind=r.kind, distance=r.distance, path=r.path)
        for r in results
    ]
    return WhoTouchedResponse(
        status="ok",
        topic=topic,
        resolved_node=node,
        hops=hops,
        count=len(hits),
        results=hits,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


@router.get("/path", response_model=PathResponse)
def path(
    a: str = Query(..., min_length=1),
    b: str = Query(..., min_length=1),
) -> PathResponse:
    """Shortest wikilink path between two notes."""
    t0 = time.monotonic()
    if not VAULT.exists():
        return PathResponse(status="error", a=a, b=b, warning=f"Vault not found at {VAULT}")
    g = gmod.build_from_vault(VAULT)
    na = gmod.resolve_node(g, a)
    nb = gmod.resolve_node(g, b)
    if na is None or nb is None:
        missing = a if na is None else b
        return PathResponse(
            status="ok", a=a, b=b, resolved_a=na, resolved_b=nb, path=None,
            duration_ms=int((time.monotonic() - t0) * 1000),
            warning=f"Node not found (or ambiguous): {missing!r}",
        )
    p = q.path_between(g, na, nb)
    return PathResponse(
        status="ok", a=a, b=b, resolved_a=na, resolved_b=nb, path=p,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


@router.get("/related", response_model=RelatedResponse)
def related(
    node: str = Query(..., min_length=1),
    depth: int = Query(default=2, ge=1, le=6),
) -> RelatedResponse:
    """Distance-tagged linked + backlinked neighbours of ``node`` (BFS)."""
    t0 = time.monotonic()
    if not VAULT.exists():
        return RelatedResponse(
            status="error", node=node, depth=depth, count=0,
            warning=f"Vault not found at {VAULT}",
        )
    g = gmod.build_from_vault(VAULT)
    seed = gmod.resolve_node(g, node)
    if seed is None:
        return RelatedResponse(
            status="ok", node=node, resolved_node=None, depth=depth, count=0,
            duration_ms=int((time.monotonic() - t0) * 1000),
            warning=f"Node not found (or ambiguous): {node!r}",
        )
    results = q.related(g, seed, depth=depth)
    hits = [RelatedHit(node=r.node, kind=r.kind, distance=r.distance) for r in results]
    return RelatedResponse(
        status="ok", node=node, resolved_node=seed, depth=depth,
        count=len(hits), results=hits,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
