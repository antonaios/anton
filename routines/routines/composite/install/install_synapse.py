"""install_synapse — scripted, idempotent installer for the ANTON-side
Synapse composite engine.

Encodes the 8-step workaround the 2026-05-26 overnight spike validated
empirically (see ``<repo>/SYNAPSE-SPIKE-RESULTS-2026-05-26.md``
Items 1 + § "Operator decisions surfaced" #3). Bypasses:

  * ``synapse start`` CLI's Node ≥20.9 prerequisite (host now on Node 24
    but the installer still copes with 18 — backend itself runs fine);
  * the interactive ``synapse setup`` wizard (no ``--non-interactive``
    flag exists upstream — we write ``data/settings.json`` directly from
    ``synapse.setup_wizard.DEFAULT_SETTINGS``);
  * Windows cp1252 console crashes on the engine's stdout arrows / emoji
    (sets ``PYTHONIOENCODING=utf-8`` + ``PYTHONUTF8=1`` in the launcher
    env).

The script is **safe to re-run**. Every step probes for existing state
and skips/refreshes accordingly. Failure modes are explicit (Click's
``BadParameter`` / ``ClickException``) — no silent skips.

Usage (run from anywhere; absolute paths are baked in)::

    python install_synapse.py install        # full install
    python install_synapse.py start          # launch backend only
    python install_synapse.py stop           # stop running backend
    python install_synapse.py status         # report install + run state
    python install_synapse.py register-task  # Windows Scheduled Task autostart

All commands accept ``--data-dir`` and ``--port`` overrides if the
operator wants to relocate the install. Defaults match the spike layout.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click


# ────────────────────────────────────────────────────────────────────────────
# Defaults (match the 2026-05-26 spike install at ``<repo>/synapse``)
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_INSTALL_ROOT = Path("<repo>/synapse")
DEFAULT_DATA_DIR = DEFAULT_INSTALL_ROOT / "data"
DEFAULT_VENV_DIR = DEFAULT_INSTALL_ROOT / ".venv"
DEFAULT_BACKEND_PORT = 9100
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
SYNAPSE_PIN = "synapse-orch-ai==1.6.4"
SCHEDULED_TASK_NAME = "ANTON-Synapse-Backend"

logger = logging.getLogger("install_synapse")


# ────────────────────────────────────────────────────────────────────────────
# Tool + agent placeholders. Operator extends after promotion.
# ────────────────────────────────────────────────────────────────────────────

PLACEHOLDER_CUSTOM_TOOLS: list[dict[str, Any]] = [
    # Real ANTON tools land here once #26b ships. For now the recall tool
    # from the spike is preserved so a fresh install can repro the
    # roundtrip test from SYNAPSE-SPIKE-RESULTS Item 2.
    {
        "name": "anton_recall",
        "tool_type": "http",
        "method": "POST",
        "url": f"{DEFAULT_BRIDGE_URL}/api/recall",
        # Sec-Fetch-Site: none attests this is a non-browser loopback caller so
        # the bridge's CSRF guard (routines/api/middleware/security.py, F-1)
        # accepts it after the fail-closed change — a state-changing request
        # with neither Origin nor a same-origin/none Sec-Fetch-Site is now 403'd.
        "headers": {"Content-Type": "application/json", "Sec-Fetch-Site": "none"},
        "description": (
            "ANTON recall: semantic search across the vault. Returns hits "
            "and optional synthesis. Backed by routines.recall."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string."},
                "synthesise": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to LLM-synthesise the hits.",
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "description": "Max hits (1-50).",
                },
            },
            "required": ["query"],
        },
    },
    # PLACEHOLDER for compose_pitch_payload (delivered by #26b promotion).
    # The installer ships the shape so a fresh install is ready the moment
    # #26b's compose_proxy.py is promoted into routines/api/routes/. Until
    # then, calling this tool returns 404 from the bridge — visible failure,
    # no silent skip.
    {
        "name": "compose_pitch_payload",
        "tool_type": "http",
        "method": "POST",
        "url": f"{DEFAULT_BRIDGE_URL}/api/composite/_compose/compose_pitch_payload",
        # Sec-Fetch-Site: none — see anton_recall above: non-browser loopback
        # attestation for the bridge's fail-closed CSRF guard (F-1).
        "headers": {"Content-Type": "application/json", "Sec-Fetch-Site": "none"},
        "description": (
            "Bridge-side compose proxy (TRANSFORM substitute, no Docker). "
            "Shapes a PitchPayload dict from the per-step shared_state subset."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "shared_state_subset": {
                    "type": "object",
                    "description": "Slice of Synapse shared_state to shape.",
                }
            },
            "required": ["shared_state_subset"],
        },
    },
]


PLACEHOLDER_HOST_AGENTS: list[dict[str, Any]] = [
    # One placeholder host agent per future composite. The /pitch host
    # agent enumerates every tool the /pitch DAG uses; the dispatcher
    # gates tool visibility on agent membership (spike Item 2 Finding #3).
    {
        "id": "agent_pitch_host",
        "name": "Pitch Composite Host",
        "description": (
            "Host agent for the /pitch composite. Enumerates every ANTON "
            "tool the pitch DAG calls. Required by Synapse — custom tools "
            "are invisible to the dispatcher unless they appear in some "
            "agent's `tools` list (spike Item 2 Finding #3)."
        ),
        "avatar": "default",
        "type": "conversational",
        "tools": [
            "anton_recall",
            "compose_pitch_payload",
            # Real composite tools land here once their bridge routes
            # exist (lbo, comps, dcf, research, buyer_list,
            # ppt_assemble, etc.). Placeholder keeps the shape valid.
        ],
        "repos": [],
        "db_configs": [],
        "system_prompt": (
            "You are the host agent for the /pitch composite. You only "
            "select tools listed in your `tools` array. Use the composite "
            "orchestration JSON as your DAG; do not improvise step order."
        ),
        "orchestration_id": None,
        "model": None,
        "provider": None,
        "max_turns": None,
    },
]


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class InstallPaths:
    install_root: Path
    venv_dir: Path
    data_dir: Path
    backend_port: int

    @property
    def python(self) -> Path:
        """Path to the venv's python.exe (Windows) or python (POSIX)."""
        if sys.platform == "win32":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    @property
    def backend_main(self) -> Path:
        """Path to ``backend/main.py`` inside the installed wheel."""
        if sys.platform == "win32":
            return self.venv_dir / "Lib" / "site-packages" / "backend" / "main.py"
        # POSIX site-packages location varies by python minor — discover.
        site_packages = next(
            (self.venv_dir / "lib").glob("python*/site-packages"), None
        )
        if site_packages is None:
            raise click.ClickException(
                f"Could not locate site-packages under {self.venv_dir}/lib"
            )
        return site_packages / "backend" / "main.py"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def custom_tools_file(self) -> Path:
        return self.data_dir / "custom_tools.json"

    @property
    def user_agents_file(self) -> Path:
        return self.data_dir / "user_agents.json"

    @property
    def backend_log(self) -> Path:
        return self.data_dir / "backend.log"


def _build_launch_env(paths: InstallPaths, jwt_secret: str) -> dict[str, str]:
    """Compose the env vars the spike validated as necessary."""
    env = os.environ.copy()
    env.update(
        {
            "SYNAPSE_DATA_DIR": str(paths.data_dir),
            "SYNAPSE_BACKEND_PORT": str(paths.backend_port),
            "SYNAPSE_JWT_SECRET": jwt_secret,
            # Windows cp1252 crash mitigation — the engine prints arrows
            # + emoji liberally. Without these, first run dies with
            # ``UnicodeEncodeError: 'charmap' codec can't encode '→'``.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _is_port_listening(port: int) -> bool:
    """Cheap port probe via ``socket``. No external deps."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def _wait_for_backend(port: int, timeout_s: float = 30.0) -> bool:
    """Poll ``port`` until it answers or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _is_port_listening(port):
            return True
        time.sleep(0.5)
    return False


def _build_default_settings(port: int) -> dict[str, Any]:
    """Synapse DEFAULT_SETTINGS with the ANTON-correct overrides.

    Source template: ``synapse.setup_wizard.DEFAULT_SETTINGS`` (see the
    installed wheel at
    ``<venv>/Lib/site-packages/synapse/setup_wizard.py:26``). We avoid
    importing from the venv at install-time because the venv may not
    exist yet on a cold run — we redeclare the canonical shape here
    and document the source path.
    """
    return {
        "agent_name": "Synapse",
        "model": "qwen3:14b",
        "mode": "local",
        "openai_key": "",
        "anthropic_key": "",
        "gemini_key": "",
        "google_maps_api_key": "",
        "login_enabled": False,
        "login_username": "admin",
        "login_password_hash": "",
        "bedrock_api_key": "",
        "bedrock_inference_profile": "",
        "embedding_model": "",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_session_token": "",
        "aws_region": "us-east-1",
        "sql_connection_string": "",
        "ollama_base_url": "http://127.0.0.1:11434",
        "openai_compatible_key": "",
        "openai_compatible_base_url": "",
        "openai_compatible_models": "",
        "local_compatible_base_url": "",
        "local_compatible_key": "",
        "local_compatible_models": "",
        "openai_compatible_embed_models": "",
        "local_compatible_embed_models": "",
        "n8n_url": "http://localhost:5678",
        "n8n_api_key": "",
        "n8n_table_id": "",
        "global_config": {},
        "vault_enabled": True,
        "vault_threshold": 100000,
        "coding_agent_enabled": True,
        "report_agent_enabled": True,
        "backend_port": port,
        "frontend_port": 3000,
    }


def _generate_jwt_secret() -> str:
    """64-char hex secret. Persisted to data/.jwt_secret so re-runs use
    the same value (otherwise every restart invalidates extant sessions)."""
    import secrets

    return secrets.token_hex(32)


def _load_or_create_jwt_secret(data_dir: Path) -> str:
    """Idempotent JWT secret persistence."""
    secret_file = data_dir / ".jwt_secret"
    if secret_file.is_file():
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = _generate_jwt_secret()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


# ────────────────────────────────────────────────────────────────────────────
# Install steps (each idempotent + verifiable)
# ────────────────────────────────────────────────────────────────────────────


def step_1_create_venv(paths: InstallPaths) -> None:
    """Step 1 — ``python -m venv <install_root>/.venv``."""
    if paths.python.is_file():
        click.echo(f"  [skip] venv already exists at {paths.venv_dir}")
        return
    paths.install_root.mkdir(parents=True, exist_ok=True)
    click.echo(f"  [run]  creating venv at {paths.venv_dir}")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(paths.venv_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"venv creation failed: {e.stderr.strip() or e.stdout.strip()}"
        ) from e
    if not paths.python.is_file():
        raise click.ClickException(
            f"venv created but {paths.python} not found — installer cannot proceed"
        )


def step_2_pip_install_synapse(paths: InstallPaths) -> None:
    """Step 2 — pip install the version-pinned Synapse wheel into the venv.

    Pinned to v1.6.4 (the spike-validated version). Upgrades are
    operator-explicit: re-pin in ``SYNAPSE_PIN``, re-run installer.
    """
    # Probe whether the pinned version is already installed.
    try:
        result = subprocess.run(
            [str(paths.python), "-m", "pip", "show", "synapse-orch-ai"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            installed_version = None
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    installed_version = line.split(":", 1)[1].strip()
                    break
            target_version = SYNAPSE_PIN.split("==", 1)[1]
            if installed_version == target_version:
                click.echo(
                    f"  [skip] synapse-orch-ai=={installed_version} "
                    f"already installed"
                )
                return
            click.echo(
                f"  [warn] found synapse-orch-ai=={installed_version}, "
                f"want {target_version} — reinstalling"
            )
    except Exception as e:  # noqa: BLE001 — diagnostic only
        logger.debug("pip show probe failed (continuing to install): %s", e)

    click.echo(f"  [run]  pip install {SYNAPSE_PIN}")
    try:
        subprocess.run(
            [str(paths.python), "-m", "pip", "install", SYNAPSE_PIN],
            check=True,
            capture_output=False,  # stream pip output so 6-min install feels alive
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"pip install failed (exit {e.returncode})") from e
    if not paths.backend_main.is_file():
        raise click.ClickException(
            f"pip install succeeded but {paths.backend_main} not found"
        )


def step_3_make_data_dir(paths: InstallPaths) -> None:
    """Step 3 — ``mkdir <install_root>/data``."""
    if paths.data_dir.is_dir():
        click.echo(f"  [skip] data dir exists at {paths.data_dir}")
        return
    click.echo(f"  [run]  mkdir {paths.data_dir}")
    paths.data_dir.mkdir(parents=True, exist_ok=True)


def step_4_write_settings(paths: InstallPaths) -> None:
    """Step 4 — write ``data/settings.json`` from DEFAULT_SETTINGS template.

    Template source (documented in ``_build_default_settings`` docstring):
    ``<venv>/Lib/site-packages/synapse/setup_wizard.py:26``. We don't
    overwrite an existing settings.json — the operator may have edited
    it. Re-runs are no-ops unless ``--force-settings`` is added later.
    """
    if paths.settings_file.is_file():
        click.echo(
            f"  [skip] settings.json exists at {paths.settings_file} "
            f"(operator-editable; not overwriting)"
        )
        return
    click.echo(f"  [run]  writing settings.json (mode=local, port={paths.backend_port})")
    settings = _build_default_settings(paths.backend_port)
    paths.settings_file.write_text(
        json.dumps(settings, indent=4), encoding="utf-8"
    )


def step_5_write_custom_tools(paths: InstallPaths) -> None:
    """Step 5 — write ``data/custom_tools.json`` with ANTON HTTP tools.

    Ships with one working tool (``anton_recall``) and one placeholder
    (``compose_pitch_payload``) that lights up after #26b is promoted.
    Operator extends post-promotion by adding entries to
    ``PLACEHOLDER_CUSTOM_TOOLS`` and re-running ``install`` (or by
    POSTing to ``/api/tools/custom`` against the running backend).
    """
    existing: list[dict[str, Any]] = []
    if paths.custom_tools_file.is_file():
        try:
            existing = json.loads(paths.custom_tools_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise click.ClickException(
                f"custom_tools.json exists but is invalid JSON: {e}. "
                f"Delete it manually then re-run installer."
            ) from e

    existing_names = {t.get("name") for t in existing}
    additions = [
        t for t in PLACEHOLDER_CUSTOM_TOOLS if t["name"] not in existing_names
    ]
    if not additions:
        click.echo(
            f"  [skip] custom_tools.json already contains all placeholders "
            f"({len(existing)} tools total)"
        )
        return
    merged = existing + additions
    click.echo(
        f"  [run]  writing {len(additions)} ANTON tool(s) to custom_tools.json "
        f"({len(merged)} total)"
    )
    paths.custom_tools_file.write_text(
        json.dumps(merged, indent=4), encoding="utf-8"
    )


def step_6_write_user_agents(paths: InstallPaths) -> None:
    """Step 6 — write ``data/user_agents.json`` with composite host agents.

    Synapse auto-populates Builder agents on first backend start. We
    pre-seed the file with composite host agents so the dispatcher
    sees them immediately. If the file exists (Synapse already ran
    once and added its Builders), we merge the host agents in by ID.
    """
    existing: list[dict[str, Any]] = []
    if paths.user_agents_file.is_file():
        try:
            existing = json.loads(paths.user_agents_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise click.ClickException(
                f"user_agents.json exists but is invalid JSON: {e}. "
                f"Delete it manually then re-run installer."
            ) from e

    existing_ids = {a.get("id") for a in existing}
    additions = [a for a in PLACEHOLDER_HOST_AGENTS if a["id"] not in existing_ids]
    if not additions:
        click.echo(
            f"  [skip] user_agents.json already contains all composite host "
            f"agents ({len(existing)} agents total)"
        )
        return
    merged = existing + additions
    click.echo(
        f"  [run]  adding {len(additions)} composite host agent(s) to "
        f"user_agents.json ({len(merged)} total)"
    )
    paths.user_agents_file.write_text(
        json.dumps(merged, indent=4), encoding="utf-8"
    )


def step_7_launch_backend(paths: InstallPaths, jwt_secret: str) -> int | None:
    """Step 7 — launch ``python backend/main.py`` with the validated env.

    Returns the PID. Idempotent: if the port is already listening, we
    don't double-spawn (would race + fail bind). Output is appended to
    ``data/backend.log`` so the operator can tail it.
    """
    if _is_port_listening(paths.backend_port):
        click.echo(
            f"  [skip] backend already listening on :{paths.backend_port}"
        )
        return None

    env = _build_launch_env(paths, jwt_secret)
    click.echo(
        f"  [run]  launching backend (port {paths.backend_port}, log → "
        f"{paths.backend_log})"
    )
    log_handle = paths.backend_log.open("ab")
    try:
        proc = subprocess.Popen(
            [str(paths.python), str(paths.backend_main)],
            cwd=str(paths.backend_main.parent),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            # Detach on Windows so backend survives installer exit.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                if sys.platform == "win32"
                else 0
            ),
        )
    except OSError as e:
        log_handle.close()
        raise click.ClickException(f"backend launch failed: {e}") from e

    if not _wait_for_backend(paths.backend_port, timeout_s=30.0):
        raise click.ClickException(
            f"backend did not bind :{paths.backend_port} within 30s — "
            f"check {paths.backend_log}"
        )
    click.echo(f"  [ok]   backend up on :{paths.backend_port} (PID {proc.pid})")
    return proc.pid


def step_8_register_scheduled_task(paths: InstallPaths) -> None:
    """Step 8 — register a Windows Scheduled Task for autostart at logon.

    Idempotent via ``schtasks /Delete`` + ``/Create``. Skipped silently
    on non-Windows (POSIX path uses systemd / launchd, out of scope for
    this installer).
    """
    if sys.platform != "win32":
        click.echo("  [skip] Scheduled Task registration is Windows-only")
        return

    # Delete-then-create for idempotency. Suppress "task not found" on
    # cold runs by ignoring delete failures.
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", SCHEDULED_TASK_NAME, "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise click.ClickException(
            "schtasks.exe not found on PATH — Windows Task Scheduler "
            "unavailable"
        ) from e

    # The /TR command must encode the launcher inline. We use cmd.exe
    # to set env vars then exec python directly.
    launcher_cmd = (
        f'cmd.exe /c "set SYNAPSE_DATA_DIR={paths.data_dir}'
        f' && set SYNAPSE_BACKEND_PORT={paths.backend_port}'
        f' && set PYTHONIOENCODING=utf-8'
        f' && set PYTHONUTF8=1'
        f' && {paths.python} {paths.backend_main}'
        f' >> {paths.backend_log} 2>&1"'
    )

    click.echo(f"  [run]  registering Scheduled Task '{SCHEDULED_TASK_NAME}'")
    try:
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                SCHEDULED_TASK_NAME,
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TR",
                launcher_cmd,
                "/F",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"Scheduled Task registration failed: "
            f"{e.stderr.strip() or e.stdout.strip()}"
        ) from e
    click.echo(f"  [ok]   task '{SCHEDULED_TASK_NAME}' registered for logon")


# ────────────────────────────────────────────────────────────────────────────
# Click CLI
# ────────────────────────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--install-root",
    default=str(DEFAULT_INSTALL_ROOT),
    type=click.Path(path_type=Path),
    help="Synapse install root (venv + data live under here).",
    show_default=True,
)
@click.option(
    "--port",
    default=DEFAULT_BACKEND_PORT,
    type=int,
    help="Backend port to bind (default: 9100; bridge owns 8765).",
    show_default=True,
)
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, install_root: Path, port: int, verbose: bool) -> None:
    """Install + manage the ANTON-side Synapse composite engine."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["paths"] = InstallPaths(
        install_root=install_root,
        venv_dir=install_root / ".venv",
        data_dir=install_root / "data",
        backend_port=port,
    )


@cli.command("install")
@click.option(
    "--register-task/--no-register-task",
    default=True,
    help="Register Windows Scheduled Task for autostart at logon (default ON per "
         "operator 2026-05-27 — zero-thought new-machine setup).",
)
@click.option(
    "--no-start",
    is_flag=True,
    help="Run steps 1-6 + 8 only; do not launch the backend.",
)
@click.pass_context
def cmd_install(ctx: click.Context, register_task: bool, no_start: bool) -> None:
    """Run the full 8-step install (idempotent).

    Autostart is registered by default. New-machine setup: ``install_synapse install``
    is sufficient — operator needs no further action. Override with
    ``--no-register-task`` if you want manual-start only.
    """
    paths: InstallPaths = ctx.obj["paths"]
    click.echo(f"install_synapse v0 — target: {paths.install_root}")
    click.echo("─" * 70)

    step_1_create_venv(paths)
    step_2_pip_install_synapse(paths)
    step_3_make_data_dir(paths)
    step_4_write_settings(paths)
    step_5_write_custom_tools(paths)
    step_6_write_user_agents(paths)

    jwt_secret = _load_or_create_jwt_secret(paths.data_dir)

    if no_start:
        click.echo("  [skip] backend launch (--no-start)")
    else:
        step_7_launch_backend(paths, jwt_secret)

    # NB: register_task defaults to True per operator 2026-05-27 (zero-thought
    # new-machine setup). Override with --no-register-task for manual-start.
    if register_task:
        step_8_register_scheduled_task(paths)
    else:
        click.echo("  [skip] Scheduled Task autostart (--no-register-task passed)")

    click.echo("─" * 70)
    click.echo("install complete")
    click.echo(f"  backend       : http://127.0.0.1:{paths.backend_port}")
    click.echo(f"  data dir      : {paths.data_dir}")
    click.echo(f"  log           : {paths.backend_log}")
    click.echo("  next          : run smoke tests in INSTALL-README.md §3")


@cli.command("start")
@click.pass_context
def cmd_start(ctx: click.Context) -> None:
    """Launch the backend only (steps 7). Safe to re-run."""
    paths: InstallPaths = ctx.obj["paths"]
    if not paths.python.is_file():
        raise click.ClickException(
            f"venv not found at {paths.venv_dir} — run `install` first"
        )
    jwt_secret = _load_or_create_jwt_secret(paths.data_dir)
    step_7_launch_backend(paths, jwt_secret)


@cli.command("stop")
@click.pass_context
def cmd_stop(ctx: click.Context) -> None:
    """Stop any backend listening on the configured port."""
    paths: InstallPaths = ctx.obj["paths"]
    if not _is_port_listening(paths.backend_port):
        click.echo(f"  [skip] nothing listening on :{paths.backend_port}")
        return
    if sys.platform != "win32":
        raise click.ClickException(
            "stop is currently Windows-only — use `pkill -f backend/main.py` "
            "on POSIX"
        )
    # Find the PID owning the port via Get-NetTCPConnection.
    ps = shutil.which("powershell")
    if ps is None:
        raise click.ClickException("powershell.exe not on PATH")
    try:
        out = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-Command",
                f"Get-NetTCPConnection -LocalPort {paths.backend_port} "
                f"-State Listen | Select-Object -ExpandProperty OwningProcess",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"port owner lookup failed: {e.stderr}") from e
    pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    for pid in pids:
        click.echo(f"  [run]  Stop-Process -Id {pid} -Force")
        subprocess.run(
            [ps, "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            check=False,
        )


@cli.command("status")
@click.pass_context
def cmd_status(ctx: click.Context) -> None:
    """Report install + run state. Read-only."""
    paths: InstallPaths = ctx.obj["paths"]
    click.echo(f"install root : {paths.install_root}")
    click.echo(f"venv         : {'OK' if paths.python.is_file() else 'MISSING'}")
    click.echo(
        f"backend main : {'OK' if paths.backend_main.is_file() else 'MISSING'}"
    )
    click.echo(
        f"data dir     : {'OK' if paths.data_dir.is_dir() else 'MISSING'}"
    )
    click.echo(
        f"settings     : {'OK' if paths.settings_file.is_file() else 'MISSING'}"
    )
    click.echo(
        f"custom tools : {'OK' if paths.custom_tools_file.is_file() else 'MISSING'}"
    )
    click.echo(
        f"user agents  : {'OK' if paths.user_agents_file.is_file() else 'MISSING'}"
    )
    listening = _is_port_listening(paths.backend_port)
    click.echo(
        f"backend port : :{paths.backend_port} "
        f"{'LISTENING' if listening else 'IDLE'}"
    )
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", SCHEDULED_TASK_NAME],
                check=False,
                capture_output=True,
                text=True,
            )
            task_state = "REGISTERED" if r.returncode == 0 else "NOT REGISTERED"
        except FileNotFoundError:
            task_state = "schtasks.exe unavailable"
        click.echo(f"sched task   : {task_state}")


@cli.command("register-task")
@click.pass_context
def cmd_register_task(ctx: click.Context) -> None:
    """Register the Windows Scheduled Task only (step 8)."""
    paths: InstallPaths = ctx.obj["paths"]
    step_8_register_scheduled_task(paths)


def main() -> None:  # pragma: no cover
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()
