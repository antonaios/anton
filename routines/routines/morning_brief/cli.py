"""CLI for the morning brief routine."""

from __future__ import annotations

import logging
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.morning_brief.pull import gather_context
from routines.morning_brief.reader import load_for_date
from routines.morning_brief.schema import MorningBrief
from routines.morning_brief.synthesise import anton_suggests, classify_actions
from routines.morning_brief.writer import brief_path, write_brief
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.profile import load as load_profile
from routines.shared.vault_writer import VaultPaths

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Morning brief — auto-generated daily summary via local Ollama."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("generate")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--date", "date_str", default=None,
              help="ISO date for the brief (default: today UTC)")
@click.option("--days-lookback", type=int, default=7,
              help="Days back to scan for actions")
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--model", default="qwen3:14b")
@click.option("--dry-run", is_flag=True,
              help="Print the brief to stdout instead of writing it")
def generate_cmd(
    vault: Path, date_str: str | None, days_lookback: int,
    ollama_url: str, model: str, dry_run: bool,
) -> None:
    """Generate today's morning brief and write to Routines/morning-briefs/."""
    started = time.monotonic()
    the_date = (
        date_cls.fromisoformat(date_str) if date_str
        else datetime.now(timezone.utc).date()
    )
    paths = VaultPaths(vault)
    run_id = audit.new_run_id()

    # Load profile for active sectors + voice context.
    try:
        profile = load_profile(vault)
        active_sectors = profile.active_sectors or []
        profile_context = (
            f"Operator: {profile.operator or 'unknown'}. "
            f"Active sectors: {', '.join(active_sectors) or 'none'}."
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not load profile: {e}", err=True)
        active_sectors = []
        profile_context = ""

    client = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"Ollama unreachable at {ollama_url}: {e}", err=True)
        sys.exit(2)

    # 1) Gather
    click.echo(f"gathering context (vault={vault}, days_lookback={days_lookback})...")
    ctx = gather_context(
        paths.root, today=the_date, days_lookback=days_lookback,
        active_sectors=active_sectors,
    )
    click.echo(f"  actions={len(ctx.needs_you)} sector_news={len(ctx.sector_news)}")

    # 2) Classify actions via LLM
    click.echo("classifying actions...")
    needs_you = classify_actions(ctx.needs_you, today=the_date, client=client, model=model)
    click.echo(f"  -> {len(needs_you)} brief rows")

    # 3) Anton's suggestion
    click.echo("composing 'Anton suggests'...")
    suggests = anton_suggests(
        needs_you, ctx.sector_news,
        profile_context=profile_context,
        client=client, model=model,
    )
    click.echo(f"  -> {len(suggests)} chars")

    # Assemble
    weekday = the_date.strftime("%a")
    full_date = the_date.strftime("%a · %d %b %Y · UTC")
    brief = MorningBrief(
        date=full_date,
        source=f"Generated · Local Ollama {model}",
        needsYou=needs_you,
        sectorThisWeek=ctx.sector_news,
        antonSuggests=suggests,
    )
    _ = weekday  # silence linter

    if dry_run:
        click.echo("\n── dry-run: brief preview ──\n")
        click.echo(f"# Morning Brief · {brief.date}\n")
        for r in brief.needsYou:
            click.echo(f"  [{r.marker.upper()}] {r.text} — {r.sub}")
        if brief.sectorThisWeek:
            click.echo("\n  ── Sector this week ──")
            for r in brief.sectorThisWeek:
                click.echo(f"  - {r.text} ({r.sub})")
        click.echo("\n  ── Anton suggests ──")
        click.echo(f"  {brief.antonSuggests}")
        return

    # 4) Write
    path = write_brief(paths.root, brief, the_date)
    click.echo(f"\nOK wrote {path}")

    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:morning-brief"},
        entity_type="vault_note",
        entity_id=str(path),
        action="run",
        routine="morning-brief", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={"date": the_date.isoformat(), "days_lookback": days_lookback},
        outputs={
            "path": str(path),
            "needs_you_count": len(needs_you),
            "sector_news_count": len(ctx.sector_news),
            "anton_suggests_chars": len(suggests),
        },
        duration_ms=duration_ms,
    )


@main.command("show")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--date", "date_str", default=None)
def show_cmd(vault: Path, date_str: str | None) -> None:
    """Print today's morning brief (or one for --date)."""
    the_date = (
        date_cls.fromisoformat(date_str) if date_str
        else datetime.now(timezone.utc).date()
    )
    paths = VaultPaths(vault)
    brief = load_for_date(paths.root, the_date)
    if brief is None:
        path = brief_path(paths.root, the_date)
        click.echo(f"No brief for {the_date.isoformat()} (expected at {path}).")
        sys.exit(1)

    click.echo(f"# Morning Brief · {brief.date}")
    click.echo(f"_{brief.source}_\n")
    for r in brief.needsYou:
        click.echo(f"[{r.marker.upper()}] {r.text} — {r.sub}")
    if brief.sectorThisWeek:
        click.echo("\n── Sector this week ──")
        for r in brief.sectorThisWeek:
            click.echo(f"- {r.text} ({r.sub})")
    click.echo(f"\n{brief.antonSuggests}")


if __name__ == "__main__":
    main()
