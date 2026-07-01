"""CLI for the stale-gate sweep (#steal-kocoro P3).

Subcommands:

    stale-gate run            Scan runs/ for runs stuck on a human gate; WARN
                              those past the warn threshold, fail-closed
                              auto-cancel those past the long horizon, and retire
                              crew runs orphaned by a bridge restart.
    stale-gate run --dry-run  Classify + report without writing.
    stale-gate show           Print the resolved thresholds.

Thresholds are env knobs (see ``routines/stale_gate/sweep.py``):
``AGENTIC_STALE_GATE_WARN_HOURS`` (default 6),
``AGENTIC_STALE_GATE_CANCEL_HOURS`` (default 168 = 7 days),
``AGENTIC_STALE_GATE_CREW_ORPHAN_HOURS`` (default 2).

The scheduler runs ``stale-gate run`` as a subprocess job (see
``routines/scheduler/jobs.py::_JOB_SPECS``), the same pattern as the other cron
routines, so a sweep crash can never take the bridge down.
"""

from __future__ import annotations

import json
import logging

import click

from routines.stale_gate import sweep


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Stale-gate sweep — retire runs stuck on a human-approval step."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--dry-run", is_flag=True, help="Classify + report without writing.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def run(dry_run: bool, fmt: str) -> None:
    """Run the stale-gate sweep over runs/composite.*.jsonl + runs/crew.*.jsonl."""
    result = sweep.run_sweep(dry_run=dry_run)
    if fmt == "json":
        click.echo(json.dumps(result.summary(), indent=2))
        return
    prefix = "[dry-run] " if dry_run else ""
    click.echo(
        f"{prefix}stale-gate: scanned={result.scanned} fresh={result.fresh} "
        f"warned={len(result.warned)} cancelled={len(result.cancelled)} "
        f"finalized={len(result.finalized)} errors={len(result.errors)}"
    )
    for g in result.warned:
        click.echo(
            f"{prefix}  WARN     {g.lane}:{g.key} run={g.run_id} "
            f"paused {g.hours_paused:.1f}h"
        )
    for g in result.cancelled:
        click.echo(
            f"{prefix}  CANCEL   {g.lane}:{g.key} run={g.run_id} "
            f"paused {g.hours_paused:.1f}h (>= horizon)"
        )
    for g in result.finalized:
        click.echo(
            f"{prefix}  FINALIZE {g.lane}:{g.key} run={g.run_id} "
            f"non-terminal {g.hours_paused:.1f}h (restart orphan)"
        )
    for err in result.errors:
        click.echo(f"{prefix}  ERROR    {err}", err=True)


@main.command()
def show() -> None:
    """Print the EFFECTIVE thresholds the sweep will use (with the cancel>warn
    clamp applied — so the operator never sees a horizon the sweep won't use)."""
    warn_h, cancel_h, crew_h = sweep.effective_thresholds()
    click.echo(f"{sweep.WARN_HOURS_ENV}:        {warn_h}h (warn)")
    click.echo(f"{sweep.CANCEL_HOURS_ENV}:      {cancel_h}h (auto-cancel horizon; clamped to > warn)")
    click.echo(f"{sweep.CREW_ORPHAN_HOURS_ENV}: {crew_h}h (crew orphan)")


if __name__ == "__main__":
    main()
