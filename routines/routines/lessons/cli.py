"""CLI for the cross-project lessons routine.

Two commands:

    lessons-learned scan
        Walk every project's 13 Lessons Learned.md, extract operator-
        flagged patterns + cluster cross-project themes, write a
        proposal at Routines/lessons-learned/<date>-cross-project.md.

    lessons-learned suggest --project <DEAL>
        Deterministic match of Registers/Lessons.md entries against
        the target project's industry / sector / subsector (read from
        the project's 00 Brief.md frontmatter). Prints markdown bullets
        ready to paste into §6 "From prior similar deals" of the brief.
        No LLM call.

No scheduled task ships yet — install one once 2-3 closed projects
exist and the cross-project mode starts producing real signal.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.lessons.pull import gather_lessons
from routines.lessons.schema import LessonsProposal
from routines.lessons.suggest import (
    render_brief_bullets, suggest, suggest_for_project,
)
from routines.lessons.synthesise import (
    cluster_lessons, label_cluster, label_pattern,
)
from routines.lessons.writer import proposal_path, write_proposal
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Cross-project Lessons Learned — semantic memory across deals."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("scan")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--date", "date_str", default=None,
              help="ISO date for the proposal (default: today UTC)")
@click.option("--min-cluster-size", type=int, default=2, show_default=True)
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--model", default="qwen3:14b")
@click.option("--skip-llm", is_flag=True,
              help="Skip LLM labelling — emit raw patterns + clusters only")
def scan_cmd(
    vault: Path, date_str: str | None, min_cluster_size: int,
    ollama_url: str, model: str, skip_llm: bool,
) -> None:
    """Walk projects, cluster lessons, write the proposal."""
    started = time.monotonic()
    the_date = (
        date_cls.fromisoformat(date_str) if date_str
        else datetime.now(timezone.utc).date()
    )
    paths = VaultPaths(vault)
    run_id = audit.new_run_id()

    click.echo(f"scanning lessons under {vault}...")
    bundle = gather_lessons(paths.root)
    click.echo(
        f"  projects={len(bundle.projects_scanned)} "
        f"(closed={bundle.closed_count}, open={bundle.open_count}) "
        f"items={len(bundle.items)} patterns={len(bundle.patterns)}"
    )

    client: OllamaClient | None = None
    if not skip_llm and (bundle.patterns or bundle.items):
        client = OllamaClient(base_url=ollama_url)
        try:
            client.health()
        except OllamaError as e:
            click.echo(f"warning: Ollama unreachable ({e}); falling back to --skip-llm", err=True)
            client = None

    # Mode A: label each flagged pattern (single-project, high confidence)
    pattern_entries: list[str] = []
    if client and bundle.patterns:
        click.echo(f"labelling {len(bundle.patterns)} flagged pattern(s)...")
        for p in bundle.patterns:
            entry = label_pattern(p, client=client, model=model)
            if entry:
                pattern_entries.append(entry)

    # Mode B: cluster across projects (≥ 2 distinct projects required)
    click.echo("clustering cross-project lessons...")
    clusters = cluster_lessons(bundle.items, min_cluster_size=min_cluster_size)
    click.echo(f"  -> {len(clusters)} cross-project cluster(s)")
    if client and clusters:
        click.echo("labelling clusters...")
        for c in clusters:
            label_cluster(c, client=client, model=model)

    doc = LessonsProposal(
        generated_at=datetime.now(timezone.utc),
        patterns=bundle.patterns,
        clusters=clusters,
        projects_scanned=bundle.projects_scanned,
        closed_count=bundle.closed_count,
        open_count=bundle.open_count,
    )

    path = write_proposal(paths.root, doc, the_date)
    # Append per-pattern register entries to the proposal in a code block so
    # the operator can paste them straight into Registers/Lessons.md.
    if pattern_entries:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n## Proposed Registers/Lessons.md entries (Mode A)\n\n")
            for entry in pattern_entries:
                f.write("```markdown\n")
                f.write(entry.rstrip())
                f.write("\n```\n\n")

    click.echo(f"\nOK wrote {path}")
    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:lessons"},
        entity_type="vault_note",
        entity_id=str(path),
        action="run",
        routine="lessons", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={
            "vault": str(vault),
            "min_cluster_size": min_cluster_size,
            "skip_llm": skip_llm,
            "projects": bundle.projects_scanned,
        },
        outputs={
            "proposal_path": str(path),
            "patterns": len(bundle.patterns),
            "clusters": len(clusters),
            "pattern_entries_rendered": len(pattern_entries),
        },
        duration_ms=duration_ms,
        # Phase B lane fields: reads project Lessons files (semantic-ish
        # per-project knowledge), proposes additions to the cross-project
        # register (semantic). Audit records both ends.
        semantic_target=f"Registers/Lessons.md (proposal at {path.name})",
    )


# ── suggest (watch-outs auto-suggest for the project-brief §6) ────────────


@main.command("suggest")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--project", "project_name", default=None,
              help="Project name under Projects/<X>/; reads sector/subsector/industry "
                   "from its 00 Brief.md frontmatter.")
@click.option("--industry", default=None,
              help="Direct override (alternative to --project).")
@click.option("--sector", default=None,
              help="Direct override (alternative to --project). Accepts either "
                   "'Telecoms' or '[[Sectors/Telecoms]]'.")
@click.option("--subsector", default=None,
              help="Direct override (alternative to --project).")
@click.option("--limit", type=int, default=10, show_default=True,
              help="Cap on number of suggestions returned.")
@click.option("--format", "out_format",
              type=click.Choice(["bullets", "verbose"]),
              default="bullets", show_default=True,
              help="bullets: paste-ready markdown for the brief §6. "
                   "verbose: includes match reason + score.")
def suggest_cmd(
    vault: Path, project_name: str | None,
    industry: str | None, sector: str | None, subsector: str | None,
    limit: int, out_format: str,
) -> None:
    """Match Registers/Lessons.md entries against a project's sector context.

    Deterministic — no LLM call. Either provide --project (reads the
    brief's frontmatter) or pass --sector / --subsector / --industry
    directly. Output is markdown bullets ready to paste into §6
    "From prior similar deals" of the brief, or verbose with scores.
    """
    if not project_name and not (industry or sector or subsector):
        click.echo("error: pass --project OR at least one of --industry / --sector / --subsector", err=True)
        sys.exit(2)

    paths = VaultPaths(vault)
    try:
        if project_name:
            suggestions = suggest_for_project(paths.root, project_name, limit=limit)
        else:
            suggestions = suggest(
                paths.root,
                industry=industry, sector=sector, subsector=subsector,
                limit=limit,
            )
    except FileNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)

    if not suggestions:
        click.echo("(no matching lessons in Registers/Lessons.md yet — leave §6 'From prior similar deals' empty or populate manually as deals close)")
        return

    if out_format == "bullets":
        click.echo(render_brief_bullets(suggestions))
        return

    # verbose
    click.echo(f"{len(suggestions)} suggestion(s):\n")
    for s in suggestions:
        click.echo(f"[score {s.score}] {s.lesson.slug}")
        click.echo(f"    title:  {s.lesson.title}")
        click.echo(f"    reason: {s.reason}")
        click.echo(f"    paste:  - {s.lesson.title.rstrip('. ')} -> [[Registers/Lessons#{s.lesson.slug}]]")
        click.echo("")


if __name__ == "__main__":
    main()
