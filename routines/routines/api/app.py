"""FastAPI bridge for the routines CLI surface.

Run from the routines repo:

    python -m routines.api.app

Binds to 127.0.0.1:8765 by default. A non-loopback ``AGENTIC_API_HOST``
refuses to start unless ``AGENTIC_ALLOW_PUBLIC_BIND=1`` is set
(#ops-bind-guard) — and then it logs a loud warning.

Dashboard delivery mode is env-driven (``AGENTIC_DASHBOARD_MODE``):
  * **development** (default) — Vite dev server runs on :5173 and proxies
    /api/* here. Bridge serves only the API.
  * **production** — bridge ALSO serves ``dashboard/dist/`` at ``/``. No
    Vite process. Cuts idle RAM (Vite + HMR + esbuild watcher are gone)
    at the cost of needing ``npm run build`` before UI changes are visible.

Set ``AGENTIC_DASHBOARD_MODE=production`` via ``setx`` for permanence; the
start script (``scripts/start-agentic-os.ps1``) reads the same variable
and skips spawning Vite when production. ``AGENTIC_DASHBOARD_DIST`` can
override the dist path if the layout changes.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from routines.api.middleware.run_id import RunIdMiddleware
from routines.api.middleware.security import SecurityHeadersMiddleware
from routines.api.routes import (
    actions_decay as actions_decay_routes,
    activity as activity_routes,
    attachments as attachments_routes,
    audit as audit_routes,
    bd_decay as bd_decay_routes,
    budgets as budgets_routes,
    sensitivity_overrides as sensitivity_overrides_routes,
    mnpi_attestations as mnpi_attestations_routes,
    compose as compose_routes,
    comps as comps_routes,
    credentials as credentials_routes,
    crew as crew_routes,
    crew_providers as crew_providers_routes,
    daily as daily_routes,
    daily_digest as daily_digest_routes,
    dashboard as dashboard_routes,
    deal_tracker as deal_tracker_routes,
    dismissals as dismissals_routes,
    drafts as drafts_routes,
    earnings as earnings_routes,
    equity_research as equity_research_routes,
    intake as intake_routes,
    lane_status as lane_status_routes,
    lbo as lbo_routes,
    lbo_intake_agent as lbo_intake_agent_routes,
    lessons_suggest as lessons_suggest_routes,
    markets as markets_routes,
    morning_brief as morning_brief_routes,
    morning_brief_skill as morning_brief_skill_routes,
    operator_config as operator_config_routes,
    project_chat as project_chat_routes,
    projects as projects_routes,
    promotion as promotion_routes,
    proposals as proposals_routes,
    pulse as pulse_routes,
    recall as recall_routes,
    recall_query as recall_query_routes,
    routing_matrix as routing_matrix_routes,
    scheduler as scheduler_routes,
    sectornews as sectornews_routes,
    sessions as sessions_routes,
    skill_runs as skill_runs_routes,
    skills_providers as skills_providers_routes,
    telemetry as telemetry_routes,
    ticker_multiples as ticker_multiples_routes,
    usage as usage_routes,
    vault_graph as vault_graph_routes,
    vault_health as vault_health_routes,
    workspaces as workspaces_routes,
)
from routines.hooks.central_guards import SkillPreconditionsNotMet
from routines.skills._runtime.guardrails import GuardrailRetriesExhausted
from routines.scheduler import get_scheduler

# #26b — import the compose-key package so per-key handlers self-register
# with the ComposeRegistry at startup (each ``routines/composite/compose/
# <key>.py`` calls ``register_compose_key`` at module-import time).
import routines.composite.compose  # noqa: F401


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Pre-warm the markets provider in the main thread.

    OpenBB's package_builder.auto_build() installs SIGTERM/SIGINT
    handlers via signal.signal(), which only works in the main
    interpreter thread. If the first call hits a FastAPI route
    handler (worker thread), the build crashes with "signal only
    works in main thread". By forcing the import here at app-
    startup time, the build runs in the main thread and subsequent
    imports skip it.
    """
    # #plan-tier-toggle — seed the live plan tier from the persisted operator
    # choice (routines/state/plan_tier.json) BEFORE any routing. A UI flip thus
    # survives a bridge restart and is authoritative over the launcher env.
    try:
        from routines.shared.routing import load_persisted_plan_tier
        load_persisted_plan_tier()
    except Exception as e:  # noqa: BLE001 — never block boot on tier seeding
        logging.getLogger("uvicorn").warning("plan-tier seed failed: %s", e)

    try:
        from routines.markets import get_provider
        provider = get_provider()
        logging.getLogger("uvicorn").info(
            "markets provider warm: %s", provider.name
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "markets pre-warm failed (will fall back at call time): %s", e
        )

    # F-27 — tighten state/ (credential blobs + DPAPI key) to the operator
    # SID at startup. Idempotent + best-effort: a failure logs and the
    # posture stays DPAPI-only (today's), never worse.
    try:
        from routines.credentials.state_acl import harden_state_dir_acl
        harden_state_dir_acl()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "state ACL hardening unavailable: %s", e
        )

    # #operator-tab v2 — bridge stored API keys into the process env so
    # env-reading consumers (sector-news, FRED macro leg) and scheduler
    # subprocesses pick up tab-entered/rotated keys. Store wins inside
    # this process; see routines/credentials/env_bridge.py.
    try:
        from routines.credentials.env_bridge import apply_all
        bridged = apply_all()
        if bridged:
            logging.getLogger("uvicorn").info(
                "credentials env-bridge: %d stored key(s) exported (%s)",
                len(bridged), ", ".join(bridged),
            )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "credentials env-bridge failed (stored keys not exported): %s", e
        )

    # #llm-routing-postjune15 B3 — seed the Agent-SDK monthly-credit budget
    # policy from AGENTIC_AGENT_SDK_CREDIT_USD (idempotent + non-clobbering;
    # no-op when the env var is unset). Models the plan credit as a #57
    # provider-scope cap so the existing gate + incident + ack machinery
    # enforces credit exhaustion. Best-effort: a failure logs and the operator
    # can still create the policy via /api/budgets.
    try:
        from routines.budgets.seed import seed_agent_sdk_credit
        seeded = seed_agent_sdk_credit()
        if seeded is not None:
            logging.getLogger("uvicorn").info(
                "Agent-SDK credit policy seeded: cap_usd=%s", seeded.cap_usd,
            )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "Agent-SDK credit seed failed (create it via /api/budgets): %s", e
        )

    # Embedded scheduler (#23) — see routines/scheduler/README.md.
    scheduler = get_scheduler()
    try:
        scheduler.start()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "scheduler start failed (jobs disabled until restart): %s", e
        )

    # #23 finish — register the cron jobs (morning-brief, daily-digest,
    # vault-health-*, sector-news). Idempotent (replace_existing=True) so
    # re-registering across reloads is safe. Jobs land in the ``ephemeral``
    # jobstore — durability is via re-registration on every lifespan start.
    try:
        from routines.scheduler.jobs import register_all_jobs
        registered = register_all_jobs()
        logging.getLogger("uvicorn").info(
            "scheduler: registered %d cron jobs (%s)",
            len(registered), ", ".join(registered),
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "scheduler: job registration failed (scheduler still running, "
            "but no jobs will fire): %s", e
        )

    # Skill registry scan + startup validation (#61). HARD-FAIL: a skill whose
    # frontmatter is inconsistent (confidential/MNPI skill listing a cloud
    # lane; bad workspace_scope/sensitivity enum; missing required key; non-
    # positive cost ceiling) must NOT reach first-call — refuse to boot. This
    # is deliberately OUTSIDE a swallow-the-exception try/except so the
    # RuntimeError propagates and the bridge fails to start with a structured
    # error naming the offending skill.
    from routines.skills.registry import scan, validate_or_raise
    scan()
    validate_or_raise()
    from routines.skills.registry import _REGISTRY as _skill_registry
    logging.getLogger("uvicorn").info(
        "skill registry: %d skills validated", len(_skill_registry)
    )

    # Composite-template validation (#steal-kocoro P2). Same #61 HARD-FAIL
    # contract as the skill registry above: a malformed composite orchestration
    # JSON (unknown step type, dangling edge, cyclic DAG, a tool/agent step with
    # no host agent pinned, a typed consumer field-accessing a raw tool-output
    # string, or a step more sensitive than the composite's declared max tier)
    # must NOT reach a /pitch run — refuse to BOOT with a structured error naming
    # the offending composite. Validator-only: reads composites/*.orchestration.
    # json, no Synapse contact / network. Pre-Phase-6 the composites dir is
    # absent → no-op. OUTSIDE a swallow so the RuntimeError aborts boot.
    from routines.composite.lint import (
        iter_composite_files,
        validate_or_raise as validate_composite_templates,
    )
    validate_composite_templates()
    logging.getLogger("uvicorn").info(
        "composite templates: %d validated", len(iter_composite_files())
    )

    # Tier 2 (#llm-routing-tier-2): warm the operator provider-overrides
    # sidecar at boot so a hot dispatch path serves it from cache (the loader
    # re-reads on mtime change, so PATCH / hot-edits are still picked up).
    from routines.skills.registry import load_skill_overrides, sidecar_path
    _overrides = load_skill_overrides()
    logging.getLogger("uvicorn").info(
        "provider overrides sidecar: %d skill override(s) loaded from %s",
        len(_overrides), sidecar_path(),
    )

    # LLM telemetry hooks (#13) + tool-side central guards (#22 finish).
    #
    # Importing ``routines.hooks.central_guards`` triggers @before/@after
    # decorator registration of all four guards:
    #   * enforce_sensitivity_lane (@before_llm_call) — fail-closed on
    #     too-permissive lanes for confidential/MNPI. Chat dispatcher
    #     passes a 200k cost-cap on the synthetic SkillRef so the
    #     LLM-side cost-cap guard never trips on long conversations
    #     (see routines/sessions/router.py).
    #   * audit_log (@after_llm_call) — per-call LLM audit row.
    #   * audit_tool_call (@after_tool_call) — per-call tool audit row.
    #   * enforce_workspace_policy (@before_tool_call) — refuses writes
    #     outside [[workspace-write-policy]] §2 paths. Only fires for
    #     tools whose name is in ``_WRITE_TOOL_NAMES`` so the workflow
    #     skills (comps_pull, equity_research, ...) pass through
    #     unrestricted.
    #   * enforce_skill_sensitivity (@before_tool_call, #61) — hard gate
    #     for registered-skill tool calls: refuses a scoped skill on the
    #     wrong workspace type, or any skill on MNPI inputs. Reads
    #     workspace_scope from the registry scanned above. Non-skill tools
    #     (load_skill_metadata → KeyError) pass through.
    # ``register_central_guards()`` adds the bus-side audit subscribers
    # on top.
    # ── SECURITY-CRITICAL guard registration — FAIL-CLOSED (F-35 / BUDGET-5) ──
    # Previously ALL hook wiring (guards + telemetry + counters) sat in ONE
    # broad try/except that logged a warning and let the bridge boot ANYWAY on
    # any failure — so a registration/import error in the sensitivity gate,
    # workspace policy, or #57 budget gate silently disabled the gate while the
    # bridge kept serving ungated. For a SECURITY gate that's the wrong failure
    # mode. Register the security-critical hooks OUTSIDE the swallow and assert
    # they actually attached (by FUNCTION IDENTITY — not by __name__, which a
    # collision could false-pass; codex-5.5 budget r1); a failure here ABORTS
    # boot (loud) instead of quietly running with the gate off. (Importing the
    # modules triggers their @before_*/@after_* self-registration.)
    #
    # The LLM-telemetry WRITE hook is security-critical TOO: the #57 budget gate
    # aggregates monthly spend by reading the rows that hook writes to
    # llm_calls.jsonl. If it failed to register, the gate would boot present but
    # toothless (spend never accumulates → the USD cap never trips). So it lives
    # in this fail-closed block, not the best-effort one (codex-5.5 budget r1).
    from routines.hooks import central_guards as _central_guards
    from routines.hooks.central_guards import (
        enforce_sensitivity_lane,
        enforce_skill_sensitivity,
        enforce_workspace_policy,
    )
    from routines.budgets.gate import enforce_budget_gate
    from routines.skills._runtime import llm_call_cap as _llm_call_cap  # noqa: F401  (#67)
    from routines.telemetry.llm_hooks import (
        register_llm_telemetry_hooks,
        telemetry_write,
    )

    register_llm_telemetry_hooks()
    _central_guards.register_central_guards()

    # The four @before_* security guards self-register at MODULE IMPORT, but a
    # Python import is cached — so if the hook registry was ever cleared (a
    # FastAPI reload, or a test that calls ``hook_registry.clear()``), importing
    # the already-loaded module does NOT re-run the decorators and the guards
    # would be absent. RE-ATTACH any missing one idempotently here (identity
    # check avoids double-registration) so registration is reload-safe, the same
    # contract ``register_llm_telemetry_hooks()`` already honours. The assert
    # below then catches a genuine wiring failure, not a stale import cache.
    from routines.hooks import before_llm_call, before_tool_call, hook_registry as _hook_registry

    def _ensure(decorator, func, phase: str, kind: str) -> None:
        present = {h.func for h in _hook_registry.list(phase=phase, kind=kind)}
        if func not in present:
            decorator(func)

    _ensure(before_llm_call, enforce_sensitivity_lane, "before", "llm")
    _ensure(before_llm_call, enforce_budget_gate, "before", "llm")
    _ensure(before_tool_call, enforce_skill_sensitivity, "before", "tool")
    _ensure(before_tool_call, enforce_workspace_policy, "before", "tool")

    _before_llm = {h.func for h in _hook_registry.list(phase="before", kind="llm")}
    _before_tool = {h.func for h in _hook_registry.list(phase="before", kind="tool")}
    _after_llm = {h.func for h in _hook_registry.list(phase="after", kind="llm")}
    _required = [
        ("enforce_sensitivity_lane (before_llm)", enforce_sensitivity_lane in _before_llm),
        ("enforce_budget_gate (before_llm)", enforce_budget_gate in _before_llm),
        ("enforce_skill_sensitivity (before_tool)", enforce_skill_sensitivity in _before_tool),
        ("enforce_workspace_policy (before_tool)", enforce_workspace_policy in _before_tool),
        ("telemetry spend writer (after_llm)", telemetry_write in _after_llm),
    ]
    _absent = [name for name, present in _required if not present]
    if _absent:
        raise RuntimeError(
            "SECURITY gate registration incomplete — refusing to boot ungated; "
            f"missing hooks: {_absent}"
        )

    # ── Best-effort: documented-inert per-tool sub-cap counter ───────────────
    # #74.5 per-tool sub-caps — sibling of #67, bounds each tool (e.g.
    # ``vault_read``) SEPARATELY via ``@before_tool_call``. Today (pre-#21) no
    # skill declares ``cost_ceiling_tool_*`` so the counter increments for
    # telemetry and nothing is blocked; live the moment a SKILL.md declares a
    # per-tool ceiling. A registration failure here disables nothing
    # security-relevant, so it stays non-fatal.
    try:
        from routines.skills._runtime import tool_call_cap as _tool_call_cap  # noqa: F401
        logging.getLogger("uvicorn").info(
            "hooks: LLM telemetry + central guards + budget gate + "
            "llm_calls cap + tool_calls sub-caps active "
            "(#13 + #22 LLM-side + tool-side + #57 + #67 + #74.5)"
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "hooks: #74.5 per-tool sub-cap registration failed (security gates "
            "+ budget accounting are active; only the inert per-tool counter is "
            "absent): %s", e
        )

    # #75: probe the claude CLI binary so the chat dispatcher can route
    # the cloud lane through `claude -p` (Agent SDK subprocess) instead
    # of the legacy stub. Discovery order: AGENTIC_CLAUDE_CLI_PATH env
    # var → shutil.which("claude") → ~/.local/bin/claude.exe + sibling
    # candidates. Selected path is logged so the operator can grep the
    # bridge log if the wrong binary is picked. When discovery fails,
    # cloud chat falls back to the unwired-stub notice (no crash).
    try:
        from routines.sessions.router import _preflight_claude_cli, _preflight_codex_cli
        _preflight_claude_cli()
        # Tier 1 (#llm-routing-tier-1, 2026-06-03): probe Codex CLI too.
        # Best-effort — Codex absence doesn't block boot; the dispatcher
        # falls back to Anthropic when AGENTIC_CLOUD_PROVIDER=openai is
        # requested but the CLI isn't available.
        _preflight_codex_cli()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning(
            "claude subprocess pre-flight failed (cloud chat falls back to stub): %s",
            e,
        )

    yield

    try:
        scheduler.stop(wait=False)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn").warning("scheduler shutdown error: %s", e)


def _resolve_dashboard_dist() -> Path:
    """Return the dashboard/dist path the bridge should serve in production mode.

    Order of preference:
    1. ``AGENTIC_DASHBOARD_DIST`` env var if set (escape hatch for non-default layouts).
    2. ``<routines-repo>/../dashboard/dist`` (the umbrella layout).
    """
    override = os.environ.get("AGENTIC_DASHBOARD_DIST")
    if override:
        return Path(override)
    # __file__ is …/routines/routines/api/app.py — climb to the umbrella, then dashboard/dist.
    return Path(__file__).resolve().parents[3] / "dashboard" / "dist"


def _mount_dashboard_if_production(app: FastAPI) -> None:
    """Mount ``dashboard/dist/`` at ``/`` when AGENTIC_DASHBOARD_MODE=production.

    Must be called AFTER every ``/api/*`` router is registered — the mount is a
    catch-all and any subsequent route registration would be shadowed.
    """
    mode = os.environ.get("AGENTIC_DASHBOARD_MODE", "development").lower()
    if mode != "production":
        return
    dist_path = _resolve_dashboard_dist()
    log = logging.getLogger("uvicorn")
    if not dist_path.is_dir():
        log.warning(
            "dashboard: AGENTIC_DASHBOARD_MODE=production but dist not found at %s — "
            "run `npm run build` in dashboard/ or unset the env var.", dist_path,
        )
        return
    app.mount("/", StaticFiles(directory=str(dist_path), html=True), name="dashboard")
    log.info("dashboard: serving %s at /", dist_path)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agentic OS — routines bridge",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # Loopback only. In development the Vite dev server makes cross-origin
    # requests from :5173 → :8765. In production the dashboard is same-origin
    # (served by this app at :8765) so CORS isn't exercised, but the dev
    # origins stay allowed so a developer can spin up Vite alongside.
    #
    # Starlette middleware runs in REVERSE order of registration (LIFO):
    # the LAST added wraps OUTERMOST. We want CORS outermost (so it can
    # answer OPTIONS preflight + attach CORS headers to every response,
    # including our X-ANTON-Run-Id one). So register RunIdMiddleware
    # FIRST, CORS SECOND. The browser sees a CORS-compliant response
    # whose body was produced inside a run_id-bound context.
    #
    # The X-ANTON-Run-Id response header must be in CORS's
    # ``expose_headers`` so cross-origin (Vite dev) JS can read it via
    # ``response.headers.get('x-anton-run-id')``. Same-origin (production
    # mode) doesn't need this, but symmetric setup keeps the dashboard's
    # ``lib/api.ts`` (#59-harness, deferred) identical across modes.
    app.add_middleware(RunIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept", "X-ANTON-Run-Id"],
        expose_headers=["X-ANTON-Run-Id"],
    )
    # F-1 (browser-origin CSRF) + F-3 (DNS-rebinding) guard. Registered LAST so
    # it wraps OUTERMOST (Starlette middleware is LIFO): it runs BEFORE CORS,
    # RunId, routing, and every handler — a rejected request does zero work and
    # never constructs a Request/touches request.url (BadHost-immune). For
    # state-changing methods it requires a loopback Origin (or, when Origin is
    # absent, a same-site/none Sec-Fetch-Site) AND Content-Type: application/json;
    # for ALL methods it validates the raw Host header against the loopback
    # allowlist. The dashboard (same-origin :8765 in prod, :5173 in dev) always
    # sends application/json via lib/api.ts, so the guard is transparent to it.
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(recall_routes.router,     prefix="/api", tags=["recall"])
    app.include_router(sectornews_routes.router, prefix="/api", tags=["sector-news"])
    app.include_router(promotion_routes.router,  prefix="/api", tags=["memory-promote"])
    app.include_router(projects_routes.router,   prefix="/api", tags=["vault"])
    app.include_router(project_chat_routes.router, prefix="/api", tags=["project-chat"])
    app.include_router(audit_routes.router,      prefix="/api", tags=["audit"])
    app.include_router(pulse_routes.router,      prefix="/api", tags=["vault"])
    app.include_router(markets_routes.router,    prefix="/api", tags=["markets"])
    app.include_router(telemetry_routes.router,  prefix="/api", tags=["telemetry"])
    app.include_router(usage_routes.router,       prefix="/api", tags=["telemetry"])
    app.include_router(drafts_routes.router,     prefix="/api", tags=["vault"])
    app.include_router(daily_routes.router,      prefix="/api", tags=["vault"])
    app.include_router(morning_brief_routes.router, prefix="/api", tags=["morning-brief"])
    app.include_router(daily_digest_routes.router,  prefix="/api", tags=["daily-digest"])
    app.include_router(proposals_routes.router,     prefix="/api", tags=["proposals"])
    app.include_router(dismissals_routes.router,    prefix="/api", tags=["dismissals"])
    app.include_router(intake_routes.router,         prefix="/api", tags=["intake"])
    app.include_router(lbo_routes.router,            prefix="/api", tags=["workflows"])
    app.include_router(lbo_intake_agent_routes.router, prefix="/api", tags=["workflows"])
    app.include_router(bd_decay_routes.router,       prefix="/api", tags=["workflows"])
    app.include_router(comps_routes.router,          prefix="/api", tags=["workflows"])
    app.include_router(recall_query_routes.router,   prefix="/api", tags=["workflows"])
    app.include_router(equity_research_routes.router, prefix="/api", tags=["workflows"])
    app.include_router(actions_decay_routes.router,  prefix="/api", tags=["workflows"])
    app.include_router(lessons_suggest_routes.router, prefix="/api", tags=["workflows"])
    app.include_router(morning_brief_skill_routes.router, prefix="/api", tags=["workflows"])
    app.include_router(deal_tracker_routes.router,   prefix="/api", tags=["workflows"])
    app.include_router(earnings_routes.router,       prefix="/api", tags=["workflows"])
    app.include_router(ticker_multiples_routes.router, prefix="/api", tags=["workflows"])
    app.include_router(vault_health_routes.router,   prefix="/api", tags=["workflows"])
    app.include_router(vault_graph_routes.router,    prefix="/api", tags=["vault"])
    app.include_router(sessions_routes.router,       prefix="/api", tags=["sessions"])
    # #chat-attachments — document upload + local extraction. Carries its own
    # loopback guard (like credentials/intake); registered alongside sessions
    # since its path is /api/sessions/{id}/attachments.
    app.include_router(attachments_routes.router,    prefix="/api", tags=["sessions"])
    app.include_router(scheduler_routes.router,      prefix="/api", tags=["scheduler"])
    app.include_router(workspaces_routes.router,     prefix="/api", tags=["workspaces"])
    app.include_router(credentials_routes.router,    prefix="/api", tags=["credentials"])
    app.include_router(operator_config_routes.router, prefix="/api", tags=["operator-config"])
    app.include_router(budgets_routes.router,        prefix="/api", tags=["budgets"])
    # NOTE: sensitivity_overrides_routes.router already declares prefix='/api/sensitivity'
    # so we DON'T pass prefix='/api' here (would double-prefix to '/api/api/sensitivity').
    app.include_router(sensitivity_overrides_routes.router)
    # #llm-routing-postjune15 P5 — MNPI cloud-attestations. Router declares its
    # own prefix='/api/mnpi' + loopback guard (same pattern as sensitivity_overrides).
    app.include_router(mnpi_attestations_routes.router)
    # Tier 2 per-skill provider matrix + sidecar PATCH (#llm-routing-tier-2).
    # Router carries its own /api/skills prefix + loopback guard.
    app.include_router(skills_providers_routes.router)
    # #63 phase 2b — skill-run suspend/resume control (POST {run_id}/resume +
    # GET suspended). Carries its own /api/skills prefix; paths are disjoint
    # from the provider-matrix routes above.
    app.include_router(skill_runs_routes.router)
    # #llm-routing-postjune15 G4 (Mission B) -- routing lane-matrix readout.
    # Router carries its own /api/routing prefix + loopback guard.
    app.include_router(routing_matrix_routes.router)
    # #llm-routing-postjune15 G1 (Mission B) -- per-lane cloud-dispatch ladder
    # readout. Same /api/routing prefix + loopback guard.
    app.include_router(lane_status_routes.router)
    app.include_router(activity_routes.router,       prefix="/api", tags=["activity"])
    app.include_router(dashboard_routes.router,      prefix="/api", tags=["dashboard"])
    # #31 — crew lane (MetaGPT over subprocess + JSON-on-stdio). The router
    # imports NOTHING from metagpt; the boundary is routines/crew/proxy.py.
    app.include_router(crew_routes.router,           prefix="/api", tags=["crew"])
    # #crew-cloud-promotion — loopback-only crew cloud-promotion routes
    # (gated /api/crew/_llm route-through + the providers matrix/PATCH). Self-
    # prefixes /api/crew + carries its own loopback guard (like skills_providers).
    app.include_router(crew_providers_routes.router)
    # #26b — composite ``_compose`` proxy (TRANSFORM substitute; Synapse
    # calls this as a custom HTTP tool). Routes: GET /api/composite/_compose
    # (discovery) + POST /api/composite/_compose/{key}.
    app.include_router(compose_routes.router,        prefix="/api", tags=["composite"])

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # #67 — chat-friendly 422 formatter. Replaces FastAPI's default nested
    # JSON `{"detail":[{loc,msg,type,...}]}` with an additive shape that
    # also carries a one-line ``human_message`` an operator can read at a
    # glance ("Required: ticker. as_of: invalid date"). The standard
    # ``detail`` field is PRESERVED so existing API consumers (dashboard
    # 422 handlers — #58 InboxTab reject UI, #57 BudgetAckModal) parse
    # unchanged. Feeds #10 skill-input form's inline-error rendering.
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_format_validation_payload(exc),
        )

    # #74.2 readiness routing — a registered skill whose declared ``requires:``
    # preconditions (a vault path / fs root that must exist) aren't met at
    # dispatch raises ``SkillPreconditionsNotMet`` from the central guard. Map
    # it to a 409 with a one-line ``human_message`` the dashboard renders as a
    # "skill not ready" notice — the skill fails FAST instead of crashing
    # mid-run. ``detail`` mirrors the message so plain API consumers read it too.
    @app.exception_handler(SkillPreconditionsNotMet)
    async def skill_preconditions_handler(
        request: Request, exc: SkillPreconditionsNotMet,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "skill_not_ready",
                "human_message": str(exc),
                "detail": str(exc),
            },
        )

    # #24 guardrail retry loop — a skill LLM output that still fails its
    # declared guardrails after the retry budget (min(guardrail_max_retries,
    # sensitivity-tier budget); MNPI=0) raises ``GuardrailRetriesExhausted``
    # from ``llm_with_guardrails``. Map it to a 502 (the model could not
    # produce a compliant output — an upstream-output failure, not a caller
    # error) with an honest payload naming WHICH guardrail failed — never a
    # fabricated pass. The payload is rebuilt from structured fields ONLY
    # (codex SEV-2): verdict messages can embed checker exception text or
    # output-derived content, so they go to the audit rows the loop already
    # writes — correlate via run_id — never to the wire.
    @app.exception_handler(GuardrailRetriesExhausted)
    async def guardrail_retries_handler(
        request: Request, exc: GuardrailRetriesExhausted,
    ) -> JSONResponse:
        failed_names = [v.name for v in exc.failures]
        message = (
            f"skill {exc.skill_name!r} output failed guardrail(s) "
            f"[{', '.join(failed_names) or '(none recorded)'}] after "
            f"{exc.attempts} attempt(s) (retry budget {exc.budget})"
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "guardrail_retries_exhausted",
                "human_message": message,
                "detail": message,
                "skill": exc.skill_name,
                "run_id": exc.run_id,
                "attempts": exc.attempts,
                "retry_budget": exc.budget,
                "failures": [{"name": n} for n in failed_names],
            },
        )

    # MUST stay last — the dist/ mount is a catch-all and would shadow any
    # /api/* route registered after it.
    _mount_dashboard_if_production(app)

    return app


def _format_validation_payload(exc: RequestValidationError) -> dict:
    """Compose the additive 422 body. Public for direct unit-testing."""
    field_errors: list[dict] = []
    for err in exc.errors():
        # err shape: {type, loc, msg, input?, ctx?, url?}. ``loc`` is a
        # tuple like ("body", "ticker") or ("query", "limit"); strip the
        # source segment so the field name reads like "ticker", not
        # "body.ticker".
        loc = err.get("loc") or ()
        parts = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
        field_name = ".".join(parts) if parts else "(root)"
        field_errors.append({
            "field": field_name,
            "message": str(err.get("msg") or ""),
            "type": str(err.get("type") or ""),
        })

    required = [fe["field"] for fe in field_errors if fe["type"] == "missing"]
    other = [
        f"{fe['field']}: {fe['message']}"
        for fe in field_errors if fe["type"] != "missing"
    ]

    parts: list[str] = []
    if required:
        parts.append(f"Required: {', '.join(required)}")
    if other:
        parts.append("; ".join(other))
    human_message = ". ".join(parts) if parts else "Validation failed"

    # F-28 (HR + CX B-08, confirmed×2): Pydantic v2 stuffs the RAW request
    # value into every error's ``input`` (and sometimes ``ctx``) — echoing
    # ``exc.errors()`` verbatim reflected the full request body back in
    # every 422, an info-leak under the rebind/CSRF read threat model.
    # ``detail`` keeps only the structural keys; jsonable_encoder still
    # guards against non-JSON loc parts.
    from fastapi.encoders import jsonable_encoder
    redacted_detail = [
        {k: err.get(k) for k in ("type", "loc", "msg") if k in err}
        for err in exc.errors()
    ]
    return {
        "error": "validation_failed",
        "human_message": human_message,
        "field_errors": field_errors,
        "detail": jsonable_encoder(redacted_detail),
    }


app = create_app()


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _enforce_loopback_bind(host: str) -> None:
    """Refuse a non-loopback bind unless explicitly overridden.

    #ops-bind-guard — ``AGENTIC_API_HOST`` is the single direct "open the
    door" lever: every sensitivity gate downstream assumes the bridge is
    reachable from this machine only. A non-loopback host used to only
    WARN; now it refuses to start unless the operator sets
    ``AGENTIC_ALLOW_PUBLIC_BIND=1``, in which case a LOUD warning is
    logged instead. Hostname comparison is case-insensitive. Public for
    direct unit-testing.

    Raises ``SystemExit`` (exit code 1, message on stderr) on a
    non-loopback host without the override.
    """
    if host.strip().lower() in _LOOPBACK_HOSTS:
        return
    if os.environ.get("AGENTIC_ALLOW_PUBLIC_BIND") != "1":
        raise SystemExit(
            f"Refusing to bind to non-loopback host {host!r} — sensitivity "
            "gating depends on the bridge being loopback only "
            "(127.0.0.1/localhost/::1). Set AGENTIC_ALLOW_PUBLIC_BIND=1 to "
            "override if you know what you're doing."
        )
    logging.warning(
        "AGENTIC_ALLOW_PUBLIC_BIND=1 — bridge bound to non-loopback %s. "
        "Sensitivity gating depends on this being loopback only; every "
        "endpoint is now reachable from the network. Override only if you "
        "know what you're doing.",
        host,
    )


def main() -> None:
    """Entrypoint for `agentic-api` console script."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    host = os.environ.get("AGENTIC_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTIC_API_PORT", "8765"))

    _enforce_loopback_bind(host)

    # proxy_headers=False: the bridge binds loopback and is NEVER behind a reverse
    # proxy, so request.client.host must stay the true TCP socket peer. With
    # uvicorn's default proxy_headers=True (forwarded_allow_ips=127.0.0.1), a
    # loopback client could send X-Forwarded-For to rewrite request.client.host and
    # spoof the _loopback_only guard (#sec-loopback-proxy-headers, Shannon run #2).
    uvicorn.run(
        "routines.api.app:app",
        host=host,
        port=port,
        reload=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
