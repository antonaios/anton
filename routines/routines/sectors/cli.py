"""CLI for the sector extraction routine.

Subcommands:

    sector-extract from-projects     [--sector <X> | --all-active] [--vault PATH] [--since YYYY-MM-DD]
    sector-extract from-newsletters
    sector-extract from-meetings
    sector-extract from-research
    sector-extract from-bd
    sector-extract all               (runs all five extractors)
    sector-note <text> --sector <X> --claim-type <T> --source <citation>

The `all` form is what the daily 02:00 cron fires. Individual extractors
are useful for ad-hoc runs and testing.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.sectors import schema, synthesize as syn_mod, views as views_mod
from routines.sectors.extractors import (
    from_bd, from_meetings, from_newsletters, from_projects, from_research,
)
from routines.sectors.schema import SectorExtract, SectorProposal
from routines.sectors.writer import proposal_path, write_proposal
from routines.shared import audit
from routines.shared.vault_writer import VaultPaths, atomic_write

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")

_EXTRACTOR_MAP = {
    "from-projects": from_projects.gather,
    "from-newsletters": from_newsletters.gather,
    "from-meetings": from_meetings.gather,
    "from-research": from_research.gather,
    "from-bd": from_bd.gather,
}


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Sector extraction — Plan v3 §6.9 Phase 3."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("from-projects")
@click.option("--sector", help="Single sector slug (e.g. telecoms). Mutually exclusive with --all-active.")
@click.option("--all-active", is_flag=True, help="Run for every sector in profile.md active_sectors.")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None, help="ISO date — only consider inputs newer than this.")
@click.option("--skip-llm", is_flag=True, help="Skip LLM paraphrase/classification — keyword fallback only.")
def cmd_from_projects(sector, all_active, vault, since_str, skip_llm):
    """Extract from closed projects."""
    _run_single_extractor("from-projects", sector, all_active, vault, since_str, skip_llm)


@main.command("from-newsletters")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None)
@click.option("--skip-llm", is_flag=True)
def cmd_from_newsletters(sector, all_active, vault, since_str, skip_llm):
    """Extract from sector newsletters."""
    _run_single_extractor("from-newsletters", sector, all_active, vault, since_str, skip_llm)


@main.command("from-meetings")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None)
@click.option("--skip-llm", is_flag=True)
def cmd_from_meetings(sector, all_active, vault, since_str, skip_llm):
    """Extract from meeting notes (HiNotes)."""
    _run_single_extractor("from-meetings", sector, all_active, vault, since_str, skip_llm)


@main.command("from-research")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None)
@click.option("--skip-llm", is_flag=True)
def cmd_from_research(sector, all_active, vault, since_str, skip_llm):
    """Extract from operator research notes."""
    _run_single_extractor("from-research", sector, all_active, vault, since_str, skip_llm)


@main.command("from-bd")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None)
@click.option("--skip-llm", is_flag=True)
def cmd_from_bd(sector, all_active, vault, since_str, skip_llm):
    """Extract from BD activity (requires Phase 5 BD layer)."""
    _run_single_extractor("from-bd", sector, all_active, vault, since_str, skip_llm)


@main.command("all")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--since", "since_str", default=None)
@click.option("--skip-llm", is_flag=True)
def cmd_all(sector, all_active, vault, since_str, skip_llm):
    """Run all five extractors in sequence. Used by the daily 02:00 cron."""
    _run_all_extractors(sector, all_active, vault, since_str, skip_llm)


@main.command("synthesize")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
@click.option("--regenerate-views/--skip-views", default=True,
              help="Also regenerate BD.md + People.md views (default: regenerate).")
def cmd_synthesize(sector, all_active, vault, regenerate_views):
    """Sector synthesis -- read provenance, recalc metadata, propose updates.

    Used by the Sat 02:30 cron. Reads applied entries from
    Sectors/<X>/_sources/from-*.md and writes a proposal to
    Routines/sector-synthesis/<date>-<X>.md.
    """
    sectors = _resolve_sectors(sector, all_active, vault)
    today = date_cls.today()
    for sec in sectors:
        run_id = audit.new_run_id()
        click.echo(f"sector-synthesize sector={sec} run-id={run_id}")
        proposal = syn_mod.synthesize_sector(vault, sec, run_id)
        path = syn_mod.write_synthesis_proposal(vault, proposal, today)
        proposal.markdown_path = str(path.relative_to(vault)).replace("\\", "/")

        view_paths = []
        if regenerate_views:
            bd_path = views_mod.regenerate_bd_view(vault, sec)
            people_path = views_mod.regenerate_people_view(vault, sec)
            view_paths = [str(bd_path), str(people_path)]
            click.echo(f"  regenerated BD.md + People.md views")

        audit.write_structured(
            actor={"type": "system", "id": "routine:sector-synthesize"},
            entity_type="vault_note",
            entity_id=proposal.markdown_path,
            action="run",
            routine="sector-synthesize",
            run_id=run_id,
            status="ok",
            audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
            inputs={"sector": sec},
            outputs={
                "proposal_path": proposal.markdown_path,
                "claim_files_recalculated": len(proposal.recalcs),
                "view_paths": view_paths,
            },
            episodic_source=f"Sectors/{sec}/_sources/",
            semantic_target=f"Sectors/{sec}/*.md",
        )
        click.echo(f"  -> wrote {path.relative_to(vault)} ({len(proposal.recalcs)} claim files recalculated)")


@main.command("regenerate-views")
@click.option("--sector")
@click.option("--all-active", is_flag=True)
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path), default=DEFAULT_VAULT)
def cmd_regenerate_views(sector, all_active, vault):
    """Just regenerate BD.md + People.md auto-views (no synthesis proposal).

    Useful for ad-hoc refresh after adding bd_state or sectors: to a file.
    """
    sectors = _resolve_sectors(sector, all_active, vault)
    for sec in sectors:
        bd_path = views_mod.regenerate_bd_view(vault, sec)
        people_path = views_mod.regenerate_people_view(vault, sec)
        click.echo(f"sector={sec}: regenerated {bd_path.name} + {people_path.name}")


def _run_single_extractor(name: str, sector, all_active, vault, since_str, skip_llm):
    sectors = _resolve_sectors(sector, all_active, vault)
    since = date_cls.fromisoformat(since_str) if since_str else None
    extractor = _EXTRACTOR_MAP[name]
    for sec in sectors:
        run_id = audit.new_run_id()
        click.echo(f"sector-extract {name} sector={sec} run-id={run_id}")
        extracts = extractor(vault, sec, since=since, skip_llm=skip_llm)
        _emit_proposal(vault, sec, [name], extracts, run_id)


def _run_all_extractors(sector, all_active, vault, since_str, skip_llm):
    sectors = _resolve_sectors(sector, all_active, vault)
    since = date_cls.fromisoformat(since_str) if since_str else None
    for sec in sectors:
        run_id = audit.new_run_id()
        click.echo(f"sector-extract ALL sector={sec} run-id={run_id}")
        all_extracts: list[SectorExtract] = []
        for name, extractor in _EXTRACTOR_MAP.items():
            try:
                ex = extractor(vault, sec, since=since, skip_llm=skip_llm)
                all_extracts.extend(ex)
                click.echo(f"  {name}: {len(ex)} extract(s)")
            except Exception as e:  # noqa: BLE001
                click.echo(f"  {name}: FAILED ({e})", err=True)
        _emit_proposal(vault, sec, list(_EXTRACTOR_MAP.keys()), all_extracts, run_id)


def _resolve_sectors(sector, all_active, vault_root: Path) -> list[str]:
    if sector and all_active:
        click.echo("Cannot use --sector and --all-active together.", err=True)
        sys.exit(2)
    if sector:
        return [schema.slugify_sector(sector)]
    if all_active:
        return _read_active_sectors(vault_root)
    click.echo("Specify --sector <name> or --all-active.", err=True)
    sys.exit(2)


def _read_active_sectors(vault_root: Path) -> list[str]:
    """Read profile.md active_sectors and return as lowercase slugs."""
    import frontmatter
    profile = vault_root / "_claude" / "profile.md"
    if not profile.exists():
        return []
    try:
        meta = frontmatter.load(profile).metadata or {}
    except Exception:
        return []
    active = meta.get("active_sectors") or []
    if isinstance(active, list):
        return [schema.slugify_sector(str(s)) for s in active]
    return []


def _emit_proposal(vault_root: Path, sector: str, source_types: list[str],
                   extracts: list[SectorExtract], run_id: str):
    """Write proposal markdown + audit log entry."""
    today = date_cls.today()
    proposal = SectorProposal(
        sector=sector,
        generated_at=datetime.now(timezone.utc),
        source_types_run=source_types,
        extracts=extracts,
        inputs_matched=len({ex.source_path for ex in extracts}),
        run_id=run_id,
    )
    path = write_proposal(vault_root, proposal, today)
    proposal.markdown_path = str(path.relative_to(vault_root)).replace("\\", "/")

    audit.write_structured(
        actor={"type": "system", "id": "routine:sector-extract"},
        entity_type="vault_note",
        entity_id=proposal.markdown_path,
        action="run",
        routine="sector-extract",
        run_id=run_id,
        status="ok" if extracts else "skipped",
        audit_dir=Path(__file__).resolve().parent.parent.parent / "runs",
        inputs={"sector": sector, "source_types": source_types},
        outputs={"proposal_path": proposal.markdown_path, "extract_count": len(extracts)},
        episodic_source="vault scan: Projects/, Resources/Newsletters/, Companies/",
        semantic_target=f"Sectors/{sector}/_sources/ (post-apply)",
    )

    click.echo(f"  -> wrote {path.relative_to(vault_root)} ({len(extracts)} extracts)")


@click.command()
@click.argument("text")
@click.option("--sector", required=True, help="Sector slug.")
@click.option("--claim-type", required=True,
              type=click.Choice(list(schema.CLAIM_TYPES), case_sensitive=False))
@click.option("--source", "source_citation", required=True, help="Source citation.")
@click.option("--confidence", "confidence_hint",
              type=click.Choice(["high", "medium", "low"]), default="medium")
@click.option("--sensitivity", default="internal",
              type=click.Choice(["public", "internal", "confidential"]))
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
def sector_note(text, sector, claim_type, source_citation, confidence_hint,
                sensitivity, vault):
    """Add a manual operator note to a sector's from-manual.md provenance.

    Manual entries DON'T go through REVIEW chip — operator authored.
    Applied immediately. Next synthesizer run picks them up.
    """
    sector_slug = schema.slugify_sector(sector)
    target = vault / "Sectors" / sector_slug / "_sources" / "from-manual.md"
    if not target.exists():
        click.echo(f"Error: {target} doesn't exist. Has the sector been scaffolded?", err=True)
        sys.exit(1)

    today = date_cls.today()
    slug = f"note-{today.isoformat()}-{int(time.time()) % 100000}"
    entry = "\n".join([
        "",
        f"## {slug}",
        "- **author:** operator",
        f"- **noted_on:** {today.isoformat()}",
        f"- **claim_targets:** [{claim_type.capitalize()}]",
        f"- **source_citation:** {source_citation}",
        f"- **confidence_hint:** {confidence_hint}",
        f"- **sensitivity:** {sensitivity}",
        "- **Bullets:**",
        f"  - {text}",
        "",
    ])

    # Append to existing file (don't overwrite earlier entries)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    atomic_write(target, existing + entry, vault_root=vault)
    click.echo(f"Added manual note to {target.relative_to(vault)}: {slug}")


if __name__ == "__main__":
    main()
