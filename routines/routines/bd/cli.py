"""CLI for the bd routine.

Subcommands:

    bd-decay scan       Walk Companies/, list stale BD entries
    bd-decay scan --format json
    bd-note <company> --state <state> --last-contact <date> --owner <slug>
                        (deferred — operator edits Companies/<X>.md directly today)

Used by the daily 06:30 cron (`bd-decay scan` → ingested by morning-brief).
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from pathlib import Path

import click

from routines.bd.decay import (
    DECAY_THRESHOLDS, format_stale_for_morning_brief, scan as decay_scan,
)
from routines.shared import audit

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """BD (business development) watch + decay routines."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("scan")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--format", "output_format", default="markdown",
              type=click.Choice(["markdown", "json", "summary"]),
              help="Output format. 'summary' = one line per entry.")
@click.option("--today", "today_str", default=None,
              help="Override today's date (ISO YYYY-MM-DD). For testing.")
def cmd_scan(vault, output_format, today_str):
    """Walk Companies/ and list stale BD entries."""
    today = date_cls.fromisoformat(today_str) if today_str else date_cls.today()
    run_id = audit.new_run_id()

    stale = decay_scan(vault, today=today)

    audit.write_structured(
        actor={"type": "system", "id": "routine:bd-decay"},
        entity_type="session",
        entity_id=run_id,
        action="scan",
        routine="bd-decay",
        run_id=run_id,
        status="ok",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"vault": str(vault), "today": today.isoformat()},
        outputs={"stale_count": len(stale),
                 "stale_paths": [s.company_path for s in stale[:20]]},
        episodic_source="Companies/*.md (bd_state + bd_last_contact frontmatter)",
        semantic_target=None,
    )

    if output_format == "json":
        click.echo(json.dumps([
            {
                "company_path": s.company_path,
                "company_name": s.company_name,
                "sector": s.sector,
                "bd_state": s.bd_state,
                "bd_last_contact": s.bd_last_contact,
                "bd_owner": s.bd_owner,
                "days_since_contact": s.days_since_contact,
                "threshold_days": s.threshold_days,
                "days_over": s.days_over,
            } for s in stale
        ], indent=2))
        return

    if output_format == "summary":
        if not stale:
            click.echo("No stale BD entries.")
            return
        click.echo(f"{len(stale)} stale BD entries:")
        for s in stale:
            click.echo(
                f"  {s.company_name:30s} {s.bd_state:10s} {s.days_since_contact}d (threshold {s.threshold_days}d, +{s.days_over}d)"
            )
        return

    # markdown (default)
    out = format_stale_for_morning_brief(stale)
    if out:
        click.echo(out)
    else:
        click.echo("_No stale BD entries._")


@main.command("thresholds")
def cmd_thresholds():
    """Show the per-state decay thresholds."""
    click.echo("BD decay thresholds (days):")
    for state, days in DECAY_THRESHOLDS.items():
        if days < 0:
            click.echo(f"  {state:12s}  sticky (never decays)")
        else:
            click.echo(f"  {state:12s}  {days}d")


if __name__ == "__main__":
    main()
