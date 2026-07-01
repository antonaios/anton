"""CLI for the vault-graph Stage-1 graph layer (#45).

Subcommands:
    vault-graph who-touched "<topic>" [--hops N] [--kind K ...]
    vault-graph path "<A>" "<B>"
    vault-graph related "<X>" [--depth N]
    vault-graph neighbours "<X>" [--kind K ...] [--edge-kind E ...]
    vault-graph stats

The graph is rebuilt from the vault on every invocation (Stage 1 — no
persistence). Node refs accept an exact path (``People/Jane Doe``), a path
with ``.md``, or a bare name (``Jane Doe``) resolved by unique basename.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

import click

from routines.vault_graph import graph as gmod
from routines.vault_graph import queries as q


def _default_vault() -> Path:
    """Platform-appropriate vault default (mirrors ``api.deps._default_vault``).

    The bridge runs under native Windows where the vault is ``<vault>``;
    the CLI may also run under WSL where it is ``/mnt/x/OS AI Vault``. Override
    with ``AGENTIC_VAULT`` when present."""
    env = os.environ.get("AGENTIC_VAULT")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("<vault>")
    return Path("/mnt/x/OS AI Vault")


DEFAULT_VAULT = _default_vault()

_VAULT_OPT = click.option(
    "--vault",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_VAULT,
    show_default=True,
    help="Vault root (default: platform default or $AGENTIC_VAULT).",
)


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Query the vault's wikilink graph (Stage 1 — rebuilt on every call)."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _resolve_or_exit(g, ref: str) -> str:
    """Resolve a user node ref to a node id, or print a hint + exit(1)."""
    node = gmod.resolve_node(g, ref)
    if node is None:
        click.echo(
            f"Node not found (or ambiguous): {ref!r}. "
            "Try the full path, e.g. 'People/Jane Doe'.",
            err=True,
        )
        sys.exit(1)
    return node


@main.command("who-touched")
@click.argument("topic")
@_VAULT_OPT
@click.option("--hops", type=int, default=2, show_default=True)
@click.option(
    "--kind", "kinds", multiple=True,
    help="Filter results to these node kinds (repeatable): "
         "--kind Person --kind Project",
)
def who_touched_cmd(topic: str, vault: Path, hops: int, kinds: tuple[str, ...]) -> None:
    """Who/what connects to TOPIC within N hops, with paths traced."""
    g = gmod.build_from_vault(vault)
    node = _resolve_or_exit(g, topic)
    results = q.who_touched(g, node, hops=hops, kinds=list(kinds) or None)
    if not results:
        click.echo(f"No nodes within {hops} hops of {node!r}.")
        return
    click.echo(f"Within {hops} hops of [[{node}]] ({gmod.node_kind(g, node)}):\n")
    for r in results:
        trace = " → ".join(r.path) if r.path else "(no path)"
        click.echo(f"  [{r.kind}] {r.node}  (d={r.distance})")
        click.echo(f"      path: {trace}")


@main.command("path")
@click.argument("node_a")
@click.argument("node_b")
@_VAULT_OPT
def path_cmd(node_a: str, node_b: str, vault: Path) -> None:
    """Shortest wikilink path between NODE_A and NODE_B."""
    g = gmod.build_from_vault(vault)
    a = _resolve_or_exit(g, node_a)
    b = _resolve_or_exit(g, node_b)
    path = q.path_between(g, a, b)
    if path is None:
        click.echo(f"No path between {a!r} and {b!r}.")
        sys.exit(1)
    click.echo(" → ".join(path))


@main.command("related")
@click.argument("node")
@_VAULT_OPT
@click.option("--depth", type=int, default=2, show_default=True)
def related_cmd(node: str, vault: Path, depth: int) -> None:
    """Distance-tagged linked + backlinked neighbours of NODE (BFS)."""
    g = gmod.build_from_vault(vault)
    seed = _resolve_or_exit(g, node)
    results = q.related(g, seed, depth=depth)
    if not results:
        click.echo(f"No related notes within depth {depth} of {seed!r}.")
        return
    click.echo(f"Related to [[{seed}]] (depth {depth}; -=backlink +=forward):\n")
    for r in results:
        sign = f"{r.distance:+d}"
        click.echo(f"  {sign}  [{r.kind}] {r.node}")


@main.command("neighbours")
@click.argument("node")
@_VAULT_OPT
@click.option("--kind", "kinds", multiple=True, help="Filter by neighbour node kind.")
@click.option(
    "--edge-kind", "edge_kinds", multiple=True,
    help="Filter by edge kind (e.g. frontmatter.target, mentions, sources, body).",
)
def neighbours_cmd(
    node: str, vault: Path, kinds: tuple[str, ...], edge_kinds: tuple[str, ...]
) -> None:
    """Direct (1-hop) neighbours of NODE, in + out edges."""
    g = gmod.build_from_vault(vault)
    seed = _resolve_or_exit(g, node)
    results = q.neighbours(
        g, seed, kinds=list(kinds) or None, edge_kinds=list(edge_kinds) or None
    )
    if not results:
        click.echo(f"No neighbours of {seed!r}.")
        return
    click.echo(f"Neighbours of [[{seed}]]:\n")
    for n in results:
        click.echo(
            f"  ({n.direction:>4}) [{n.kind}] {n.node}  "
            f"[{', '.join(n.edge_kinds)}]"
        )


@main.command("stats")
@_VAULT_OPT
def stats_cmd(vault: Path) -> None:
    """Print graph size + node-kind breakdown (a quick sanity check)."""
    g = gmod.build_from_vault(vault)
    from collections import Counter

    kinds = Counter(gmod.node_kind(g, n) for n in g.nodes)
    dangling = sum(1 for _, d in g.nodes(data=True) if not d.get("resolved", True))
    click.echo(f"vault:    {vault}")
    click.echo(f"nodes:    {g.number_of_nodes()} ({dangling} dangling)")
    click.echo(f"edges:    {g.number_of_edges()}")
    click.echo("by kind:  " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
