"""CLI for the week-in-review routine (#38).

    week-in-review generate [--start <date>]   — draft the past week's review

Default window is the 7 days ending today (the Monday cron reviews the
prior Mon-Sun week); ``--start`` picks an ad-hoc historical window. The
draft lands at ``<vault>/Resources/Week-in-Review/<YYYY-Www>.md`` unless
``--dry-run`` is passed.

Graceful degradation: if local Ollama is unreachable the routine still
writes every mechanical section and marks the two synthesised sections
``[LLM unavailable]`` — a model outage must never lose the week's data
(mirrors the morning-brief / daily-digest fallback discipline).
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths
from routines.week_in_review.collect import VAULT, collect_week, resolve_window
from routines.week_in_review.render import render_markdown, review_path, write_review

# Derive the default vault from the same platform base as the collector's
# source repos (see collect.VAULT) so output + source stay consistent on
# both Windows and WSL.
DEFAULT_VAULT = VAULT
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Week in review — weekly DRAFT generator via local Ollama."""
    # The draft markdown carries Unicode (· — etc). On Windows the console
    # defaults to cp1252, which would crash a --dry-run print; force UTF-8 so
    # the preview prints anywhere. (Writes to disk are already UTF-8.)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except (AttributeError, ValueError):  # not a reconfigurable stream
            pass
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("generate")
@click.option("--vault", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_VAULT,
              help="Vault root (draft lands under Resources/Week-in-Review/).")
@click.option("--start", "start_str", default=None,
              help="ISO start date for an ad-hoc 7-day window "
                   "(default: the 7 days ending today UTC).")
@click.option("--today", "today_str", default=None,
              help="Override 'today' (UTC) — for deterministic re-runs.")
@click.option("--ollama-url", default="http://127.0.0.1:11434")
@click.option("--model", default="qwen3:14b")
@click.option("--dry-run", is_flag=True,
              help="Print the draft to stdout instead of writing it.")
def generate_cmd(
    vault: Path, start_str: str | None, today_str: str | None,
    ollama_url: str, model: str, dry_run: bool,
) -> None:
    """Generate this week's review draft."""
    started = time.monotonic()
    today = (
        date_cls.fromisoformat(today_str) if today_str
        else datetime.now(timezone.utc).date()
    )
    start_override = date_cls.fromisoformat(start_str) if start_str else None
    start, until, label = resolve_window(today, start_override)
    run_id = audit.new_run_id()

    # Local Ollama is optional — degrade gracefully if it's down so the
    # mechanical sections still land. (Distinct from morning-brief, which
    # hard-exits: the week-in-review's mechanical data is too valuable to
    # drop on a model outage.)
    client: OllamaClient | None = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"warning: Ollama unreachable at {ollama_url} ({e}); "
                   "writing mechanical sections only.", err=True)
        client = None

    click.echo(f"collecting week {label} ({start.isoformat()} .. {until.isoformat()})...")
    ctx = collect_week(start=start, until=until, label=label)
    click.echo(f"  commits={ctx.total_commits} shipped={len(ctx.shipped)} "
               f"fires={ctx.total_fires} next_week={len(ctx.next_week)}")

    markdown = render_markdown(ctx, client=client, model=model)

    if dry_run:
        click.echo("\n── dry-run: week-in-review draft ──\n")
        click.echo(markdown)
        return

    path = write_review(VaultPaths(vault).root, label, markdown)
    click.echo(f"\nOK wrote {path}")

    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:week-in-review"},
        entity_type="vault_note",
        entity_id=str(path),
        action="run",
        routine="week-in-review", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={"week": label, "start": start.isoformat(), "until": until.isoformat()},
        outputs={
            "path": str(path),
            "commits": ctx.total_commits,
            "shipped": len(ctx.shipped),
            "routine_fires": ctx.total_fires,
            "llm_available": client is not None,
        },
        duration_ms=duration_ms,
    )


@main.command("show")
@click.option("--vault", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--week", "week_label", default=None,
              help="ISO week label, e.g. 2026-W22 (default: the week `generate` "
                   "last drafted — the prior 7-day window, NOT the current week).")
@click.option("--today", "today_str", default=None,
              help="Override 'today' (UTC) for the default-week lookup.")
def show_cmd(vault: Path, week_label: str | None, today_str: str | None) -> None:
    """Print a stored week-in-review draft."""
    if week_label is None:
        # Mirror generate's default window so `show` (no --week) finds the
        # draft `generate` (no --start) just wrote — both resolve the prior
        # 7-day window's label, not the current ISO week.
        today = (
            date_cls.fromisoformat(today_str) if today_str
            else datetime.now(timezone.utc).date()
        )
        _, _, week_label = resolve_window(today)
    path = review_path(VaultPaths(vault).root, week_label)
    if not path.exists():
        click.echo(f"No week-in-review for {week_label} (expected at {path}).")
        sys.exit(1)
    click.echo(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
