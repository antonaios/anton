"""vault-graph — Stage 1 graph layer over the vault's wikilink structure (#45).

Walks the vault, parses ``[[wikilink]]`` references from each note's body
AND its frontmatter values, and builds an in-memory ``networkx`` graph.
Exposed as a CLI (``vault-graph who-touched``, ``vault-graph path``,
``vault-graph related``) and a bridge endpoint
(``GET /api/vault/graph/who-touched``).

Public surface:
    parser.parse_vault(root) -> ParsedVault       walk + parse
    graph.build_graph(parsed) -> nx.MultiDiGraph  build the graph
    queries.who_touched / path_between / neighbours / related

See ``graph.py`` for the Stage-2 roadmap note (persistence, temporal
triples, community detection — explicitly NOT built in Stage 1).
"""

from __future__ import annotations

__all__ = ["parser", "graph", "queries"]
