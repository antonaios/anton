"""CLI for vault health checks (Plan v3 §6.9 Phase 6).

Subcommands:

    vault-health freshness     Sweep sector claim files for stale entries
    vault-health links         Scan vault for orphan wikilinks
    vault-health speculation   Check `(speculation)` markers (stub)
    vault-health all           Run all checks (incl. constitution +
                               recall-index health); emit combined report

All output is a markdown report (default) or JSON (--format json). Reports
land at Routines/vault-health/<date>-<kind>.md with status: pending-review.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from pathlib import Path

import click

from routines.shared import audit
from routines.shared.vault_writer import atomic_write
from routines.vault_health import constitution, freshness, links, recall_index, speculation

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Vault health checks — decay defences."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("freshness")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--today", "today_str", default=None)
@click.option("--write/--no-write", default=True,
              help="Write report to Routines/vault-health/ (default: write).")
@click.option("--format", "output_format", default="markdown",
              type=click.Choice(["markdown", "json"]))
def cmd_freshness(vault, today_str, write, output_format):
    """Sector claim file freshness sweep."""
    today = date_cls.fromisoformat(today_str) if today_str else date_cls.today()
    run_id = audit.new_run_id()
    stale = freshness.scan(vault, today=today)

    audit.write_structured(
        actor={"type": "system", "id": "routine:vault-health-freshness"},
        entity_type="session",
        entity_id=run_id,
        action="scan",
        routine="vault-health-freshness",
        run_id=run_id,
        status="ok",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"vault": str(vault), "today": today.isoformat()},
        outputs={"stale_count": len(stale),
                 "auto_bump_count": sum(1 for s in stale if s.severity == "auto-bump"),
                 "warning_count": sum(1 for s in stale if s.severity == "warning")},
        episodic_source="Sectors/*/*.md (sector-claim files)",
        semantic_target=None,
    )

    if output_format == "json":
        click.echo(json.dumps([s.__dict__ for s in stale], indent=2))
        return

    report = freshness.render_report(stale, today=today)
    if write and stale:
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-freshness.md"
        atomic_write(report_path, report, vault_root=vault)
        click.echo(f"-> wrote {report_path.relative_to(vault)}")
    else:
        click.echo(report)


@main.command("links")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--write/--no-write", default=True)
@click.option("--format", "output_format", default="markdown",
              type=click.Choice(["markdown", "json"]))
def cmd_links(vault, write, output_format):
    """Orphan wikilink scan."""
    run_id = audit.new_run_id()
    orphans = links.scan(vault)

    audit.write_structured(
        actor={"type": "system", "id": "routine:vault-health-links"},
        entity_type="session",
        entity_id=run_id,
        action="scan",
        routine="vault-health-links",
        run_id=run_id,
        status="ok",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"vault": str(vault)},
        outputs={"orphan_count": len(orphans),
                 "affected_files": len({o.source_path for o in orphans})},
    )

    if output_format == "json":
        click.echo(json.dumps([o.__dict__ for o in orphans], indent=2))
        return

    report = links.render_report(orphans)
    if write and orphans:
        today = date_cls.today()
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-orphan-links.md"
        atomic_write(report_path, report, vault_root=vault)
        click.echo(f"-> wrote {report_path.relative_to(vault)} ({len(orphans)} orphan(s))")
    else:
        click.echo(report)


@main.command("speculation")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
def cmd_speculation(vault):
    """Speculation TTL scan (stub — not yet implemented)."""
    markers = speculation.scan(vault)
    click.echo(speculation.render_report(markers))


@main.command("constitution")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--format", "output_format", default="markdown",
              type=click.Choice(["markdown", "json"]))
def cmd_constitution(vault, output_format):
    """Constitution integrity check (#claudemd-restructure).

    Verifies root CLAUDE.md §4/§5 hashes + load-bearing anchors + the
    expected rule-file set against _claude/constitution-manifest.json.
    Read-only; exits 1 on any CRITICAL finding.
    """
    run_id = audit.new_run_id()
    findings = constitution.scan(vault)

    audit.write_structured(
        actor={"type": "system", "id": "routine:vault-health-constitution"},
        entity_type="session",
        entity_id=run_id,
        action="scan",
        routine="vault-health-constitution",
        run_id=run_id,
        status="ok" if not findings else "critical",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"vault": str(vault)},
        outputs={"finding_count": len(findings)},
    )

    if output_format == "json":
        click.echo(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        click.echo(constitution.render_report(findings))
    if findings:
        raise SystemExit(1)


@main.command("all")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
def cmd_all(vault):
    """Run all health checks. Used by weekly cron."""
    today = date_cls.today()

    # Freshness
    stale = freshness.scan(vault, today=today)
    click.echo(f"freshness: {len(stale)} stale claim file(s)")
    if stale:
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-freshness.md"
        atomic_write(report_path, freshness.render_report(stale, today=today), vault_root=vault)
        click.echo(f"  -> {report_path.relative_to(vault)}")

    # Links
    orphans = links.scan(vault)
    click.echo(f"links: {len(orphans)} orphan wikilink(s)")
    if orphans:
        report_path = vault / "Routines" / "vault-health" / f"{today.isoformat()}-orphan-links.md"
        atomic_write(report_path, links.render_report(orphans), vault_root=vault)
        click.echo(f"  -> {report_path.relative_to(vault)}")

    # Speculation (stub)
    click.echo("speculation: stub")

    # Constitution integrity (#claudemd-restructure) — loud, never written to
    # the vault; a CRITICAL here means §4/§5 changed without a manifest bump.
    findings = constitution.scan(vault)
    click.echo(f"constitution: {'GREEN' if not findings else f'{len(findings)} CRITICAL finding(s)'}")
    for f in findings:
        click.echo(f"  !! [{f.check}] {f.detail}")

    # Recall index health (#recall-embed-context-overflow) — notes the last
    # index run failed to index (absent from /recall) or wrote lexical-only
    # (no embedding). Read-only, no Ollama call.
    recall_findings = recall_index.scan(vault)
    click.echo(
        f"recall index: {'GREEN' if not recall_findings else f'{len(recall_findings)} WARNING finding(s)'}"
    )
    for f in recall_findings:
        click.echo(f"  !! [{f.check}] {f.detail}")


if __name__ == "__main__":
    main()
