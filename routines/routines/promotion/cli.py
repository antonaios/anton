"""CLI for memory-promote routine."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import click

from routines.promotion.propose import render_proposal
from routines.promotion.scan import scan_project
from routines.shared import audit
from routines.shared.vault_writer import VaultPaths, atomic_write


DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Memory promotion / compaction routine."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("run")
@click.argument("project")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--stale-days", type=int, default=30,
              help="Open actions older than this in meeting notes are flagged stale")
def run_cmd(project: str, vault: Path, stale_days: int) -> None:
    """Scan one project; write a proposal file to Routines/memory-promotion/."""
    paths = VaultPaths(vault)
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = audit.new_run_id()

    scan = scan_project(project, paths=paths, stale_threshold_days=stale_days)
    proposal_md = render_proposal(scan, run_id=run_id)

    out_dir = paths.routines / "memory-promotion"
    out_path = out_dir / f"{date.today().isoformat()}-{project}-{run_id}.md"
    atomic_write(out_path, proposal_md, vault_root=paths.root)

    audit.write_structured(
        actor={"type": "system", "id": "routine:memory-promote"},
        entity_type="proposal",
        entity_id=project,
        action="propose",
        routine="memory-promote", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={"project": project, "stale_days": stale_days},
        outputs={
            "proposal_path": str(out_path),
            "duplicate_decisions": len(scan.duplicate_decisions),
            "stale_actions": len(scan.stale_actions),
            "lesson_candidates": len(scan.lesson_candidates),
            "is_empty": scan.is_empty,
        },
        # Lane transition: episodic → semantic. Reads project meeting notes
        # and decision log (episodic), proposes additions to the project's
        # durable knowledge (semantic).
        episodic_source=f"Projects/{project}/02 Meeting Notes/ + 09 Decision Log.md",
        semantic_target=f"Projects/{project}/ (proposal at {out_path.name})",
    )

    click.echo(f"OK   project={project}")
    click.echo(f"     duplicate decisions: {len(scan.duplicate_decisions)}")
    click.echo(f"     stale actions:       {len(scan.stale_actions)}")
    click.echo(f"     lesson candidates:   {len(scan.lesson_candidates)}")
    click.echo(f"     wrote: {out_path}")


@main.command("run-all")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--stale-days", type=int, default=30)
def run_all_cmd(vault: Path, stale_days: int) -> None:
    """Scan every active project (excluding _template, _Trackers)."""
    paths = VaultPaths(vault)
    projects = paths.list_projects()
    if not projects:
        click.echo("No active projects found.")
        sys.exit(0)
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    for project in projects:
        run_id = audit.new_run_id()
        scan = scan_project(project, paths=paths, stale_threshold_days=stale_days)
        proposal_md = render_proposal(scan, run_id=run_id)
        out_path = paths.routines / "memory-promotion" / f"{date.today().isoformat()}-{project}-{run_id}.md"
        atomic_write(out_path, proposal_md, vault_root=paths.root)

        audit.write_structured(
            actor={"type": "system", "id": "routine:memory-promote"},
            entity_type="proposal",
            entity_id=project,
            action="propose",
            routine="memory-promote", run_id=run_id, status="ok",
            audit_dir=DEFAULT_AUDIT_DIR,
            inputs={"project": project, "stale_days": stale_days},
            outputs={
                "proposal_path": str(out_path),
                "duplicate_decisions": len(scan.duplicate_decisions),
                "stale_actions": len(scan.stale_actions),
                "lesson_candidates": len(scan.lesson_candidates),
                "is_empty": scan.is_empty,
            },
        )
        marker = "(empty)" if scan.is_empty else f"D{len(scan.duplicate_decisions)} S{len(scan.stale_actions)} L{len(scan.lesson_candidates)}"
        click.echo(f"  {project}: {marker} -> {out_path.name}")


if __name__ == "__main__":
    main()
