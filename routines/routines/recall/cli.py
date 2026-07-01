"""CLI for the recall skill.

Subcommands:
    recall index [--vault PATH] [--rebuild]            Build/refresh the embedding index
    recall query "<query>" [--filter ...] [--synthesise]  Retrieve + optionally synthesise
    recall health [--vault PATH]                        Sanity check
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from routines.recall import index as idx
from routines.recall import retrieve as rtv
from routines.recall import synthesise as syn
from routines.shared.ollama_client import OllamaClient, OllamaError


DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Hierarchical-retrieval skill for the vault."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("index")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--rebuild", is_flag=True, help="Re-embed everything (default: only changed files)")
@click.option("--ollama-url", default="http://localhost:11434")
def index_cmd(vault: Path, rebuild: bool, ollama_url: str) -> None:
    """Build or refresh the embedding index."""
    client = OllamaClient(base_url=ollama_url)
    counts = idx.index_vault(vault, client=client, rebuild=rebuild)
    click.echo(
        f"scanned={counts['scanned']} added={counts['added']} "
        f"updated={counts['updated']} unchanged={counts['unchanged']} "
        f"removed={counts['removed']} errors={counts['errors']} "
        f"degraded={counts['embed_degraded']}"
    )
    if counts["errors"] or counts["embed_degraded"]:
        click.echo(
            "WARNING: some notes did not fully index "
            "(degraded = lexical-only, no embedding) — see `recall health`."
        )
    if counts["fm_salvaged"]:
        click.echo(
            f"NOTE: {counts['fm_salvaged']} note(s) indexed via frontmatter "
            "salvage (malformed YAML; fail-closed sensitivity if undeclared) "
            "— fix the YAML at source; see `recall health`."
        )
    click.echo(f"index at: {vault / idx.DEFAULT_INDEX_DIR / idx.DEFAULT_INDEX_DB}")


@main.command("query")
@click.argument("query_text")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--type", "types", multiple=True,
              help="Filter by type (repeatable): --type meeting-note --type company-profile")
@click.option("--max-sensitivity", type=click.Choice(["public", "internal", "confidential", "MNPI"]),
              help="Exclude notes more sensitive than this")
@click.option("--project", help="Filter to notes whose `project:` references this string")
@click.option("--path-prefix", help="Restrict to notes under this path (e.g. 'Companies/')")
@click.option("--exclude-path-prefix",
              help="Exclude notes under this path (e.g. 'Inbox/HiNotes/processed/')")
@click.option("--limit", type=int, default=15)
@click.option("--synthesise", is_flag=True,
              help="Run the map-reduce synthesis step (slower, but produces a final answer)")
@click.option("--explain", is_flag=True,
              help="Print the per-result RRF-score breakdown "
                   "(vector / fts raw scores + per-channel RRF "
                   "contributions / importance / decay / final).")
@click.option("--rerank", is_flag=True,
              help="Opt into the cross-encoder rerank stage over the fused "
                   "top-k (bge-reranker-v2-m3; needs `pip install -e .[recall]`; "
                   "skips gracefully if absent). Adds ~1-3s.")
@click.option("--graph", is_flag=True,
              help="Opt into the #45 graph leg — a 3rd RRF channel that boosts "
                   "candidates structurally near the top hits (wikilink "
                   "proximity, cap 2 hops). Rebuilds the in-memory vault graph "
                   "on call; default OFF.")
@click.option("--graph-expand", "graph_expand", is_flag=True,
              help="Opt into the #45-expansion leg — INJECT the top hits' "
                   "wikilink-graph neighbours as NEW candidates (who-touched-"
                   "style relational recall, cap 2 hops), fused as an additive "
                   "RRF leg. Independent of --graph; default OFF. Injected "
                   "neighbours pass the SAME sensitivity gate as every hit.")
@click.option("--ollama-url", default="http://localhost:11434")
def query_cmd(
    query_text: str, vault: Path, types: tuple[str, ...], max_sensitivity: str | None,
    project: str | None, path_prefix: str | None, exclude_path_prefix: str | None,
    limit: int, synthesise: bool, explain: bool, rerank: bool, graph: bool,
    graph_expand: bool, ollama_url: str,
) -> None:
    """Run a recall query. Default output: ranked sources. With --synthesise,
    runs map-reduce and prints a synthesised answer with citations."""
    client = OllamaClient(base_url=ollama_url)
    f = rtv.Filter(
        types=list(types) if types else None,
        # Default an OMITTED ceiling to "internal" — parity with the /api/recall
        # route. Click's `--max-sensitivity` Choice already constrains the value
        # to the valid set, so None (flag omitted) is the only gap: without this
        # it fell through to "no effective ceiling" and surfaced confidential /
        # MNPI notes. (#no-mnpi-to-cloud ceiling parity, was cited as §5.4 —
        # see #recall-ceiling-default.)
        sensitivity_max=max_sensitivity or "internal",
        project=project,
        path_prefix=path_prefix,
        exclude_path_prefix=exclude_path_prefix,
    )
    hits = rtv.query(query_text, vault_root=vault, client=client, filter_=f,
                     limit=limit, rerank=rerank, graph=graph,
                     graph_expand=graph_expand)

    if not hits:
        click.echo("No matches.")
        sys.exit(0)

    if synthesise:
        click.echo(f"Query: {query_text}\n")
        answer = syn.synthesise(query_text, hits, client=client, vault_root=vault)
        click.echo(answer)
        click.echo()
        click.echo("---")
        click.echo("Top-ranked sources:")
        for i, h in enumerate(hits[:10], 1):
            click.echo(f"  {i}. [[{h.path.removesuffix('.md')}]]  ({h.score:.3f})")
    else:
        click.echo(f"Top {len(hits)} hits for: {query_text}\n")
        for i, h in enumerate(hits, 1):
            click.echo(f"{i}. {h.path}  (score={h.score:.3f})")
            if explain:
                # Per-result RRF-score breakdown (#54-rrf). Raw channel
                # scores (vector cosine / fts normalised) explain WHY a
                # doc matched; the per-channel RRF contributions explain
                # how the fusion summed; importance/decay/contradiction
                # are the post-fusion multipliers. Older indexes
                # (no FTS5 / pre-#54b code path) leave the fields None.
                v, f, imp, dec = (
                    h.vector_score, h.fts_score, h.importance, h.expires_decay,
                )
                # FTS-only hits carry vector_score=None (no vector channel) — that
                # is NOT an old index; still show the FTS/RRF breakdown with
                # vector=n/a (Codex pass-2 SEV-3). Only the absence of the fusion
                # fields (f/imp/dec) means a genuinely pre-#54b index.
                if f is not None and imp is not None and dec is not None:
                    v_rrf = h.vector_rrf if h.vector_rrf is not None else 0.0
                    f_rrf = h.fts_rrf if h.fts_rrf is not None else 0.0
                    # #45 graph leg — g_rrf=0 + graph=n/a when the channel was
                    # off (no --graph) or this note was unreachable within the
                    # 2-hop cap. ``graph=dist N`` shows WHY it was boosted.
                    g_rrf = h.graph_rrf if h.graph_rrf is not None else 0.0
                    gstr = ("n/a" if h.graph_distance is None
                            else f"dist={h.graph_distance}")
                    # #45-expansion leg — the expand fragments are appended ONLY
                    # when --graph-expand was requested, so an off-path
                    # `--explain` line is BYTE-IDENTICAL to the pre-#45-expansion
                    # format (Codex SEV-2). When on, ``expand=dist N`` shows the
                    # note was surfaced relationally, off the lexical lanes.
                    gx_rrf = h.graph_expand_rrf if h.graph_expand_rrf is not None else 0.0
                    if graph_expand:
                        gxstr = ("n/a" if h.graph_expand_distance is None
                                 else f"dist={h.graph_expand_distance}")
                        expand_part = f" expand={gxstr}"
                        gx_sum = f"+gx={gx_rrf:.5f}"
                    else:
                        expand_part = ""
                        gx_sum = ""
                    rrf = (h.rrf_score if h.rrf_score is not None
                           else (v_rrf + f_rrf + g_rrf + gx_rrf))
                    cpen = (
                        h.contradiction_penalty
                        if h.contradiction_penalty is not None else 1.0
                    )
                    vstr = "n/a" if v is None else f"{v:.3f}"
                    # Source-tier multiplier — appended ONLY when non-neutral
                    # (tier ≠ 2 / ×1.0), so an untagged vault's --explain line
                    # stays byte-identical to the pre-tier format (the
                    # #45-expansion precedent: never change off-path output).
                    if (h.source_tier is not None
                            and h.tier_multiplier is not None
                            and h.source_tier != 2):
                        tier_part = (
                            f" tier={h.source_tier}"
                            f"(×{h.tier_multiplier:.2f})"
                        )
                    else:
                        tier_part = ""
                    click.echo(
                        f"   explain: vector={vstr} fts={f:.3f} graph={gstr}"
                        f"{expand_part}  "
                        f"rrf(v={v_rrf:.5f}+f={f_rrf:.5f}+g={g_rrf:.5f}"
                        f"{gx_sum}={rrf:.5f})  "
                        f"importance={imp}(×{imp / 3.0:.2f}) "
                        f"decay={dec:.2f} contradiction=×{cpen:.2f}"
                        f"{tier_part}  "
                        f"final={h.score:.5f}"
                    )
                else:
                    click.echo(
                        "   explain: (no component breakdown — "
                        "older index, run `recall index --rebuild`)"
                    )
            if h.tldr:
                click.echo(f"   tldr: {h.tldr[:160]}")


@main.command("health")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--ollama-url", default="http://localhost:11434")
def health_cmd(vault: Path, ollama_url: str) -> None:
    """Sanity check Ollama + index state.

    Exit codes: 0 healthy · 1 index unhealthy (last run had errors, or
    degraded lexical-only notes present) · 2 Ollama unreachable.
    """
    db = vault / idx.DEFAULT_INDEX_DIR / idx.DEFAULT_INDEX_DB
    click.echo(f"vault:        {vault}")
    click.echo(f"index db:     {db} ({'exists' if db.exists() else 'NOT BUILT — run `recall index`'})")
    unhealthy = False
    health = idx.index_health(db)
    if health["exists"]:
        import sqlite3
        conn = sqlite3.connect(str(db))
        models = [m[0] for m in conn.execute(
            "SELECT DISTINCT embedding_model FROM notes WHERE embedding_model IS NOT NULL"
        )]
        conn.close()
        click.echo(f"indexed notes: {health['notes']}")
        click.echo(f"embedding models in index: {models}")
        # #recall-embed-context-overflow — a degraded note is visible to the
        # FTS lane only (embed failed at index time); errors mean notes that
        # are MISSING from the index entirely. Both demand attention.
        if health["degraded"]:
            unhealthy = True
            click.echo(f"DEGRADED (lexical-only, no embedding): {health['degraded']} note(s)")
            for p in health["degraded_paths"][:20]:
                click.echo(f"  - {p}")
            if health["degraded"] > 20:
                click.echo(f"  ... and {health['degraded'] - 20} more")
        last = health["last_run"]
        if last is not None:
            click.echo(
                f"last index run: {last.get('run_at', '?')} "
                f"(errors={last.get('errors', 0)} "
                f"degraded={last.get('embed_degraded', 0)} "
                f"chunk_errors={last.get('chunk_errors', 0)} "
                f"salvaged={last.get('fm_salvaged', 0)})"
            )
            if last.get("errors"):
                unhealthy = True
                click.echo(f"ERRORS in last run: {last['errors']} note(s) NOT indexed")
                for p in last.get("error_paths", [])[:20]:
                    click.echo(f"  - {p}")
            if last.get("fm_salvaged"):
                # Informational, not unhealthy — the note IS retrievable;
                # the YAML still wants fixing at source.
                click.echo(
                    f"frontmatter salvaged (malformed YAML — fix at source): "
                    f"{last['fm_salvaged']} note(s)"
                )
                for p in last.get("salvaged_paths", [])[:20]:
                    click.echo(f"  - {p}")
    client = OllamaClient(base_url=ollama_url)
    try:
        h = client.health()
        click.echo(f"ollama:       v{h['version']}, models: {', '.join(h['models'])}")
    except OllamaError as e:
        click.echo(f"OLLAMA NOT REACHABLE: {e}")
        sys.exit(2)
    if unhealthy:
        sys.exit(1)


if __name__ == "__main__":
    main()
