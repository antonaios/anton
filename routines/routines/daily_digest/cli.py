"""CLI for the daily digest routine.

Two commands, parallel to morning-brief:

    daily-digest generate    — gather today's activity, write the digest
    daily-digest show        — print the stored digest for today / --date
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.daily_digest.pull import gather_context
from routines.daily_digest.reader import load_for_date
from routines.daily_digest.schema import DailyDigest
from routines.daily_digest.synthesise import (
    anton_closes, routine_rows, vault_rows,
)
from routines.daily_digest.writer import digest_path, write_digest
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.profile import load as load_profile
from routines.shared.vault_writer import VaultPaths

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Daily digest — end-of-day wrap-up via local Ollama."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("generate")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--date", "date_str", default=None,
              help="ISO date for the digest (default: today UTC)")
@click.option("--max-writes", type=int, default=20, show_default=True,
              help="Cap on vault writes surfaced in the digest")
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--model", default="qwen3:14b")
@click.option("--dry-run", is_flag=True,
              help="Print the digest to stdout instead of writing it")
def generate_cmd(
    vault: Path, date_str: str | None, max_writes: int,
    ollama_url: str, model: str, dry_run: bool,
) -> None:
    """Generate today's digest and write to Routines/daily-digests/."""
    started = time.monotonic()
    the_date = (
        date_cls.fromisoformat(date_str) if date_str
        else datetime.now(timezone.utc).date()
    )
    paths = VaultPaths(vault)
    run_id = audit.new_run_id()

    try:
        profile = load_profile(vault)
        profile_context = (
            f"Operator: {profile.operator or 'unknown'}. "
            f"Active sectors: {', '.join(profile.active_sectors or []) or 'none'}."
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not load profile: {e}", err=True)
        profile_context = ""

    client = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"Ollama unreachable at {ollama_url}: {e}", err=True)
        sys.exit(2)

    click.echo(f"gathering context (vault={vault}, runs={DEFAULT_AUDIT_DIR})...")
    ctx = gather_context(
        paths.root, DEFAULT_AUDIT_DIR,
        today=the_date, max_writes=max_writes,
    )
    click.echo(f"  routines={len(ctx.routines)} vault_writes={len(ctx.vault_writes)}")

    click.echo("composing 'Anton closes'...")
    closes = anton_closes(
        ctx, profile_context=profile_context, client=client, model=model,
    )
    click.echo(f"  -> {len(closes)} chars")

    full_date = the_date.strftime("%a · %d %b %Y · UTC")
    digest = DailyDigest(
        date=full_date,
        source=f"Generated · Local Ollama {model}",
        activity=routine_rows(ctx.routines),
        vaultChanges=vault_rows(ctx.vault_writes),
        antonCloses=closes,
    )

    if dry_run:
        click.echo("\n-- dry-run: digest preview --\n")
        click.echo(f"# Daily Digest · {digest.date}\n")
        if digest.activity:
            click.echo("Routines:")
            for r in digest.activity:
                click.echo(f"  - {r.text} — {r.sub}")
        if digest.vaultChanges:
            click.echo("\nVault writes:")
            for r in digest.vaultChanges:
                click.echo(f"  - {r.text} — {r.sub}")
        click.echo("\nAnton closes:")
        click.echo(f"  {digest.antonCloses}")
        return

    path = write_digest(paths.root, digest, the_date)
    click.echo(f"\nOK wrote {path}")

    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:daily-digest"},
        entity_type="vault_note",
        entity_id=str(path),
        action="run",
        routine="daily-digest", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={"date": the_date.isoformat(), "max_writes": max_writes},
        outputs={
            "path": str(path),
            "routine_rows": len(digest.activity),
            "vault_writes": len(digest.vaultChanges),
            "anton_closes_chars": len(closes),
        },
        duration_ms=duration_ms,
    )


@main.command("show")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--date", "date_str", default=None)
def show_cmd(vault: Path, date_str: str | None) -> None:
    """Print today's digest (or one for --date)."""
    the_date = (
        date_cls.fromisoformat(date_str) if date_str
        else datetime.now(timezone.utc).date()
    )
    paths = VaultPaths(vault)
    digest = load_for_date(paths.root, the_date)
    if digest is None:
        path = digest_path(paths.root, the_date)
        click.echo(f"No digest for {the_date.isoformat()} (expected at {path}).")
        sys.exit(1)

    click.echo(f"# Daily Digest · {digest.date}")
    click.echo(f"_{digest.source}_\n")
    if digest.activity:
        click.echo("-- Routines today --")
        for r in digest.activity:
            click.echo(f"- {r.text} -- {r.sub}")
    if digest.vaultChanges:
        click.echo("\n-- Vault writes --")
        for r in digest.vaultChanges:
            click.echo(f"- {r.text} -- {r.sub}")
    click.echo(f"\n{digest.antonCloses}")


if __name__ == "__main__":
    main()
