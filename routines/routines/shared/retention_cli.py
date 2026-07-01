"""CLI for the audit/telemetry retention pass (#ops-retention).

Subcommands:

    retention run         Prune runs/*.jsonl + telemetry + audit_index.db
                          older than the retention window, then VACUUM.
    retention run --dry-run
                          Report what WOULD be pruned without writing.
    retention show        Print the resolved window + target paths.

The retention window is a SINGLE knob: the ``AGENTIC_RETENTION_DAYS`` env
var (default 90). Prune is AGE-ONLY and crash-safe — see
``routines/shared/retention.py`` for the safety contract.

The scheduler runs ``retention run`` weekly as a subprocess job (see
``routines/scheduler/jobs.py::_JOB_SPECS``), the same pattern as the other
cron routines, so a retention crash can never take the bridge down.
"""

from __future__ import annotations

import json
import logging

import click

from routines.shared import retention


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Audit/telemetry retention — bounded disk growth."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option(
    "--days", type=click.IntRange(min=1), default=None,
    help="Override the retention window in days, >= 1 "
    "(default: AGENTIC_RETENTION_DAYS or 90). A non-positive value would "
    "prune everything and is rejected.",
)
@click.option("--dry-run", is_flag=True, help="Report without writing.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def run(days: int | None, dry_run: bool, fmt: str) -> None:
    """Run the retention pass over runs/telemetry JSONL + audit_index.db."""
    summary = retention.run_retention(days=days, dry_run=dry_run)
    if fmt == "json":
        click.echo(json.dumps(summary.as_dict(), indent=2))
        return
    prefix = "[dry-run] " if dry_run else ""
    click.echo(
        f"{prefix}retention window={summary.days}d cutoff={summary.cutoff_iso}"
    )
    click.echo(
        f"{prefix}jsonl: {summary.total_lines_pruned()} line(s) pruned across "
        f"{len(summary.jsonl)} file(s)"
    )
    for r in summary.jsonl:
        if r.pruned or r.error:
            note = f" ERROR: {r.error}" if r.error else ""
            click.echo(
                f"  - {r.path}: -{r.pruned} (kept {r.kept}, "
                f"undated-kept {r.undated_kept}){note}"
            )
    for s in summary.sqlite:
        note = f" ERROR: {s.error}" if s.error else ""
        click.echo(
            f"{prefix}sqlite: {s.db_path}: -{s.deleted} row(s), "
            f"vacuumed={s.vacuumed}{note}"
        )


@main.command()
def show() -> None:
    """Print the resolved retention window + target paths."""
    click.echo(f"AGENTIC_RETENTION_DAYS resolves to: {retention.retention_days()} days")
    click.echo(f"runs dir:        {retention._default_runs_dir()}")
    click.echo(f"telemetry jsonl: {retention._default_telemetry_jsonl()}")
    click.echo(f"audit_index.db:  {retention._default_audit_db_path()}")


if __name__ == "__main__":
    main()
