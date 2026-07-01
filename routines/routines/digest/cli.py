"""CLI for the digest crew (#ingest-digest: stages 1-5; emit is bridge-side + --emit-gated).

    digest <dir> --project <X>

Operator-attended (decision 3). Triggers the ``digest`` CREW — it does NOT run a
plain routine (decision 4). The blocking runner reuses the EXACT gate the HTTP
crew route uses (``registry.resolve_crew_sensitivity`` → ``enforce_sensitivity_
lane`` → ``audit_mirror``) so the sensitivity guard fires BEFORE the subprocess
starts, then launches the crew synchronously via ``routines.crew.proxy`` and
prints/persists the intermediate per-doc fact structures the crew wrote.

Stages 3-4 (cross-doc synthesise + completeness review) run IN the crew; stage 5
(emit-to-vault) runs HERE, bridge-side + operator-gated: dry-run by default,
writing to ``Projects/<deal>/digest/`` only on ``--emit`` (the crew can't write
the vault). The CLI surfaces all of it.

Watcher automation is v2 (decision 3) — this is the attended CLI only.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click

from routines.crew import artefacts, audit_mirror, proxy, registry
from routines.hooks.central_guards import (
    SensitivityViolation,
    enforce_sensitivity_lane,
)
from routines.hooks.types import LLMCallHookContext, SkillRef, WorkspaceRef
from routines.shared import audit
from routines.digest.emit import emit_digest

log = logging.getLogger(__name__)

DIGEST_VERB = "digest"
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parents[2] / "runs"
# Mirrors crews_src/_shared/digest/analyzer.py::DEFAULT_CONCURRENCY (can't be
# imported across the venv boundary). Keep in sync.
DEFAULT_CONCURRENCY = 3


class DigestRunError(RuntimeError):
    """A digest run failed before/at launch (refused, venv missing, crew error)."""


def run_digest_blocking(
    drop_dir: str,
    project: str,
    *,
    workspace_type: str = "project",
    workspace_name: str | None = None,
    declared_tier: str | None = None,
    sensitivity_override: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    recursive: bool = False,
    narrative: bool = False,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    timeout_s: float | None = None,
) -> dict:
    """Resolve sensitivity → run the central guard → launch the digest crew
    synchronously → mirror audit rows. Returns the parsed ``CrewOutput`` dict.

    Raises :class:`DigestRunError` on a refused/guarded run (the crew never
    launches) or a missing crew venv. Crew-level failures (timeout/error) come
    back IN the returned dict's ``status`` — the proxy returns a structured
    error rather than raising for those.

    The trust + fail-closed posture is identical to the HTTP route's
    ``run_crew`` (see its docstring): caller-supplied workspace tier only
    TIGHTENS, overrides can't loosen, the guard re-tightens via ``_strictest``,
    and lanes are local-only in v1."""
    manifest = registry.get_manifest(DIGEST_VERB)
    if not proxy.crew_venv_available():
        raise DigestRunError(
            f"crew venv not installed at {proxy.CREW_PYTHON} — run "
            f"`python -m routines.crew.install.install_metagpt install`"
        )

    ws_name = workspace_name or project or "_digest"
    run_id = audit.new_run_id()

    # 1. Resolve sensitivity + lane (registry matrix; refused override = no run).
    try:
        crew_sensitivity = registry.resolve_crew_sensitivity(
            manifest, workspace_type, declared_tier, sensitivity_override,  # type: ignore[arg-type]
        )
    except registry.SensitivityRefused as e:
        audit_mirror.write_refusal(
            verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir,
            workspace_type=workspace_type, workspace_name=ws_name, reason=str(e),
        )
        raise DigestRunError(f"sensitivity refused: {e}") from e
    crew_lane = registry.pick_lane(crew_sensitivity)

    # 2. Central sensitivity-lane guard — the SAME gate skills + the crew route
    #    use. Fires BEFORE any subprocess exists.
    guard_ctx = LLMCallHookContext(
        run_id=run_id,
        skill=SkillRef(
            name=f"crew.{DIGEST_VERB}",
            metadata={
                "sensitivity": crew_sensitivity,
                "cost_cap_tokens": manifest.cost_cap_tokens,
            },
        ),
        workspace=WorkspaceRef(type=workspace_type, name=ws_name),
        sensitivity=crew_sensitivity,
        lane=crew_lane,
        provider="ollama" if crew_lane.startswith("ollama") else "claude-cli",
        model=registry.model_for_lane(crew_lane, manifest),
        prompt="",  # pre-launch gate; no doc content crosses the bridge here
    )
    try:
        enforce_sensitivity_lane(guard_ctx)
    except SensitivityViolation as e:
        audit_mirror.write_refusal(
            verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir,
            workspace_type=workspace_type, workspace_name=ws_name, reason=str(e),
        )
        raise DigestRunError(f"sensitivity-lane guard refused: {e}") from e
    crew_sensitivity = guard_ctx.sensitivity  # the guard may have tightened it

    # 3. Parent "started" row before the process exists.
    args = {"drop_dir": drop_dir, "project": project,
            "concurrency": concurrency, "recursive": recursive,
            "narrative": narrative}
    audit_mirror.write_parent_started(
        verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir,
        workspace_type=workspace_type, workspace_name=ws_name,
        sensitivity=crew_sensitivity, lane=crew_lane,
        args=args, cost_cap_tokens=manifest.cost_cap_tokens,
    )

    # 4. Launch the crew subprocess synchronously (operator-attended — no
    #    background thread, no on_human_input; the digest crew never asks).
    crew_input = {
        "crew_verb": DIGEST_VERB,
        "run_id": run_id,
        "workspace": {
            "type": workspace_type, "name": ws_name,
            "sensitivity_tier": crew_sensitivity,
        },
        "args": args,
        "cost_cap_tokens": manifest.cost_cap_tokens,
        "llm_config": registry.build_llm_config(crew_lane, manifest),
    }
    # Bridge-extract every supported drop-dir doc → args["extracted_text"]
    # (integration: digest extraction moved bridge-side onto the /triage pattern
    # — the crews venv has no PDF/DOCX libs). A bad drop_dir raises here, before
    # the subprocess; per-doc failures are skipped inside (the crew degrades that
    # one doc). The HTTP route does the same via its worker thread.
    try:
        crew_input = artefacts.prepare_crew_input(DIGEST_VERB, crew_input)
    except artefacts.CrewInputError as e:
        audit_mirror.write_parent_completion(
            verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir, status="error",
            workspace_type=workspace_type, workspace_name=ws_name,
            sensitivity=crew_sensitivity, duration_ms=0, result=None, error=str(e),
        )
        raise DigestRunError(f"input prep failed: {e}") from e
    bound = min(float(manifest.cost_cap_seconds), float(proxy.WALL_CLOCK_TIMEOUT_S))
    if timeout_s is not None:
        bound = min(bound, float(timeout_s))

    result: dict | None = None
    status = "error"
    error: str | None = None
    t0 = time.monotonic()
    try:
        result = proxy.launch_crew(
            manifest.module, crew_input, run_id, crew_sensitivity, timeout_s=bound,
        )
        status = str(result.get("status", "ok"))
        if result.get("error"):
            error = str(result["error"])
        if str(result.get("run_id") or "") != run_id:
            # Reject a mismatched response — mutate the RETURNED dict, not just
            # the audit vars, so the CLI can't exit 0 on it (codex-5.5 SEV-2).
            status = "error"
            error = "run_id mismatch — crew echoed a different run id"
            result["status"] = status
            result["error"] = error
    except proxy.CrewTimeoutError as e:
        status, error = "timeout", str(e)
    except proxy.CrewSubprocessError as e:
        status, error = "error", str(e)

    # 5. Mirror parent completion + per-role child rows.
    duration_ms = int((time.monotonic() - t0) * 1000)
    audit_mirror.write_parent_completion(
        verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir, status=status,
        workspace_type=workspace_type, workspace_name=ws_name,
        sensitivity=crew_sensitivity, duration_ms=duration_ms,
        result=result, error=error,
    )
    if result and isinstance(result.get("roles_log"), list):
        audit_mirror.write_role_rows(
            verb=DIGEST_VERB, run_id=run_id, audit_dir=audit_dir,
            roles_log=result["roles_log"],
        )

    if result is None:
        raise DigestRunError(error or "crew launch failed")
    result.setdefault("run_id", run_id)
    return result


# ── pretty-print the intermediate per-doc structures ──────────────────────────


def _print_slice(result: dict) -> dict | None:
    """Print the crew summary + the per-doc fact structures from the intermediate
    artefact the crew wrote (if present + readable). Returns the parsed
    intermediate payload (dict) so the caller can emit it (stage 5), or None when
    there is no readable artefact."""
    click.echo(f"\n{result.get('summary', '(no summary)')}")
    tokens = result.get("token_count", 0)
    click.echo(f"  status={result.get('status')}  tokens={tokens}")

    artefact = next(
        (a for a in (result.get("artefacts") or [])
         if str(a.get("path", "")).endswith(".digest.json")),
        None,
    )
    if not artefact:
        click.echo("  (no intermediate artefact — run produced no per-doc detail)")
        return None
    try:
        payload = json.loads(Path(artefact["path"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"  (could not read intermediate artefact: {e})")
        return None

    scan = payload.get("scan", {})
    click.echo(
        f"\n-- scan: {scan.get('total_files', 0)} file(s), "
        f"{scan.get('unique_docs', 0)} unique, {scan.get('duplicates', 0)} dup, "
        f"{scan.get('unsupported', 0)} unsupported "
        f"(project tier: {scan.get('project_sensitivity')}) --"
    )
    for a in payload.get("analyses", []):
        routing = a.get("routing") or {}
        click.echo(
            f"\n[{a.get('doc_type')}] {Path(a.get('path', '')).name}  "
            f"status={a.get('status')} enriched={a.get('enriched')}"
        )
        click.echo(
            f"   routing: verified_public={routing.get('verified_public')} "
            f"cloud_eligible={routing.get('cloud_eligible')} "
            f"effective_lane={routing.get('effective_lane')!r}"
        )
        click.echo(f"   reason: {routing.get('reason')}")
        if a.get("entities"):
            click.echo(f"   entities: {', '.join(a['entities'][:10])}")
        for fct in (a.get("facts") or [])[:20]:
            extra = " ".join(x for x in (fct.get("unit"), fct.get("period")) if x)
            click.echo(
                f"   - [{fct.get('kind')}] {fct.get('subject')} | "
                f"{fct.get('field')} = {fct.get('value')}"
                + (f" ({extra})" if extra else "")
            )
    syn = payload.get("synthesis")
    if syn:
        ents = syn.get("entities") or []
        cons = syn.get("contradictions") or []
        click.echo(
            f"\n-- synthesis (stage 3): {len(ents)} fused entities, "
            f"{len(syn.get('facts') or [])} fact(s), "
            f"{len(cons)} contradiction(s) --"
        )
        if ents:
            click.echo("   entities: " + ", ".join(
                str(e.get("name", "")) for e in ents[:15]))
        for c in cons[:15]:
            vals = " vs ".join(str(e.get("value")) for e in (c.get("entries") or []))
            click.echo(
                f"   contradiction: {c.get('subject')} | {c.get('field')}: {vals}"
            )
        narrative = str(syn.get("narrative") or "").strip()
        if narrative:
            click.echo("\n   narrative (non-authoritative LLM summary):\n   "
                       + narrative.replace("\n", "\n   "))
    rev = payload.get("review")
    if rev:
        gate = "PASSED" if rev.get("passed") else "FAILED"
        click.echo(
            f"\n-- review gate (stage 4): {gate} -- "
            f"{len(rev.get('uncited') or [])} uncited, "
            f"{len(rev.get('new_entities') or [])} new entity(ies), "
            f"{len(rev.get('orphan_subjects') or [])} orphan subject(s) --"
        )
        for u in (rev.get("uncited") or [])[:15]:
            click.echo(f"   UNCITED: {u}")
        if rev.get("new_entities"):
            click.echo("   new entities: " + ", ".join(rev["new_entities"][:15]))
        if rev.get("orphan_subjects"):
            click.echo("   orphan subjects: "
                       + ", ".join(rev["orphan_subjects"][:15]))
    deferred = payload.get("deferred_stages") or []
    if deferred:
        click.echo(
            f"\n  crew-deferred stages: {', '.join(deferred)} "
            f"(emit runs bridge-side — see below)"
        )
    click.echo(f"\n  full intermediate JSON: {artefact['path']}")
    return payload


def _emit_digest_note(result: dict, payload: dict, *, write: bool) -> None:
    """Render (dry-run) or write the stage-5 digest note to the vault. Bridge-
    side + operator-gated: dry-run by default, writes only on --emit. Best-effort
    — an emit failure prints but never changes the crew's exit code (the digest
    itself already succeeded)."""
    try:
        from routines.api import deps  # lazy: honour a monkeypatched deps.VAULT
        er = emit_digest(
            payload, run_id=str(result.get("run_id") or ""),
            created=time.strftime("%Y-%m-%d"), vault_root=Path(deps.VAULT),
            write=write,
        )
    except Exception as e:  # noqa: BLE001 — emit must never fail a successful digest
        click.echo(f"\n  emit skipped: {type(e).__name__}: {e}", err=True)
        return
    if write:
        click.echo(f"\n  emitted digest note -> {er.path} ({er.bytes} bytes)")
    else:
        click.echo(
            f"\n  [dry-run] would write digest note -> {er.rel_path} "
            f"({er.bytes} bytes). Re-run with --emit to write it."
        )


@click.command()
@click.argument("drop_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project", required=True, help="Deal/project this doc pile belongs to.")
@click.option("--workspace-type", type=click.Choice(["project", "bd", "general"]),
              default="project", show_default=True,
              help="Workspace tier source. project/bd → confidential (fail-closed).")
@click.option("--workspace-name", default=None,
              help="Workspace name (defaults to --project).")
@click.option("--concurrency", type=int, default=DEFAULT_CONCURRENCY, show_default=True,
              help="Max parallel per-doc analyzers (bounded concurrency).")
@click.option("--recursive", is_flag=True, help="Scan subdirectories too.")
@click.option("--narrative", is_flag=True,
              help="Also generate a (non-authoritative) local-LLM cross-doc "
                   "summary. Default off — the deterministic fusion always runs.")
@click.option("--emit", is_flag=True,
              help="Write the digest note to Projects/<deal>/digest/ via the "
                   "central write policy. Default off — dry-run prints the "
                   "would-write path only ([no-overwrite-without-confirmation]).")
@click.option("--debug", is_flag=True)
def main(
    drop_dir: Path, project: str, workspace_type: str, workspace_name: str | None,
    concurrency: int, recursive: bool, narrative: bool, emit: bool, debug: bool,
) -> None:
    """Ingest a drop dir of deal docs through the digest crew (stages 1-2)."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    click.echo(f"digesting {drop_dir} (project={project}, workspace={workspace_type})...")
    try:
        result = run_digest_blocking(
            str(drop_dir), project,
            workspace_type=workspace_type, workspace_name=workspace_name,
            concurrency=concurrency, recursive=recursive, narrative=narrative,
            audit_dir=DEFAULT_AUDIT_DIR,  # read at call time so tests can redirect
        )
    except DigestRunError as e:
        click.echo(f"digest refused/failed: {e}", err=True)
        # 3 = refused/guarded (operator-actionable); 2 = infra (venv) — both
        # collapse to a non-zero exit here; the message distinguishes them.
        sys.exit(3 if "refused" in str(e) else 2)

    payload = _print_slice(result)
    if result.get("status") == "ok" and payload is not None:
        _emit_digest_note(result, payload, write=emit)
    if result.get("status") != "ok":
        click.echo(f"\ncrew status: {result.get('status')} — {result.get('error')}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
