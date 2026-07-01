"""CLI entrypoint for the engine.

Exit codes:
    0   success
    1   user error (missing file, bad input, etc.)
    2   engine error (Excel COM failure, hash mismatch, validation failed, etc.)
    3   environment error (xlwings missing, scaffold issue)

Usage:
    engine list
    engine validate <name> [--rehash]
    engine run <name> --inputs inputs.json [--client-fs client-fs.json] --workspace <type>:<name> [--workspace-root <path>] [--no-archive]
    engine new-workspace <type> <name>
    engine list-workspaces [--type <type>]
    engine workspace-status <type>:<name>
    engine audit-tail [-n 10]

    # deprecated aliases (still accepted): --deal <Deal> ⇒ --workspace project:<Deal>
    #                                       new-project / project-status
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import click

import valuation
from valuation import audit, naming, workspace
from valuation.exceptions import (
    ClientFSBlockInvalid,
    ClientFSFormulaCollision,
    ConvergenceFailed,
    EngineError,
    InputCellMismatch,
    ProjectBootstrapError,
    TemplateHashMismatch,
    TemplateNotFound,
    ValidationFailed,
)
from valuation.workspace import WORKSPACE_TYPES
from valuation.registry import TemplateRegistry


# Resolve registry path relative to the package install (the engine repo root).
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "templates" / "templates.yaml"


def _load_registry(registry_path: Path = REGISTRY_PATH) -> TemplateRegistry:
    try:
        return TemplateRegistry.from_yaml(registry_path)
    except FileNotFoundError:
        click.echo(f"ERROR: registry not found at {registry_path}", err=True)
        sys.exit(1)
    except (TemplateNotFound, ValueError) as e:
        click.echo(f"ERROR: registry invalid: {e}", err=True)
        sys.exit(1)


def _resolve_run_workspace(workspace_ref: str | None, deal: str | None,
                           workspace_root: Path | None) -> "workspace.Workspace":
    """Resolve --workspace / --deal into a :class:`Workspace`. Exactly one of the
    two must be supplied; the legacy ``--deal X`` maps to ``project:X``."""
    if workspace_ref and deal:
        click.echo("ERROR: pass either --workspace or --deal, not both.", err=True)
        sys.exit(1)
    if not workspace_ref and not deal:
        click.echo("ERROR: --workspace <type>:<name> is required "
                   "(or the deprecated --deal <Deal>).", err=True)
        sys.exit(1)
    try:
        if workspace_ref:
            ws_type, ws_name = workspace.parse_workspace_ref(workspace_ref)
        else:
            ws_type, ws_name = "project", deal  # type: ignore[assignment]
        return workspace.paths_for(
            ws_type, ws_name,
            fs_root=workspace_root if workspace_root else None,
        )
    except ProjectBootstrapError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)


@click.group()
@click.version_option(version=valuation.__version__, prog_name="engine")
def main() -> None:
    """Agentic OS valuation engine — Excel-template wrapper CLI."""


# --------------------------------------------------------------------- list

@main.command("list")
def list_templates() -> None:
    """List registered templates and their on-disk paths."""
    registry = _load_registry()
    names = registry.names()
    if not names:
        click.echo(f"Registry: {REGISTRY_PATH}")
        click.echo("(no templates registered)")
        return
    click.echo(f"Registry: {REGISTRY_PATH}")
    click.echo()
    for name in names:
        spec = registry.resolve(name)
        click.echo(f"  {name}  v{spec.version}")
        click.echo(f"      path:        {spec.path}")
        click.echo(f"      inputs:      {len(spec.inputs)} required + {len(spec.optional_inputs)} optional")
        click.echo(f"      outputs:     {len(spec.outputs)}")
        click.echo(f"      validation:  {len(spec.validation)} rule(s)")
        click.echo(f"      post-recalc: {len(spec.post_recalc_hardcode)} hardcode step(s)")
        click.echo(f"      hash:        {spec.template_hash[:24]}…")


# ----------------------------------------------------------------- validate

@main.command("validate")
@click.argument("template_name")
@click.option("--rehash", is_flag=True, help="Update the stored hash to the live file's current hash")
def validate_template(template_name: str, rehash: bool) -> None:
    """Offline-check that the template's cell map resolves against the live .xlsx."""
    from valuation import excel_engine

    registry = _load_registry()
    try:
        spec = registry.resolve(template_name)
    except TemplateNotFound as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    issues = excel_engine.validate(spec)
    if issues:
        click.echo(f"Validation FAILED for {template_name!r}:", err=True)
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
        sys.exit(2)

    click.echo(f"Validation OK for {template_name!r}")
    click.echo(f"  path:    {spec.path}")
    click.echo(f"  hash:    {spec.template_hash}")
    click.echo(f"  inputs:  {len(spec.inputs)} required, {len(spec.optional_inputs)} optional")
    click.echo(f"  outputs: {len(spec.outputs)}")
    if rehash:
        click.echo("  (--rehash: hash recomputed at registry load time)")


# --------------------------------------------------------------------- run

@main.command("run")
@click.argument("template_name")
@click.option("--inputs", "inputs_path", type=click.Path(exists=True, path_type=Path), required=True,
              help="JSON file with input values")
@click.option("--client-fs", "client_fs_path", type=click.Path(exists=True, path_type=Path), default=None,
              help="Optional JSON file with a Client_FS operating-model block, written "
                   "into the copied workbook BEFORE the input cells (same manual-calc "
                   "window). Shape: {\"dates\": [10 ISO dates], \"rows\": {\"6\": [10 "
                   "numbers], ...}, \"zero_rows\": [ints], \"sheet\": \"Client_FS\"}. "
                   "UNITS: values are written verbatim (full currency units — the "
                   "engine applies NO x1e6). See templates/templates.yaml.")
@click.option("--workspace", "workspace_ref", default=None,
              help="Target workspace as '<type>:<name>' (type ∈ project|bd|general), "
                   "e.g. 'project:DemoDeal' or 'general:ASOS'.")
@click.option("--workspace-root", "workspace_root", type=click.Path(path_type=Path), default=None,
              help="Filesystem root for the workspace type. The bridge passes this "
                   "(resolved from the operator profile); omit on the CLI to use the "
                   "profile-mirrored default under <workspace-root>/.")
@click.option("--deal", "deal", default=None,
              help="DEPRECATED — alias for --workspace project:<Deal>.")
@click.option("--no-archive", is_flag=True, help="Skip the per-workspace `00. OLD/` archive step")
@click.option("--no-hash-check", is_flag=True, help="Skip the live-file hash verification")
@click.option("--output-json", "output_json", is_flag=True,
              help="Emit a single machine-readable JSON line on stdout after a successful "
                   "run (run_id, output_path, status, duration_ms, convergence_iters, outputs). "
                   "Opt-in; the human-readable summary is preserved either way. Consumed by the "
                   "routines bridge (#21 LBO skill).")
def run_template(template_name: str, inputs_path: Path, client_fs_path: Path | None,
                 workspace_ref: str | None,
                 workspace_root: Path | None, deal: str | None, no_archive: bool,
                 no_hash_check: bool, output_json: bool) -> None:
    """Run a registered template against the provided inputs."""
    from valuation import excel_engine

    registry = _load_registry()
    try:
        spec = registry.resolve(template_name)
    except TemplateNotFound as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    raw_inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    # Underscore-prefixed keys are comment / metadata convention (JSON has no
    # comment syntax). Strip them so they don't fail the engine's strict
    # required-vs-optional check.
    inputs = {k: v for k, v in raw_inputs.items() if not k.startswith("_")}
    stripped = [k for k in raw_inputs if k.startswith("_")]
    if stripped:
        click.echo(f"  (ignoring {len(stripped)} comment key(s): {stripped})")

    # Optional Client_FS operating-model block (same _-prefix comment convention).
    # Root-type check here keeps the documented contract — a malformed block
    # fails exit 1 BEFORE Excel opens (codex review 2026-06-10: a JSON
    # array/string root would otherwise crash on .items() outside the mapped
    # ClientFSBlockInvalid path).
    client_fs = None
    if client_fs_path is not None:
        raw_client_fs = json.loads(client_fs_path.read_text(encoding="utf-8"))
        if not isinstance(raw_client_fs, dict):
            click.echo(f"\nCLIENT_FS BLOCK INVALID:\nroot of {client_fs_path} must be a "
                       f"JSON object, got {type(raw_client_fs).__name__}", err=True)
            sys.exit(1)
        client_fs = {k: v for k, v in raw_client_fs.items() if not k.startswith("_")}

    # Resolve --workspace / --deal into a Workspace.
    ws = _resolve_run_workspace(workspace_ref, deal, workspace_root)

    # Pre-flight: the workspace must exist (project ⇒ file-system + vault).
    if not ws.exists:
        s = ws.status
        click.echo(f"ERROR: workspace {ws.type}:{ws.name!r} not fully scaffolded.", err=True)
        click.echo(f"  file-system: {'OK' if s['fs'] else 'MISSING ' + str(ws.fs_dir)}", err=True)
        if ws.has_vault:
            click.echo(f"  vault:       {'OK' if s.get('vault') else 'MISSING ' + str(ws.vault_dir)}", err=True)
        if ws.has_vault and s["fs"] and not s.get("vault"):
            # Half-scaffolded: the file-system side exists but the vault counterpart
            # is missing. `new-workspace` would REFUSE (it rejects an existing fs dir),
            # so point at the vault-repair path instead of a command that can't run.
            click.echo(
                f"The file-system folder exists but its vault counterpart is missing. "
                f"`engine new-workspace` cannot repair this (it refuses an existing "
                f"file-system folder). Scaffold the vault side directly — copy "
                f"`{workspace.VAULT_TEMPLATE}` to `{ws.vault_dir}`, or (re)create the "
                f"workspace via the bridge, which scaffolds both sides.", err=True)
        else:
            click.echo(f"Run `engine new-workspace {ws.type} {ws.name!r}` to bootstrap it.", err=True)
        sys.exit(1)

    # Compose output path per the (workspace-type-aware) naming policy.
    out_path = naming.next_output_path_for(ws, template_name)
    output_dir = out_path.parent
    output_filename = out_path.name

    click.echo(f"Running {template_name!r} for workspace={ws.type}:{ws.name!r}")
    click.echo(f"  template:    {spec.path}  (hash {spec.template_hash[:16]}…)")
    click.echo(f"  inputs:      {len(inputs)} keys from {inputs_path}")
    if client_fs is not None:
        click.echo(f"  client_fs:   {len(client_fs.get('rows') or {})} data row(s) + "
                   f"{len(client_fs.get('zero_rows') or [])} zero row(s) from {client_fs_path}")
    click.echo(f"  output:      {out_path}")

    try:
        run = excel_engine.run(
            spec=spec,
            inputs=inputs,
            output_dir=output_dir,
            output_filename=output_filename,
            verify_hash=not no_hash_check,
            client_fs=client_fs,
        )
    except InputCellMismatch as e:
        click.echo(f"\nINPUT MISMATCH:\n{e}", err=True)
        sys.exit(1)
    except ClientFSBlockInvalid as e:
        click.echo(f"\nCLIENT_FS BLOCK INVALID:\n{e}", err=True)
        sys.exit(1)
    except ClientFSFormulaCollision as e:
        click.echo(f"\nCLIENT_FS FORMULA COLLISION:\n{e}", err=True)
        sys.exit(2)
    except TemplateHashMismatch as e:
        click.echo(f"\nHASH MISMATCH:\n{e}", err=True)
        sys.exit(2)
    except ConvergenceFailed as e:
        click.echo(f"\nCONVERGENCE FAILED:\n{e}", err=True)
        sys.exit(2)
    except ValidationFailed as e:
        click.echo(f"\nVALIDATION FAILED:\n{e}", err=True)
        sys.exit(2)
    except EngineError as e:
        click.echo(f"\nENGINE ERROR:\n{e}", err=True)
        sys.exit(2)

    # Audit
    audit.write(run, audit_log=Path(__file__).resolve().parent.parent / "runs" / "audit.jsonl")

    # Archive prior versions (after success — we don't archive on failure)
    if not no_archive:
        moved = naming.archive_supersedes_for(ws, template_name, keep_current=out_path)
        if moved:
            click.echo(f"\nArchived {len(moved)} prior version(s) to 00. OLD/:")
            for m in moved:
                click.echo(f"  - {m.name}")

    click.echo(f"\nRun OK in {run.duration_ms} ms  (convergence iters: {run.convergence_iters})")
    if not output_json:
        click.echo(f"Outputs:")
        for k, v in run.outputs.items():
            click.echo(f"  {k}: {v!r}")

    # Opt-in machine-readable line for the routines bridge (#21). Emitted as
    # the FINAL stdout line so a consumer can take the last line and parse it.
    # Serialization-only: this is a faithful dump of the EngineRun the run()
    # call already produced — no recomputation, no template-specific shaping.
    # 2D ranges come through as nested lists; Excel-error strings / None pass
    # through unchanged. ``default=str`` is a belt-and-braces guard for any
    # stray non-JSON-native cell value (e.g. a datetime read back from a date
    # cell) so the line is always valid JSON.
    if output_json:
        payload = {
            "run_id": run.run_id,
            "template_name": run.template_name,
            "template_version": run.template_version,
            "status": run.status,
            "duration_ms": run.duration_ms,
            "convergence_iters": run.convergence_iters,
            "output_path": str(run.output_path),
            "outputs": run.outputs,
        }
        click.echo(json.dumps(payload, default=str))


# ------------------------------------------------------------- workspaces

@main.command("new-workspace")
@click.argument("workspace_type", type=click.Choice(WORKSPACE_TYPES))
@click.argument("name")
@click.option("--workspace-root", "workspace_root", type=click.Path(path_type=Path), default=None,
              help="Filesystem root override (default: profile-mirrored root for the type).")
def new_workspace_cmd(workspace_type: str, name: str, workspace_root: Path | None) -> None:
    """Create a new workspace. project ⇒ file-system + vault (atomic — if the
    vault copy fails the file-system side rolls back); bd/general ⇒ file-system only."""
    try:
        ws = workspace.new_workspace(
            workspace_type, name,  # type: ignore[arg-type]
            fs_root=workspace_root if workspace_root else None,
        )
    except ProjectBootstrapError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    click.echo(f"Workspace {ws.type}:{ws.name!r} bootstrapped.")
    click.echo(f"  file-system: {ws.fs_dir}")
    if ws.has_vault:
        click.echo(f"  vault:       {ws.vault_dir}")


@main.command("list-workspaces")
@click.option("--type", "workspace_type", type=click.Choice(WORKSPACE_TYPES), default=None,
              help="Limit to one type; default lists all three.")
@click.option("--workspace-root", "workspace_root", type=click.Path(path_type=Path), default=None,
              help="Filesystem root override (only valid with --type).")
def list_workspaces_cmd(workspace_type: str | None, workspace_root: Path | None) -> None:
    """List existing workspaces (immediate subfolders of each type's root)."""
    if workspace_root and not workspace_type:
        click.echo("ERROR: --workspace-root requires --type.", err=True)
        sys.exit(1)
    types = [workspace_type] if workspace_type else list(WORKSPACE_TYPES)
    for t in types:
        names = workspace.list_workspaces(t, fs_root=workspace_root if workspace_root else None)  # type: ignore[arg-type]
        click.echo(f"{t} ({len(names)}):")
        for n in names:
            click.echo(f"  {n}")
        if not names:
            click.echo("  (none)")


@main.command("workspace-status")
@click.argument("workspace_ref")
@click.option("--workspace-root", "workspace_root", type=click.Path(path_type=Path), default=None)
def workspace_status_cmd(workspace_ref: str, workspace_root: Path | None) -> None:
    """Report whether a '<type>:<name>' workspace is scaffolded."""
    try:
        ws_type, ws_name = workspace.parse_workspace_ref(workspace_ref)
        ws = workspace.paths_for(ws_type, ws_name, fs_root=workspace_root if workspace_root else None)
    except ProjectBootstrapError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    s = ws.status
    click.echo(f"Workspace: {ws.type}:{ws.name!r}")
    click.echo(f"  file-system: {'OK' if s['fs'] else 'MISSING'}  ({ws.fs_dir})")
    if ws.has_vault:
        click.echo(f"  vault:       {'OK' if s.get('vault') else 'MISSING'}  ({ws.vault_dir})")
    sys.exit(0 if ws.exists else 1)


# --------------------------------------------- deprecated project aliases

@main.command("new-project")
@click.argument("deal")
def new_project_cmd(deal: str) -> None:
    """DEPRECATED — alias for `new-workspace project <deal>`."""
    try:
        ws = workspace.new_workspace("project", deal)
    except ProjectBootstrapError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    click.echo(f"Project {ws.name!r} bootstrapped.")
    click.echo(f"  file-system: {ws.fs_dir}")
    click.echo(f"  vault:       {ws.vault_dir}")


@main.command("project-status")
@click.argument("deal")
def project_status_cmd(deal: str) -> None:
    """DEPRECATED — alias for `workspace-status project:<deal>`."""
    ws = workspace.paths_for("project", deal)
    s = ws.status
    click.echo(f"Deal: {ws.name!r}")
    click.echo(f"  file-system: {'OK' if s['fs'] else 'MISSING'}  ({ws.fs_dir})")
    click.echo(f"  vault:       {'OK' if s.get('vault') else 'MISSING'}  ({ws.vault_dir})")
    sys.exit(0 if ws.exists else 1)


# --------------------------------------------------------------- audit-tail

@main.command("audit-tail")
@click.option("-n", default=10, help="Number of records to show")
def audit_tail_cmd(n: int) -> None:
    """Show the last N entries from the run audit log."""
    audit_log = Path(__file__).resolve().parent.parent / "runs" / "audit.jsonl"
    records = audit.tail(n=n, audit_log=audit_log)
    if not records:
        click.echo("(audit log empty or missing)")
        return
    for r in records:
        click.echo(f"{r['ts']}  {r['template']:>8s}  {r['status']:>16s}  "
                   f"{r['duration_ms']:>5d} ms  iters={r['convergence_iters']}  "
                   f"{Path(r['output_path']).name}")


if __name__ == "__main__":
    main()
