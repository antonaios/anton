"""CLI for the sector-news routine."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from routines.sectornews.coverage import load_coverage
from routines.sectornews.firecrawl_client import FirecrawlClient, FirecrawlError
from routines.sectornews.pipeline import run_for_sector
from routines.sectornews.search_provider import (
    NoProviderConfiguredError, get_search_client,
)
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths


DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Sector newsletter routine."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("run")
@click.argument("sector")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--days", type=int, default=7)
@click.option("--limit", type=int, default=15)
@click.option("--provider", type=click.Choice(["auto", "firecrawl", "tavily"]), default="auto",
              help="'auto' tries Firecrawl, falls back to Tavily on auth error")
@click.option("--dry-run", is_flag=True, help="Don't write the newsletter file; log what would be written")
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--feed-deals/--no-feed-deals", default=True,
              help="Auto-feed M&A-looking items into the canonical precedent transactions tracker (default on)")
def run_cmd(sector: str, vault: Path, days: int, limit: int, provider: str, dry_run: bool,
            ollama_url: str, feed_deals: bool) -> None:
    """Run the pipeline for one sector or news-coverage row.

    e.g. `sector-news run Travel --days 14`. The name is first resolved
    against `_claude/news-coverage.md` rows (case-insensitive); a name
    with no coverage row falls back to the legacy `Sectors/<X>.md` path.
    """
    paths = VaultPaths(vault)
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fc = get_search_client(provider)
    except (FirecrawlError, NoProviderConfiguredError) as e:
        click.echo(f"Search provider setup failed: {e}", err=True)
        sys.exit(2)
    ollama = OllamaClient(base_url=ollama_url)

    entries, _src = load_coverage(vault)
    entry = next((e for e in entries if e.name.lower() == sector.lower()), None)
    if entry is not None:
        click.echo(f"coverage row: {entry.name}"
                   + (f" (sector: {entry.sector})" if entry.sector else " (standalone topic)"))

    result = run_for_sector(
        entry.name if entry is not None else sector,
        paths=paths, fc=fc, ollama=ollama,
        audit_dir=DEFAULT_AUDIT_DIR,
        days=days, fetch_limit=limit,
        dry_run=dry_run,
        feed_deals=feed_deals,
        coverage=entry,
    )
    if result.status == "ok":
        click.echo(f"OK   sector={result.sector}")
        click.echo(f"     items: fetched={result.items_fetched} "
                   f"deduped={result.items_deduped} scored={result.items_scored}")
        if feed_deals:
            click.echo(f"     deals: filtered={result.deals_filtered} "
                       f"appended={result.deals_appended} skipped={result.deals_skipped}")
        if result.output_path:
            click.echo(f"     wrote: {result.output_path}")
        click.echo(f"     duration: {result.duration_ms}ms")
    else:
        click.echo(f"ERROR  sector={result.sector}: {result.error}", err=True)
        sys.exit(1)


@main.command("run-all")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--days", type=int, default=7)
@click.option("--limit", type=int, default=15)
@click.option("--provider", type=click.Choice(["auto", "firecrawl", "tavily"]), default="auto")
@click.option("--dry-run", is_flag=True)
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--feed-deals/--no-feed-deals", default=True,
              help="Auto-feed M&A-looking items into the canonical precedent transactions tracker (default on)")
def run_all_cmd(vault: Path, days: int, limit: int, provider: str, dry_run: bool,
                ollama_url: str, feed_deals: bool) -> None:
    """Run the pipeline for every enabled row in `_claude/news-coverage.md`
    (#operator-tab §3a). When the coverage file is absent, the list is
    synthesised from profile.md `active_sectors:` — the pre-decoupling
    behaviour, so a missing file can't break the morning run.
    """
    paths = VaultPaths(vault)
    entries, src = load_coverage(vault)
    enabled = [e for e in entries if e.enabled]
    if not enabled:
        click.echo(
            "No enabled news-coverage rows (and no active_sectors to "
            "synthesise from); nothing to do.", err=True,
        )
        sys.exit(2)
    disabled = len(entries) - len(enabled)
    click.echo(
        f"coverage: {len(enabled)} enabled row(s) ({src})"
        + (f", {disabled} paused" if disabled else "")
    )

    try:
        fc = get_search_client(provider)
    except (FirecrawlError, NoProviderConfiguredError) as e:
        click.echo(f"Search provider setup failed: {e}", err=True)
        sys.exit(2)
    ollama = OllamaClient(base_url=ollama_url)
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    for entry in enabled:
        click.echo(f"\n--- {entry.name} ---")
        result = run_for_sector(
            entry.name, paths=paths, fc=fc, ollama=ollama,
            audit_dir=DEFAULT_AUDIT_DIR,
            days=days, fetch_limit=limit,
            dry_run=dry_run,
            feed_deals=feed_deals,
            coverage=entry,
        )
        if result.status == "ok":
            extra = ""
            if feed_deals and result.deals_filtered:
                extra = f" deals[+{result.deals_appended}/{result.deals_skipped}skip]"
            click.echo(f"OK  fetched={result.items_fetched} scored={result.items_scored}{extra} "
                       f"path={result.output_path or '(dry-run)'}")
        else:
            click.echo(f"ERR {result.error}")
            failed.append(entry.name)

    if failed:
        click.echo(f"\n{len(failed)} coverage row(s) failed: {failed}", err=True)
        sys.exit(1)


@main.command("health")
@click.option("--ollama-url", default="http://localhost:11434")
def health_cmd(ollama_url: str) -> None:
    """Sanity-check search providers + Ollama."""
    import os
    fck = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    tk_csv = os.environ.get("TAVILY_API_KEYS", "").strip()
    tk_single = os.environ.get("TAVILY_API_KEY", "").strip()
    tavily_keys = [k.strip() for k in tk_csv.split(",") if k.strip()] if tk_csv else (
        [tk_single] if tk_single else []
    )

    if fck:
        click.echo(f"FIRECRAWL_API_KEY: present (length {len(fck)})")
    else:
        click.echo("FIRECRAWL_API_KEY: NOT SET")

    if tavily_keys:
        click.echo(f"TAVILY: {len(tavily_keys)} key(s) configured "
                   f"(lengths: {[len(k) for k in tavily_keys]})")
    else:
        click.echo("TAVILY_API_KEY{,S}: NOT SET")

    if not (fck or tavily_keys):
        click.echo("\nERROR: no search provider configured. Set at least one of "
                   "FIRECRAWL_API_KEY / TAVILY_API_KEYS via Windows User-scope "
                   "`setx` (see HANDOFF.md §3).", err=True)
        sys.exit(2)

    try:
        client = get_search_client("auto")
        click.echo(f"Search client (auto): instantiated as {type(client).__name__}")
    except Exception as e:  # noqa: BLE001
        click.echo(f"Search client failed: {e}", err=True)
        sys.exit(2)

    try:
        h = OllamaClient(base_url=ollama_url).health()
        click.echo(f"Ollama: v{h['version']}, models: {', '.join(h['models'])}")
    except OllamaError as e:
        click.echo(f"Ollama not reachable: {e}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
