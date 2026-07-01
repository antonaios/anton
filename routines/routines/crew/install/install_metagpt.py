"""install_metagpt — scripted, idempotent installer for the ANTON crew engine.

Mirrors the #26a ``install_synapse.py`` precedent: a third-party orchestration
engine is installed ISOLATED — its own venv, driven over a process boundary,
never imported into the bridge process. Differences from the Synapse install:

  * **Python 3.11 venv**, not the host default — MetaGPT supports 3.9-3.11
    only (the bridge venv is 3.14). Step 1 probes ``py -3.11`` and refuses
    to fall back to an unsupported interpreter.
  * **No resident backend** — crews are on-demand subprocesses launched by
    ``routines/crew/proxy.py``, so there are no start/stop/scheduled-task
    steps and no port to manage.
  * **File deployment** — the crew-side modules are versioned in THIS repo
    under ``<repo>/crews_src/`` (single source of truth, reviewable in the
    branch diff) and copied to the live crew dir by step 4. The staged
    Phase-7 pack kept them only as unversioned templates; deploying from the
    repo replaces that hand-copy step.

The script is **safe to re-run** — every step probes existing state and
skips/refreshes accordingly. Failure modes are explicit Click exceptions.

Usage (run from the routines venv; absolute defaults are baked in)::

    python -m routines.crew.install.install_metagpt install
    python -m routines.crew.install.install_metagpt deploy-files   # files only
    python -m routines.crew.install.install_metagpt verify         # post-install checks
    python -m routines.crew.install.install_metagpt status         # read-only report

All commands accept ``--crew-root`` if the operator relocates the install.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click

# ────────────────────────────────────────────────────────────────────────────
# Defaults (match METAGPT-INTEGRATION-SPEC.md §1.1 layout)
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_CREW_ROOT = Path(r"<repo>\crews")
METAGPT_PIN = "metagpt>=0.8,<0.9"   # spec §1.3; operator may pin a sha later
PYTHON_LAUNCHER_SPEC = "-3.11"      # MetaGPT supports 3.9-3.11, NOT 3.12+

# NOTE (#ingest-digest ↔ #32 reconciliation): the digest crew's text
# extraction does NOT add PDF/DOCX deps to this shared crews venv. The /triage
# (#32) precedent — captured in the crew-venv-no-pdf project memory — is that a
# crew needing PDFs extracts BRIDGE-SIDE (the routines venv already has
# pypdfium2/python-docx) and passes page-tagged text over the stdio boundary in
# CrewInput.args, because installing into the shared crews venv is operator-
# gated and risks the live bridge + sibling sessions. Moving digest's extraction
# bridge-side is the integration seam (see crews_src/_shared/digest/extract.py).

# Crew-side sources versioned in this repo (deployed by step 4).
SOURCE_DIR = Path(__file__).resolve().parents[3] / "crews_src"
DEPLOY_FILES = (
    "hello_world_crew.py",
    # #33 /explore — DeepDive crew + its stdlib-only logic module.
    "explore_crew.py",
    "explore_lib.py",
    "triage_crew.py",                # #32 — CIMTriage crew
    # #36 — /debate crew + its pure (metagpt-free) logic helper.
    "debate_crew.py",
    "debate_support.py",
    "_shared/__init__.py",
    "_shared/boundary.py",
    "_shared/ollama_config.py",
    # #crew-cloud-promotion — the bridge route-through LLM provider. Load-bearing:
    # ollama_config.build_ollama_llm_for_role does `from _shared.bridge_llm import
    # BridgeLLM` for a promoted role, so a deploy WITHOUT this ImportErrors any
    # promoted crew at role construction.
    "_shared/bridge_llm.py",
    "_shared/human_provider.py",
    # Shared vault-scan (integration: /explore + /debate both import it).
    # Load-bearing — explore_lib + debate_support `from _shared.vault_scan
    # import ...` at module top, so a deploy WITHOUT this crashes both crews at
    # import in the crew venv (codex correctness HIGH).
    "_shared/vault_scan.py",
    "_shared/triage_lib.py",         # #32 — pure triage helpers (stdlib only)
    # #ingest-digest — the digest crew + its deterministic helpers (stages 1-5:
    # scanner/classifier/extract/analyzer + synthesize/review).
    "digest_crew.py",
    "_shared/digest/__init__.py",
    "_shared/digest/models.py",
    "_shared/digest/scanner.py",
    "_shared/digest/classifier.py",
    "_shared/digest/extract.py",
    "_shared/digest/analyzer.py",
    "_shared/digest/synthesize.py",
    "_shared/digest/review.py",
    # metagpt 0.8.x requires an ``llm:`` default config at import time
    # (Config.default() runs at module import); without this the crew
    # subprocess dies before it can speak the boundary protocol.
    "config/config2.yaml",
)


@dataclass
class CrewPaths:
    crew_root: Path

    @property
    def venv_dir(self) -> Path:
        return self.crew_root / ".venv"

    @property
    def python(self) -> Path:
        if sys.platform == "win32":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    @property
    def logs_dir(self) -> Path:
        return self.crew_root / ".logs"


# ────────────────────────────────────────────────────────────────────────────
# Steps (each idempotent + verifiable)
# ────────────────────────────────────────────────────────────────────────────


def _find_python_311() -> list[str]:
    """Resolve a Python 3.11 interpreter invocation, or raise.

    Windows: the ``py`` launcher with ``-3.11``. POSIX: ``python3.11`` on
    PATH. Refuses anything else — MetaGPT does not support 3.12+."""
    candidates: list[list[str]] = []
    if sys.platform == "win32":
        candidates.append(["py", PYTHON_LAUNCHER_SPEC])
    candidates.append(["python3.11"])
    for cmd in candidates:
        try:
            r = subprocess.run(
                [*cmd, "--version"], check=False, capture_output=True, text=True,
            )
        except FileNotFoundError:
            continue
        if r.returncode == 0 and "3.11." in (r.stdout + r.stderr):
            return cmd
    raise click.ClickException(
        "Python 3.11 not found (MetaGPT supports 3.9-3.11, not 3.12+). "
        "Install via `winget install Python.Python.3.11` and re-run."
    )


def step_1_create_venv(paths: CrewPaths) -> None:
    """Step 1 — ``py -3.11 -m venv <crew_root>/.venv``."""
    if paths.python.is_file():
        ver = subprocess.run(
            [str(paths.python), "--version"],
            check=False, capture_output=True, text=True,
        ).stdout.strip()
        if "3.11." in ver:
            click.echo(f"  [skip] venv already exists at {paths.venv_dir} ({ver})")
            return
        raise click.ClickException(
            f"venv at {paths.venv_dir} is {ver or 'unreadable'}, not 3.11 — "
            f"delete it and re-run (MetaGPT needs 3.9-3.11)"
        )
    py311 = _find_python_311()
    paths.crew_root.mkdir(parents=True, exist_ok=True)
    click.echo(f"  [run]  creating 3.11 venv at {paths.venv_dir}")
    try:
        subprocess.run(
            [*py311, "-m", "venv", str(paths.venv_dir)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"venv creation failed: {e.stderr.strip() or e.stdout.strip()}"
        ) from e
    if not paths.python.is_file():
        raise click.ClickException(
            f"venv created but {paths.python} not found — cannot proceed"
        )


def step_2_pip_install_metagpt(paths: CrewPaths) -> None:
    """Step 2 — pip install the pinned MetaGPT range into the crew venv.

    Heavy install (~40 deps incl. llama-index for metagpt.rag); expect
    several minutes. Re-runs skip when any 0.8.x is already present."""
    probe = subprocess.run(
        [str(paths.python), "-m", "pip", "show", "metagpt"],
        check=False, capture_output=True, text=True,
    )
    if probe.returncode == 0:
        version = next(
            (ln.split(":", 1)[1].strip() for ln in probe.stdout.splitlines()
             if ln.startswith("Version:")),
            "?",
        )
        if version.startswith("0.8"):
            click.echo(f"  [skip] metagpt=={version} already installed")
            return
        click.echo(f"  [warn] metagpt=={version} found, want {METAGPT_PIN} — reinstalling")
    click.echo(f"  [run]  pip install \"{METAGPT_PIN}\" (several minutes)")
    try:
        subprocess.run(
            [str(paths.python), "-m", "pip", "install", "--upgrade", "pip"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [str(paths.python), "-m", "pip", "install", METAGPT_PIN],
            check=True, capture_output=False,  # stream output so it feels alive
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"pip install failed (exit {e.returncode})") from e


def step_3_make_dirs(paths: CrewPaths) -> None:
    """Step 3 — crew log dir (crews write debug telemetry to files, NEVER
    stderr — stderr is a fault signal to the bridge)."""
    if paths.logs_dir.is_dir():
        click.echo(f"  [skip] logs dir exists at {paths.logs_dir}")
        return
    click.echo(f"  [run]  mkdir {paths.logs_dir}")
    paths.logs_dir.mkdir(parents=True, exist_ok=True)


def step_4_deploy_files(paths: CrewPaths) -> None:
    """Step 4 — copy the crew-side sources from the repo to the live dir.

    Always refreshes (the repo is the source of truth). Never copies the
    other direction; never touches the venv."""
    if not SOURCE_DIR.is_dir():
        raise click.ClickException(f"source dir missing: {SOURCE_DIR}")
    for rel in DEPLOY_FILES:
        src = SOURCE_DIR / rel
        dst = paths.crew_root / rel
        if not src.is_file():
            raise click.ClickException(f"source file missing: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        click.echo(f"  [run]  deployed {rel}")


def step_5_verify(paths: CrewPaths) -> None:
    """Step 5 — post-install checks (spec §1.4 dep-tree spot-check):
      (a) metagpt imports + reports a 0.8.x version
      (b) pydantic is v2
      (c) the boundary module parses the canonical smoke payload
      (d) the HumanProvider subclass loads (import-time errors here would
          block future HITL crews)
      (e) no synapse-orch-ai in the crew venv (lane separation)"""
    if not paths.python.is_file():
        raise click.ClickException(f"crew venv missing at {paths.venv_dir}")

    checks = [
        ("metagpt imports", "import metagpt; print(getattr(metagpt, '__version__', 'unknown'))"),
        ("pydantic v2", "import pydantic; assert str(pydantic.VERSION).startswith('2.'), pydantic.VERSION; print(pydantic.VERSION)"),
        ("boundary parses", (
            "from _shared.boundary import CrewInput; "
            "ci = CrewInput.model_validate({'crew_verb':'hello_world','run_id':'smk0001a',"
            "'workspace':{'type':'general','name':'_smoke','sensitivity_tier':'internal'},"
            "'args':{'topic':'test'},'cost_cap_tokens':10000,"
            "'llm_config':{'provider':'ollama','base_url':'http://127.0.0.1:11434',"
            "'model_analyst':'qwen3:14b','model_reviewer':'qwen3:8b','model_synthesist':'qwen3:14b'}}); "
            "print(ci.run_id)"
        )),
        ("HumanProvider subclass loads", "from _shared.human_provider import ANTONHumanProvider; print('ok')"),
        # Shared vault-scan + the two crews that import it (integration): catches
        # a missing _shared/vault_scan.py deploy that would crash /explore +
        # /debate at import (codex correctness HIGH).
        ("vault_scan loads", "from _shared.vault_scan import scan_vault_for_target, vault_root; print('ok')"),
        ("explore crew imports", "import explore_crew; print(explore_crew.MANIFEST['verb'])"),
        ("debate crew imports", "import debate_crew; print(debate_crew.MANIFEST['verb'])"),
        # #ingest-digest — the digest crew + its deterministic helpers import
        # (metagpt present here; pypdfium/docx are deferred so this passes even
        # before step 2b, but its analyzer/extract modules must parse).
        ("digest crew imports", "import digest_crew; print(digest_crew.MANIFEST['verb'])"),
        ("digest classifier loads", "from _shared.digest.classifier import classify_doc; print('ok')"),
        ("triage crew imports", "import triage_crew; print(triage_crew.MANIFEST['verb'])"),
    ]
    for label, code in checks:
        r = subprocess.run(
            [str(paths.python), "-c", code],
            cwd=str(paths.crew_root), check=False, capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise click.ClickException(
                f"verify failed [{label}]: {r.stderr.strip()[:800]}"
            )
        click.echo(f"  [ok]   {label}: {r.stdout.strip()}")

    r = subprocess.run(
        [str(paths.python), "-m", "pip", "show", "synapse-orch-ai"],
        check=False, capture_output=True, text=True,
    )
    if r.returncode == 0:
        raise click.ClickException(
            "synapse-orch-ai found in the CREW venv — lane separation broken "
            "(the composite engine has its own venv). Uninstall + investigate."
        )
    click.echo("  [ok]   no synapse-orch-ai in crew venv")


# ────────────────────────────────────────────────────────────────────────────
# Click CLI
# ────────────────────────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--crew-root",
    default=str(DEFAULT_CREW_ROOT),
    type=click.Path(path_type=Path),
    help="Crew install root (venv + deployed modules live under here).",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, crew_root: Path) -> None:
    """Install + manage the ANTON crew engine (MetaGPT, isolated venv)."""
    ctx.ensure_object(dict)
    ctx.obj["paths"] = CrewPaths(crew_root=crew_root)


@cli.command("install")
@click.pass_context
def cmd_install(ctx: click.Context) -> None:
    """Run the full install (idempotent): venv → pip → dirs → deploy → verify."""
    paths: CrewPaths = ctx.obj["paths"]
    click.echo(f"install_metagpt v0 — target: {paths.crew_root}")
    click.echo("-" * 70)
    step_1_create_venv(paths)
    step_2_pip_install_metagpt(paths)
    step_3_make_dirs(paths)
    step_4_deploy_files(paths)
    step_5_verify(paths)
    click.echo("-" * 70)
    click.echo("install complete")
    click.echo(f"  crew python : {paths.python}")
    click.echo("  smoke test  : echo CrewInput-JSON | "
               f"\"{paths.python}\" -m hello_world_crew  (needs Ollama up)")


@cli.command("deploy-files")
@click.pass_context
def cmd_deploy_files(ctx: click.Context) -> None:
    """Refresh the deployed crew-side modules from the repo (step 4 only)."""
    paths: CrewPaths = ctx.obj["paths"]
    step_3_make_dirs(paths)
    step_4_deploy_files(paths)


@cli.command("verify")
@click.pass_context
def cmd_verify(ctx: click.Context) -> None:
    """Run the post-install checks (step 5 only)."""
    paths: CrewPaths = ctx.obj["paths"]
    step_5_verify(paths)


@cli.command("status")
@click.pass_context
def cmd_status(ctx: click.Context) -> None:
    """Report install state. Read-only."""
    paths: CrewPaths = ctx.obj["paths"]
    click.echo(f"crew root   : {paths.crew_root}")
    click.echo(f"venv python : {'OK' if paths.python.is_file() else 'MISSING'} ({paths.python})")
    for rel in DEPLOY_FILES:
        deployed = (paths.crew_root / rel).is_file()
        click.echo(f"deployed    : {'OK' if deployed else 'MISSING'}  {rel}")
    click.echo(f"logs dir    : {'OK' if paths.logs_dir.is_dir() else 'MISSING'}")
    if paths.python.is_file():
        r = subprocess.run(
            [str(paths.python), "-m", "pip", "show", "metagpt"],
            check=False, capture_output=True, text=True,
        )
        version = next(
            (ln.split(':', 1)[1].strip() for ln in r.stdout.splitlines()
             if ln.startswith("Version:")),
            None,
        ) if r.returncode == 0 else None
        click.echo(f"metagpt     : {version or 'NOT INSTALLED'}")


def main() -> None:  # pragma: no cover
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()
