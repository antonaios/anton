"""Build an in-memory ``networkx`` graph from a parsed vault (#45, Stage 1).

Node = a vault note, identified by its vault-relative path WITHOUT the ``.md``
suffix (e.g. ``People/Jane Doe``, ``Companies/DemoTelco Group plc``). Each node
carries ``kind`` (Person / Company / Sector / Project / Note) and ``title``
attributes. Edge = a directed ``[[wikilink]]`` reference, source-note →
target-note, carrying a ``kind`` attribute (the field context the link was
found in: ``frontmatter.<field>`` / ``body`` / ``mentions`` / ``sources``).

We use ``networkx.MultiDiGraph`` so multiple links between the same ordered
pair of notes (e.g. a frontmatter ``target:`` link AND a body mention) are
preserved as distinct parallel edges, each keeping its own ``kind``. The
brief allows either DiGraph or MultiDiGraph; MultiDiGraph is the strict
superset and is what lets ``neighbours(kinds=[...])`` filter precisely on the
relationship type without collapsing a node pair's multiple relationships.

Link-target resolution
----------------------
A wikilink target may be **path-qualified** (``Companies/Foo``) or a **bare
name** (``Foo``, ``CLAUDE``). Resolution:

  1. Exact match on a known node path (the common case — the vault links by
     full path).
  2. Bare name → unique node whose path STEM matches (case-insensitive). If
     the stem is ambiguous (two notes share a basename), the link is left
     **dangling** rather than guessing.
  3. Unresolved targets become **dangling nodes** (added with
     ``kind="Note"`` and ``resolved=False``) so that the path/traversal
     queries still see the edge — a link to a not-yet-created note is real
     structural signal, not an error.

REBUILD-ON-CALL, NO PERSISTENCE (Stage 1)
-----------------------------------------
The graph is rebuilt from scratch on every CLI invocation / bridge request.
No SQLite, no kuzu, no on-disk cache. Fast enough for vaults < ~5k notes
(the perf test asserts a 1000-note vault builds + queries in < 2s).

ROADMAP — NOT BUILT IN STAGE 1 (do not implement here)
------------------------------------------------------
The following are explicitly OUT OF SCOPE for Stage 1 and tracked elsewhere:

  * **#45b — persistence** — SQLite recursive-CTE pattern or an embedded
    graph store (kuzu) with incremental rebuild on the HiNotes watcher.
    Triggered when a Stage-1 query exceeds ~2s.
  * **#45b — ``vault_edges`` temporal triples** —
    ``(subject, predicate, object, valid_from, valid_to, source)`` from the
    Mnemosyne schema, so claims about People/Companies become
    time-windowed rather than eternally true.
  * **Leiden community detection** (``graspologic.partition.leiden``) — surfaces
    network-neighbourhood clusters; ~0.5 day on top of #45b.
  * **#45c — MCP server** — expose graph queries as an in-session tool.

NOTE (do not build now): recall (#54) will later gain a **3rd RRF channel =
graph**, composing with ``#54-rrf`` so retrieval fuses BM25 + vector + graph
proximity (agentmemory's triple-stream, reached via Anton's governed path).
That composition is recall's work, not this module's — vault_graph only needs
to expose the traversal queries it already does.
"""

from __future__ import annotations

import logging
from pathlib import Path

import networkx as nx

from routines.vault_graph.parser import (
    NOTE,
    ParsedVault,
    node_kind_for_path,
    parse_vault,
)

logger = logging.getLogger(__name__)


def _stem(node_id: str) -> str:
    """Basename of a node id (the part after the last ``/``)."""
    return node_id.rsplit("/", 1)[-1]


def _build_stem_index(node_ids: list[str]) -> dict[str, str | None]:
    """Map lower-cased stem → unique node id, or None if ambiguous.

    A stem shared by two notes resolves to None so bare-name links don't
    silently pick the wrong note."""
    index: dict[str, str | None] = {}
    for nid in node_ids:
        key = _stem(nid).lower()
        if key in index:
            index[key] = None  # collision → ambiguous
        else:
            index[key] = nid
    return index


def build_graph(parsed: ParsedVault) -> nx.MultiDiGraph:
    """Build a ``MultiDiGraph`` from a ``ParsedVault``.

    Nodes carry ``kind`` / ``title`` / ``resolved``; edges carry ``kind``.
    Targets that don't resolve to a real note become dangling nodes
    (``resolved=False``) so traversal still sees the structural edge.
    """
    g: nx.MultiDiGraph = nx.MultiDiGraph()

    # Pass 1 — add every real note as a node first, so link resolution can see
    # the full id set (forward references resolve correctly).
    real_ids: list[str] = []
    for note in parsed.notes:
        real_ids.append(note.rel_path)
        g.add_node(
            note.rel_path,
            kind=note.node_kind,
            title=note.title,
            resolved=True,
        )

    real_id_set = set(real_ids)
    stem_index = _build_stem_index(real_ids)

    def resolve(target: str) -> str:
        """Resolve a raw link target to a node id (existing or dangling)."""
        # 1. Exact path match.
        if target in real_id_set:
            return target
        # 2. Bare-name → unique stem match.
        if "/" not in target:
            hit = stem_index.get(target.lower())
            if hit is not None:
                return hit
        return target  # 3. dangling — returned verbatim

    # Pass 2 — add edges, materialising dangling targets as Note nodes.
    for note in parsed.notes:
        for edge in note.edges:
            tgt = resolve(edge.target)
            if tgt not in g:
                # Dangling target — a link to a note that doesn't exist (yet).
                g.add_node(
                    tgt,
                    kind=node_kind_for_path(tgt + ".md"),
                    title=_stem(tgt),
                    resolved=False,
                )
            # Self-links add no relational signal — skip them.
            if tgt == note.rel_path:
                continue
            g.add_edge(note.rel_path, tgt, kind=edge.kind)

    logger.debug(
        "vault_graph: built MultiDiGraph nodes=%d edges=%d (real=%d dangling=%d)",
        g.number_of_nodes(),
        g.number_of_edges(),
        len(real_id_set),
        g.number_of_nodes() - len(real_id_set),
    )
    return g


def build_from_vault(vault_root: Path) -> nx.MultiDiGraph:
    """Convenience: walk + parse + build in one call (the rebuild-on-call path)."""
    return build_graph(parse_vault(Path(vault_root)))


# ── small introspection helpers (used by queries + CLI) ───────────────────────


def node_kind(g: nx.MultiDiGraph, node_id: str) -> str:
    """Return a node's ``kind`` attribute, defaulting to ``Note`` if absent."""
    return g.nodes.get(node_id, {}).get("kind", NOTE)


def resolve_node(g: nx.MultiDiGraph, query: str) -> str | None:
    """Resolve a user-supplied node reference to an actual node id.

    Accepts an exact node id, a path with a ``.md`` suffix, or a bare name
    (case-insensitive stem match). Returns None when nothing matches, or the
    bare name is ambiguous (callers surface a helpful "not found" / "be more
    specific" message rather than guessing).
    """
    q = query.strip()
    if q.endswith(".md"):
        q = q[:-3]
    # Exact id.
    if q in g:
        return q
    # Case-insensitive exact-id match.
    lowered = q.lower()
    exact_ci = [n for n in g.nodes if n.lower() == lowered]
    if len(exact_ci) == 1:
        return exact_ci[0]
    # Bare-name stem match (unique).
    stem_matches = [n for n in g.nodes if _stem(n).lower() == lowered]
    if len(stem_matches) == 1:
        return stem_matches[0]
    return None
