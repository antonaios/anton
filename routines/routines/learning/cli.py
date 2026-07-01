"""CLI for the self-improvement (learning) routine.

Four commands:

    learn scan
        Walk Claude Code session logs, detect follow-up events, append
        them to runs/learning-events.jsonl. Idempotent on (session_id,
        timestamp, text-hash).

    learn note "always include capex history in company profiles"
        Manually record a feedback event. Use this for "I just noticed
        this gap" moments. The note lands as a FeedbackEvent with
        source=note and the next `propose` run picks it up.

    learn propose
        Read the events JSONL, cluster, generate the markdown proposal
        at Routines/learning/<date>-template-evolution.md.

    learn record-applied <proposal-path>
        Mark a proposal as applied. Stamps the frontmatter with
        status=applied, applied_at, and applied_commit (vault HEAD by
        default). Procedural-memory versioning — Plan v3 §6.5 Phase B.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import frontmatter

from routines.learning.cluster import cluster_events
from routines.learning.detect import scan_session_logs
from routines.learning.propose import build_proposal, write_proposal
from routines.learning.schema import FeedbackEvent
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"
EVENTS_LOG = DEFAULT_AUDIT_DIR / "learning-events.jsonl"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Self-improvement loop — detect, cluster, and propose template changes."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── scan ──────────────────────────────────────────────────────────────────


@main.command("scan")
@click.option("--projects-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Claude Code projects log dir (default: ~/.claude/projects)")
@click.option("--events-log", type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=EVENTS_LOG, show_default=True)
def scan_cmd(projects_dir: Path | None, events_log: Path) -> None:
    """Walk Claude Code session logs, append new feedback events."""
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    existing_keys = _load_existing_keys(events_log)
    events = scan_session_logs(projects_dir=projects_dir)
    added = 0
    skipped = 0
    with events_log.open("a", encoding="utf-8") as f:
        for ev in events:
            key = _event_key(ev)
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)
            f.write(json.dumps(_event_to_dict(ev)) + "\n")
            added += 1
    click.echo(f"scanned {len(events)} candidate events · {added} new · {skipped} duplicates")
    click.echo(f"events log: {events_log}")


# ── note (manual entry) ───────────────────────────────────────────────────


@main.command("note")
@click.argument("text")
@click.option("--target", default=None,
              help="Vault path the feedback applies to (e.g. Templates/company-profile.md)")
@click.option("--events-log", type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=EVENTS_LOG, show_default=True)
def note_cmd(text: str, target: str | None, events_log: Path) -> None:
    """Record a manual feedback event.

    Example:
      learn note "Always include capex history and projections in company profiles" \
        --target Templates/company-profile.md
    """
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    ev = FeedbackEvent(
        timestamp=now,
        text=text,
        source="note",
        operator_target=target,
    )
    with events_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_event_to_dict(ev)) + "\n")
    click.echo(f"OK  recorded note (target: {target or '-'})")
    click.echo(f"events log: {events_log}")


# ── propose ───────────────────────────────────────────────────────────────


@main.command("propose")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--events-log", type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=EVENTS_LOG, show_default=True)
@click.option("--min-cluster-size", type=int, default=2, show_default=True,
              help="Minimum events per cluster to surface in the proposal")
@click.option("--lookback-days", type=int, default=30, show_default=True)
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--model", default="qwen3:14b")
def propose_cmd(
    vault: Path, events_log: Path, min_cluster_size: int,
    lookback_days: int, ollama_url: str, model: str,
) -> None:
    """Cluster events and write the proposal markdown."""
    DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    paths = VaultPaths(vault)

    # Load events (filtered to lookback window)
    events = _load_events(events_log, lookback_days=lookback_days)
    if not events:
        click.echo("no events to cluster — run `learn scan` first, or capture some `learn note ...` entries")
        return

    click.echo(f"loaded {len(events)} events from last {lookback_days}d")

    client = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"Ollama unreachable: {e}", err=True)
        sys.exit(2)

    click.echo("clustering...")
    clusters = cluster_events(events, client=client, min_cluster_size=min_cluster_size)
    click.echo(f"  -> {len(clusters)} clusters with >= {min_cluster_size} events")
    if not clusters:
        click.echo("(no patterns yet — keep working, more signal will accumulate)")
        return

    click.echo("naming clusters via LLM...")
    doc = build_proposal(clusters, client=client, model=model)

    path = write_proposal(paths.root, doc)
    click.echo(f"\nOK wrote proposal: {path}")
    click.echo(f"   {len([c for c in doc.clusters if c.theme not in ('(reject)','(unlabeled)')])} accepted, "
               f"{len([c for c in doc.clusters if c.theme in ('(reject)','(unlabeled)')])} skipped")

    # Lane transition: episodic → procedural. The scan step consumed
    # session JSONL logs (episodic memory of past Claude conversations)
    # plus explicit `learn note` entries; the propose step emits a
    # markdown that suggests procedural-memory edits (Templates/, CLAUDE.md).
    proposal_targets: list[str] = []
    for c in doc.clusters:
        if c.theme in ("(reject)", "(unlabeled)") or not c.centroid_text:
            continue
        try:
            info = json.loads(c.centroid_text)
        except (json.JSONDecodeError, TypeError):
            continue
        target = info.get("target") if isinstance(info, dict) else None
        if target and target not in proposal_targets:
            proposal_targets.append(target)
    audit.write_structured(
        actor={"type": "system", "id": "routine:learning"},
        entity_type="proposal",
        entity_id=str(path),
        action="propose",
        routine="learning", run_id=audit.new_run_id(), status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={"events": len(events), "min_cluster_size": min_cluster_size, "lookback_days": lookback_days},
        outputs={"proposal_path": str(path), "clusters": len(clusters)},
        episodic_source=str(events_log),
        procedural_target=proposal_targets or [str(path)],
    )


# ── record-applied (procedural-memory versioning) ─────────────────────────


@main.command("record-applied")
@click.argument("proposal_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--commit", "commit_sha", default=None,
              help="Vault repo commit SHA that implemented this proposal "
                   "(default: HEAD of the vault repo containing the proposal)")
@click.option("--files", "applied_files", multiple=True,
              help="Vault-relative paths edited by the commit. Can repeat. "
                   "If omitted, derived from `git show --name-only <commit>`.")
@click.option("--note", default=None,
              help="Optional free-text note (e.g. why this was accepted).")
def record_applied_cmd(
    proposal_path: Path, commit_sha: str | None,
    applied_files: tuple[str, ...], note: str | None,
) -> None:
    """Stamp a learning proposal as applied, recording the vault commit.

    Procedural memory has version semantics that the other lanes don't:
    a template edit changes the shape of every future deliverable. This
    command makes that auditable — the proposal markdown becomes proof
    of which commit drove which behaviour change.

    Workflow:
        1. Operator reads `Routines/learning/<date>-template-evolution.md`.
        2. Operator edits the template files in Obsidian.
        3. Operator commits the vault.
        4. Operator runs `learn record-applied <proposal-path>` — this
           stamps the proposal with the commit SHA.
    """
    vault_root = _find_vault_root(proposal_path)
    if vault_root is None:
        click.echo(f"error: {proposal_path} is not inside a git repo", err=True)
        sys.exit(2)

    sha = commit_sha or _git_head(vault_root)
    if not sha:
        click.echo(f"error: could not read HEAD of {vault_root}", err=True)
        sys.exit(2)

    files_list = list(applied_files) or _git_files_in_commit(vault_root, sha)

    try:
        post = frontmatter.load(proposal_path)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: parse {proposal_path}: {e}", err=True)
        sys.exit(2)

    post.metadata["status"] = "applied"
    post.metadata["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    post.metadata["applied_commit"] = sha
    if files_list:
        post.metadata["applied_files"] = files_list
    if note:
        post.metadata["applied_note"] = note

    proposal_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    click.echo(f"OK  marked applied: {proposal_path.name}")
    click.echo(f"    commit:  {sha}")
    if files_list:
        click.echo(f"    files:   {', '.join(files_list)}")
    if note:
        click.echo(f"    note:    {note}")


def _find_vault_root(start: Path) -> Path | None:
    """Walk upwards from `start` to find the dir containing `.git/`."""
    p = start.resolve()
    if p.is_file():
        p = p.parent
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def _git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _git_files_in_commit(repo: Path, sha: str) -> list[str]:
    """Return vault-relative paths changed by the commit. Empty on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", sha],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


# ── helpers ───────────────────────────────────────────────────────────────


def _event_to_dict(ev: FeedbackEvent) -> dict:
    return {
        "timestamp": ev.timestamp,
        "text": ev.text,
        "source": ev.source,
        "session_id": ev.session_id,
        "prior_artifact": ev.prior_artifact,
        "prior_artifact_kind": ev.prior_artifact_kind,
        "classification": ev.classification,
        "operator_target": ev.operator_target,
    }


def _event_from_dict(d: dict) -> FeedbackEvent:
    return FeedbackEvent(
        timestamp=str(d.get("timestamp") or ""),
        text=str(d.get("text") or ""),
        source=str(d.get("source") or "scan"),
        session_id=d.get("session_id"),
        prior_artifact=d.get("prior_artifact"),
        prior_artifact_kind=d.get("prior_artifact_kind"),
        classification=d.get("classification"),
        operator_target=d.get("operator_target"),
    )


def _event_key(ev: FeedbackEvent) -> str:
    """Idempotency: same (session, timestamp, text) → same key."""
    payload = f"{ev.session_id or ''}|{ev.timestamp}|{ev.text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_existing_keys(log_path: Path) -> set[str]:
    keys: set[str] = set()
    if not log_path.exists():
        return keys
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ev = _event_from_dict(d)
                keys.add(_event_key(ev))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return keys


def _load_events(log_path: Path, *, lookback_days: int) -> list[FeedbackEvent]:
    if not log_path.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
    out: list[FeedbackEvent] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = _event_from_dict(d)
        ts_str = ev.timestamp or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0
        if ts and ts < cutoff:
            continue
        out.append(ev)
    return out


if __name__ == "__main__":
    main()
