"""CLI — ``python -m routines.learning.system_insights.cli analyse [...]``.

One command: ``analyse``. Runs the Dream Cycle Phase 5 pipeline
(readers → analyse_window → writer), honouring ``--window-days`` and
``--dry-run``. The bridge subprocesses this from the registered
``system-insights`` cron job in ``routines/scheduler/jobs.py``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.api.deps import RUNS_DIR, VAULT
from routines.learning.system_insights.job import run as run_pipeline
from routines.shared import audit


@click.group()
@click.option("--debug", is_flag=True, help="Verbose logging")
def main(debug: bool) -> None:
    """Dream Cycle Phase 5 — system self-reflection (#73)."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("analyse")
@click.option(
    "--vault",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Vault root (default: VAULT from routines.api.deps)",
)
@click.option(
    "--window-days",
    type=int,
    default=7,
    show_default=True,
    help="Lookback window in days",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print proposals to stdout without writing files",
)
def analyse_cmd(
    vault: Path | None, window_days: int, dry_run: bool,
) -> None:
    """Read telemetry, surface insights, write operator-gated proposals."""
    now = datetime.now(timezone.utc)
    vault_root = vault or VAULT
    run_id = audit.new_run_id()

    try:
        summary = run_pipeline(
            vault_root=vault_root,
            window_days=window_days,
            now=now,
            dry_run=dry_run,
        )
    except Exception as e:  # noqa: BLE001 — surface + audit
        audit.write_structured(
            actor={"type": "system", "id": "routine:system-insights"},
            entity_type="vault_note",
            entity_id=run_id,
            action="run",
            routine="system-insights",
            run_id=run_id,
            status="error",
            audit_dir=RUNS_DIR,
            inputs={
                "window_days": window_days,
                "dry_run": dry_run,
                "vault": str(vault_root),
            },
            error=f"{type(e).__name__}: {e}",
        )
        click.echo(f"system_insights: analyse failed: {e}", err=True)
        sys.exit(2)

    click.echo(json.dumps(summary, indent=2, default=str))

    audit.write_structured(
        actor={"type": "system", "id": "routine:system-insights"},
        entity_type="vault_note",
        entity_id=run_id,
        action="run",
        routine="system-insights",
        run_id=run_id,
        status="ok",
        audit_dir=RUNS_DIR,
        inputs={
            "window_days": window_days,
            "dry_run": dry_run,
            "vault": str(vault_root),
        },
        outputs={
            "proposals_total": summary["proposals_total"],
            "written": summary["written"],
            "skipped": summary["skipped"],
        },
    )


if __name__ == "__main__":
    main()
