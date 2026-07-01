"""Graph queries over the vault wikilink graph (#45, Stage 1).

All queries take a built ``networkx.MultiDiGraph`` (from ``graph.build_graph``)
so the graph is rebuilt once per CLI call / bridge request and reused across
the queries in that call.

Query surface:

  * :func:`who_touched` — nodes within ``hops`` of a topic, with the shortest
    path to each traced. This is the headline #45 query: *"who in my network
    has worked on a UK pubs-with-rooms deal?"* answered structurally rather
    than semantically. Traversal is **undirected** over the directed graph
    (a Sector is *linked-to* by Projects, so the relevant neighbours are
    reachable via in-edges as much as out-edges).

  * :func:`path_between` — the shortest wikilink path between two notes
    (undirected over the directed graph), or None if disconnected.

  * :func:`neighbours` — direct (1-hop) neighbours of a node, optionally
    filtered by node ``kinds`` and/or edge ``kinds``.

  * :func:`related` — BFS over linked + backlinked notes, each result tagged
    with a signed distance: ``-N..-1`` for backlink (incoming) hops,
    ``+1..+N`` for forward (outgoing) hops, ``0`` for the seed. This is the
    obsidian-hybrid-search ``--related`` traversal lifted into Stage 1.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Iterable

import networkx as nx

from routines.vault_graph.graph import node_kind

logger = logging.getLogger(__name__)

# Per-node adjacency-scan fan-out multiplier for the bounded BFS (Codex SEV-2).
# A single hub node's adjacency is examined at most ``max_nodes × _SCAN_FANOUT``
# entries, so per-node work is O(scan budget), NEVER O(degree). The multiplier
# is generous (×8) so a realistic vault — where every hub's degree is far below
# the budget — still examines the full adjacency and picks the exact
# lexicographic-closest set; only a pathological super-hub trips the prefix bound.
_SCAN_FANOUT = 8


# ── who_touched ───────────────────────────────────────────────────────────────


@dataclass
class TouchResult:
    """One node reachable from the topic, with the path that connects them."""

    node: str
    kind: str
    distance: int
    path: list[str] = field(default_factory=list)  # node ids, topic … node


def _bounded_neighbourhood(
    undirected: nx.Graph, topic: str, hops: int, max_nodes: int
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Frontier-BOUNDED multi-level BFS from ``topic`` (the #45 recall safety
    path — Codex SEV-2). Unlike ``nx.single_source_shortest_path*`` (which walks
    the WHOLE neighbourhood within ``hops`` to completion — expensive on a hub
    node with thousands of links), this stops EXPANDING the instant ``max_nodes``
    distinct non-topic nodes have been discovered, so the traversal cost is
    bounded *during* the walk, not trimmed after.

    Explores strictly in BFS distance order; within a level the smallest-id
    UNSEEN neighbours are kept first so the set retained when the cap bites is
    deterministic. A high-degree (hub) node's adjacency is examined for AT MOST
    ``max_nodes × _SCAN_FANOUT`` entries (``itertools.islice`` on the source) and
    only a bounded ``heapq.nsmallest`` heap of size ``remaining`` is built over
    that window, so per-node work is O(scan budget) — never O(degree) and never an
    O(deg·log deg) sort (Codex SEV-2). For a realistic vault (hub degree ≪ budget)
    the whole adjacency is still seen, so the selection is the exact lexicographic
    closest set. Returns ``(lengths, paths)`` shaped exactly like the networkx
    calls it replaces (one shortest path per discovered node)."""
    scan_cap = max(max_nodes, 1) * _SCAN_FANOUT
    lengths: dict[str, int] = {topic: 0}
    paths: dict[str, list[str]] = {topic: [topic]}
    frontier = [topic]
    depth = 0
    while frontier and depth < hops and len(lengths) - 1 < max_nodes:
        depth += 1
        nxt: list[str] = []
        for u in frontier:
            remaining = max_nodes - (len(lengths) - 1)  # -1 excludes the topic
            if remaining <= 0:
                break
            # Hard-cap the adjacency scan at ``scan_cap`` entries via islice on the
            # SOURCE iterator (not a post-filter generator, which could still drain
            # a hub), then keep the smallest ``remaining`` UNSEEN by id. Bounded
            # work + deterministic lexicographic selection within the window.
            window = itertools.islice(undirected.neighbors(u), scan_cap)
            unseen = [v for v in window if v not in lengths]
            for v in heapq.nsmallest(remaining, unseen):
                lengths[v] = depth
                paths[v] = paths[u] + [v]
                nxt.append(v)
            if len(lengths) - 1 >= max_nodes:
                logger.debug(
                    "who_touched frontier cap hit: stopped at %d nodes (hub node)",
                    max_nodes,
                )
                return lengths, paths
        frontier = nxt
    return lengths, paths


def who_touched(
    g: nx.MultiDiGraph,
    topic: str,
    *,
    hops: int = 2,
    kinds: Iterable[str] | None = None,
    max_nodes: int | None = None,
) -> list[TouchResult]:
    """Return nodes within ``hops`` of ``topic``, each with its shortest path.

    ``topic`` must be an exact node id (callers resolve fuzzy refs via
    ``graph.resolve_node`` first). Traversal is undirected over the directed
    graph so backlinks count (a Sector is reached *from* the Projects that
    link to it). Results exclude the topic itself, are filtered to ``kinds``
    when given, and are ordered by ``(distance, node)`` for determinism.

    ``max_nodes`` (default None = unbounded, the original behaviour for every
    existing caller) caps the BFS *frontier during traversal* at that many
    discovered nodes (closest-first, deterministic) — the #45 recall leg passes
    it so a hub node can't blow up the in-memory walk (Codex SEV-2).

    Returns an empty list when the topic is not in the graph.
    """
    if topic not in g:
        return []

    undirected = g.to_undirected(as_view=True)
    if max_nodes is None:
        # Single-source shortest path lengths (BFS) bounded by ``hops``.
        lengths = nx.single_source_shortest_path_length(undirected, topic, cutoff=hops)
        # Shortest paths (node-id lists) for tracing.
        paths = nx.single_source_shortest_path(undirected, topic, cutoff=hops)
    else:
        # Frontier-bounded walk — stops expanding once ``max_nodes`` are found.
        lengths, paths = _bounded_neighbourhood(undirected, topic, hops, max_nodes)

    kind_filter = set(kinds) if kinds else None
    results: list[TouchResult] = []
    for node, dist in lengths.items():
        if node == topic:
            continue
        nk = node_kind(g, node)
        if kind_filter is not None and nk not in kind_filter:
            continue
        results.append(
            TouchResult(node=node, kind=nk, distance=dist, path=paths.get(node, []))
        )
    results.sort(key=lambda r: (r.distance, r.node))
    return results


# ── path_between ──────────────────────────────────────────────────────────────


def path_between(g: nx.MultiDiGraph, a: str, b: str) -> list[str] | None:
    """Return the shortest wikilink path (node-id list) from ``a`` to ``b``.

    Undirected over the directed graph (a path that traverses a backlink is
    still a real connection). Returns ``[a]`` when ``a == b`` and they exist,
    or None when either node is missing or the two are disconnected.
    """
    if a not in g or b not in g:
        return None
    if a == b:
        return [a]
    undirected = g.to_undirected(as_view=True)
    try:
        return nx.shortest_path(undirected, a, b)
    except nx.NetworkXNoPath:
        return None


# ── neighbours ────────────────────────────────────────────────────────────────


@dataclass
class Neighbour:
    """A 1-hop neighbour with the edge kinds + direction connecting it."""

    node: str
    kind: str
    direction: str            # "out" (this→node), "in" (node→this), "both"
    edge_kinds: list[str] = field(default_factory=list)


def neighbours(
    g: nx.MultiDiGraph,
    node: str,
    *,
    kinds: Iterable[str] | None = None,
    edge_kinds: Iterable[str] | None = None,
) -> list[Neighbour]:
    """Return the direct (1-hop) neighbours of ``node``.

    Considers BOTH out-edges (notes this node links to) and in-edges
    (notes that link to this node — backlinks). ``kinds`` filters on the
    neighbour's node kind (Person / Company / …); ``edge_kinds`` filters on
    the edge's ``kind`` attribute (``frontmatter.target`` / ``mentions`` /
    …). Returns an empty list when the node is absent.
    """
    if node not in g:
        return []

    node_kind_filter = set(kinds) if kinds else None
    edge_kind_filter = set(edge_kinds) if edge_kinds else None

    # Accumulate per-neighbour: set of edge kinds + which directions seen.
    acc: dict[str, dict[str, object]] = {}

    def _record(other: str, ekind: str, direction: str) -> None:
        if edge_kind_filter is not None and ekind not in edge_kind_filter:
            return
        slot = acc.setdefault(other, {"kinds": set(), "dirs": set()})
        slot["kinds"].add(ekind)        # type: ignore[union-attr]
        slot["dirs"].add(direction)     # type: ignore[union-attr]

    # Out-edges: node → succ (MultiDiGraph: iterate keyed edge data).
    for _, succ, data in g.out_edges(node, data=True):
        _record(succ, data.get("kind", "body"), "out")
    # In-edges: pred → node (backlinks).
    for pred, _, data in g.in_edges(node, data=True):
        _record(pred, data.get("kind", "body"), "in")

    out: list[Neighbour] = []
    for other, slot in acc.items():
        nk = node_kind(g, other)
        if node_kind_filter is not None and nk not in node_kind_filter:
            continue
        dirs = slot["dirs"]  # type: ignore[assignment]
        direction = "both" if {"in", "out"} <= dirs else next(iter(dirs))  # type: ignore[operator]
        out.append(
            Neighbour(
                node=other,
                kind=nk,
                direction=direction,
                edge_kinds=sorted(slot["kinds"]),  # type: ignore[arg-type]
            )
        )
    out.sort(key=lambda n: n.node)
    return out


# ── related (distance-tagged BFS) ─────────────────────────────────────────────


@dataclass
class RelatedResult:
    """A related node tagged with a signed distance.

    Positive distance = reached by following outgoing links (things this
    note references); negative = reached by following backlinks (things that
    reference this note). 0 is the seed itself (not returned)."""

    node: str
    kind: str
    distance: int             # signed: -depth..-1 backlink, +1..+depth forward


def related(g: nx.MultiDiGraph, node: str, *, depth: int = 2) -> list[RelatedResult]:
    """BFS over linked + backlinked notes, distance-tagged (#45 ``--related``).

    Runs two directed BFS passes from ``node``:
      * **forward** over out-edges → results tagged ``+1 .. +depth``
      * **backward** over in-edges → results tagged ``-1 .. -depth``

    A node reachable both ways keeps the hop with the smaller absolute
    distance (ties → the forward/positive tag, since an outgoing reference
    is the more direct relationship). Ordered by ``(abs(distance), distance,
    node)`` so the closest neighbours come first and, within a ring, forward
    links precede backlinks. Excludes the seed; empty when the seed is
    absent.
    """
    if node not in g:
        return []

    best: dict[str, int] = {}

    def _bfs(directed_lengths: dict[str, int], sign: int) -> None:
        for other, dist in directed_lengths.items():
            if other == node or dist == 0:
                continue
            signed = sign * dist
            cur = best.get(other)
            if cur is None or abs(signed) < abs(cur) or (
                abs(signed) == abs(cur) and signed > cur
            ):
                best[other] = signed

    # Forward: follow out-edges (successors).
    fwd = nx.single_source_shortest_path_length(g, node, cutoff=depth)
    _bfs(fwd, +1)
    # Backward: follow in-edges. Reverse view flips edge direction so a BFS
    # on it walks the backlinks.
    rev = g.reverse(copy=False)
    bwd = nx.single_source_shortest_path_length(rev, node, cutoff=depth)
    _bfs(bwd, -1)

    out = [
        RelatedResult(node=n, kind=node_kind(g, n), distance=d)
        for n, d in best.items()
    ]
    out.sort(key=lambda r: (abs(r.distance), -r.distance, r.node))
    return out
