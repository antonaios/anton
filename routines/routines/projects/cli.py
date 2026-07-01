"""CLI for the projects routines.

Subcommands:

    actions-decay scan        Walk all projects; list overdue + stale actions
    actions-decay scan --format json
    actions-decay scan --format brief    (markdown for morning brief)

Used by the daily 06:45 cron, downstream of bd-decay (which runs 06:30).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date as date_cls
from pathlib import Path

import click

from routines.projects.decay import format_for_morning_brief, scan as decay_scan
from routines.shared import audit

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Projects routines — Open Actions decay sweep."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("scan")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--today", "today_str", default=None,
              help="Override today's date (ISO YYYY-MM-DD). For testing.")
@click.option("--format", "output_format", default="summary",
              type=click.Choice(["summary", "json", "brief"]),
              help="'brief' = morning-brief markdown; 'summary' = one line per item; 'json' = full data")
def cmd_scan(vault, today_str, output_format):
    """Walk all projects; list overdue + stale actions."""
    today = date_cls.fromisoformat(today_str) if today_str else date_cls.today()
    run_id = audit.new_run_id()

    sweep = decay_scan(vault, today=today)

    audit.write_structured(
        actor={"type": "system", "id": "routine:actions-decay"},
        entity_type="workspace",
        entity_id=run_id,
        action="scan",
        routine="actions-decay",
        run_id=run_id,
        status="ok",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"vault": str(vault), "today": today.isoformat()},
        outputs={
            "projects_scanned": len(sweep.projects_scanned),
            "overdue_count": len(sweep.overdue),
            "stale_count": len(sweep.stale),
        },
        episodic_source="Projects/**/*.md + Companies/*.md (per project)",
        semantic_target=None,
    )

    if output_format == "json":
        click.echo(json.dumps({
            "projects_scanned": sweep.projects_scanned,
            "overdue": [dataclasses.asdict(a) for a in sweep.overdue],
            "stale": [dataclasses.asdict(a) for a in sweep.stale],
        }, indent=2))
        return

    if output_format == "brief":
        out = format_for_morning_brief(sweep)
        click.echo(out if out else "_No decayed actions._")
        return

    # summary (default)
    if not sweep.overdue and not sweep.stale:
        click.echo(f"No decayed actions across {len(sweep.projects_scanned)} project(s).")
        return
    click.echo(
        f"{len(sweep.overdue)} overdue, {len(sweep.stale)} stale "
        f"across {len(sweep.projects_scanned)} project(s):"
    )
    for a in sweep.overdue:
        marker = " [URGENT]" if a.urgent else ""
        click.echo(f"  OVERDUE  [{a.project}]  {a.title}  (due {a.due}, owner {a.owner}){marker}")
    for a in sweep.stale:
        click.echo(f"  STALE    [{a.project}]  {a.title}  (owner {a.owner})")


if __name__ == "__main__":
    main()
