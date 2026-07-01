"""Morning-brief skill bridge route (#21 — tenth SKILL.md migration).

``POST /api/workflows/morning-brief`` — fires the on-demand
regeneration of today's morning brief (operator-initiated mid-day
after closing a deal or adding actions). The 06:30 daily cron stays
canonical and fires the routine directly via the CLI; this route is
the operator-pulled path with SKILL.md governance.

The route orchestrates the existing pipeline directly (no rewrite of
the routine logic — `pull.gather_context`, `synthesise.classify_actions`,
`synthesise.anton_suggests`, `writer.write_brief` are imported as-is):

  1. Reads the skill registry for governance metadata (sensitivity,
     scope, cost caps) — no inlined constants.
  2. Wraps the in-process call in the real ``tool_call_hooks`` context
     manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope:
     any``, ``sensitivity: internal``) the only non-NO-OP refusal path
     is ``workspace_sensitivity: MNPI`` (the §5.2 cross-skill gate).
  3. Loads the operator's profile to derive `active_sectors` (the
     newsletter filter) + the `profile_context` string the LLM #2
     prompt consumes. Silent degradation on profile-parse failure
     (mirrors the CLI's behaviour at lines 64-74 of cli.py).
  4. Health-checks Ollama BEFORE firing the pipeline. On
     ``OllamaError`` at health, sets ``ollama_state="unreachable"``
     and routes both Stage 2 and Stage 3 through the deterministic
     fallback path (`_fallback_classify` + `_fallback_suggest`) — the
     brief is still written but with the truth-surface on the
     response so the operator knows.
  5. Writes the brief atomically to
     ``Routines/morning-briefs/<date>.md`` (overwrite on same-date
     refire — deliberate; the brief IS today's snapshot).
  6. Verifies the Iron Law clause 1 round-trip mechanically: parses
     the written file's frontmatter back, deserialises the ``data:``
     key, and asserts equality with the in-memory brief. Surfaces
     ``frontmatter_data_complete: bool`` on the response.

The existing READ endpoint (``GET /api/morning-brief/today`` at
``routines/api/routes/morning_brief.py``) is UNTOUCHED — that path
serves the stored brief to the dashboard's MorningBriefPanel. This
route is the WRITE path (regeneration); the read path consumes what
this route produces. The 06:30 cron config (in
``routines/scheduler/jobs.py``) is UNTOUCHED.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

import frontmatter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from routines.api.deps import VAULT
from routines.morning_brief.pull import gather_context
from routines.morning_brief.schema import BriefRow, MorningBrief
from routines.morning_brief.synthesise import (
    anton_suggests as _anton_suggests,
    classify_actions as _classify_actions,
    _fallback_classify,
    _fallback_suggest,
)
from routines.morning_brief.writer import brief_path, write_brief
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.profile import load as load_profile
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


OllamaState = Literal["ok", "unreachable", "fallback"]


# ── request / response models ───────────────────────────────────────────────


class MorningBriefRequest(BaseModel):
    """On-demand morning-brief regeneration request from the dashboard
    or Cmd-K.

    All fields are optional — the defaults match the CLI
    (``morning-brief generate``). The operator's typical Cmd-K fire is
    ``POST /api/workflows/morning-brief`` with an empty body, which
    regenerates today's brief with `days_lookback=7` and
    `model=qwen3:14b`.
    """

    date: Optional[str] = Field(
        default=None,
        description="ISO date for the brief (default: today UTC).",
    )
    days_lookback: int = Field(
        default=7, gt=0,
        description="Days back to scan for actions.",
    )
    model: str = Field(
        default="qwen3:14b",
        description="Ollama model for classify_actions + anton_suggests.",
    )
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Ollama base URL (loopback http(s) only; defaults to localhost:11434).",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, return the brief payload without writing the file. "
            "`note_path` will be null on the response."
        ),
    )
    # workspace fields are conventional across all skill routes (#61). For
    # this any-scope, internal skill the central guard fires on every
    # call; the only non-NO-OP refusal path is workspace_sensitivity=MNPI
    # (the §5.2 cross-skill gate). Default to "general" / "default" /
    # "internal" so the dashboard's default request body passes the guard.
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"

    @field_validator("ollama_url")
    @classmethod
    def _ollama_url_loopback_only(cls, v: str) -> str:
        # F-15 (HR S-7): ``ollama_url`` is caller-controllable and was handed
        # verbatim to ``OllamaClient(base_url=...)`` → SSRF: an attacker could
        # point the bridge at any host (probe internal services / exfil the
        # vault-derived prompt to an external collector). The bridge ALWAYS
        # talks to a LOCAL Ollama, so constrain the field to an http(s) URL
        # whose host is loopback, with no userinfo / no embedded credentials.
        raw = (v or "").strip()
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https"):
            raise ValueError("ollama_url must be an http(s) URL")
        # ``@`` in the authority = userinfo (user:pass@host) — reject; it can
        # smuggle a different effective host past a naive host check.
        if "@" in parts.netloc:
            raise ValueError("ollama_url must not contain userinfo credentials")
        host = (parts.hostname or "").lower()
        if not _is_loopback_host(host):
            raise ValueError(
                "ollama_url host must be loopback (127.0.0.1 / ::1 / localhost) "
                "— the bridge only talks to a local Ollama"
            )
        return raw


def _is_loopback_host(host: str) -> bool:
    """True iff ``host`` is a loopback address or the ``localhost`` name.

    ``localhost`` is allowed (the route's documented default) alongside the
    explicit loopback IPs; any IP literal is validated via ``ipaddress`` so a
    non-loopback address (incl. IPv6, or a decimal-encoded IPv4 like
    ``2130706433``) cannot slip through a string compare."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class InputEcho(BaseModel):
    """Verbatim echo of the request inputs (after defaulting) so the
    operator can confirm what the routine actually consumed."""

    date: str                       # ISO date
    days_lookback: int
    model: str
    dry_run: bool


class ContextCounts(BaseModel):
    """Headline counts from `gather_context` — operator sanity-check
    surface."""

    actions_gathered: int           # raw candidates from _gather_actions
    actions_classified: int         # rows surviving classify_actions (LLM #1 OR fallback)
    sector_news: int                # rows from _gather_sector_news


class MorningBriefResult(BaseModel):
    """Structured morning-brief result. Cousin shape to sector-news +
    equity-research (LLM + writer + structured response). The deliverable
    is the WRITTEN ARTEFACT at `Routines/morning-briefs/<date>.md`; the
    response surfaces the in-memory brief + the written path + the
    Iron Law verification surfaces.
    """

    status: Literal["ok", "error"]
    run_id: str
    ollama_state: OllamaState
    input_echo: InputEcho
    context_counts: ContextCounts
    brief: MorningBrief
    note_path: Optional[str] = None         # vault-relative; null if dry_run=True
    frontmatter_data_complete: Optional[bool] = None  # null if dry_run=True
    duration_ms: int = 0
    error: Optional[str] = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _resolve_date(date_str: Optional[str]) -> date_cls:
    """Parse the request's date string OR default to today UTC."""
    if not date_str:
        return datetime.now(timezone.utc).date()
    try:
        return date_cls.fromisoformat(date_str)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail=f"Bad date: {e}",
        ) from e


def _load_profile_context(vault_root: Path) -> tuple[list[str], str]:
    """Read the operator's profile to derive `active_sectors` + the
    `profile_context` string for LLM #2.

    Silent degradation: if the profile is missing or unparseable, the
    routine still runs with empty `active_sectors` (no newsletter
    filter) + an empty `profile_context` string. Mirrors the CLI's
    behaviour at lines 64-74 of cli.py."""
    try:
        profile = load_profile(vault_root)
        active_sectors = profile.active_sectors or []
        profile_context = (
            f"Operator: {profile.operator or 'unknown'}. "
            f"Active sectors: {', '.join(active_sectors) or 'none'}."
        )
        return active_sectors, profile_context
    except Exception as e:  # noqa: BLE001
        log.warning("morning-brief: profile load failed: %s", e)
        return [], ""


def _check_ollama(client: OllamaClient) -> OllamaState:
    """Health-check Ollama. Returns `"ok"` if reachable,
    `"unreachable"` if not.

    The route uses this BEFORE firing the pipeline so the response's
    `ollama_state` is honest. If `"unreachable"`, the route routes
    both Stage 2 and Stage 3 through the deterministic fallback path
    (`_fallback_classify` + `_fallback_suggest`)."""
    try:
        client.health()
        return "ok"
    except OllamaError as e:
        log.warning("morning-brief: Ollama unreachable: %s", e)
        return "unreachable"


def _verify_frontmatter_data_roundtrip(
    note_path: Path, in_memory_brief: MorningBrief,
) -> bool:
    """Iron Law clause 1 — parse the written file's frontmatter back,
    deserialise the `data:` key into a `MorningBrief`, and assert
    equality with the in-memory brief.

    Returns True iff the round-trip is byte-faithful. False if the
    file is missing, the frontmatter is malformed, the `data:` key
    is absent, OR the deserialised brief diverges from the in-memory
    one. Logs a warning on failure so the operator can debug."""
    if not note_path.exists():
        log.warning(
            "morning-brief: frontmatter round-trip — file missing: %s",
            note_path,
        )
        return False
    try:
        post = frontmatter.loads(note_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning(
            "morning-brief: frontmatter round-trip — parse failed: %s", e,
        )
        return False

    raw_data = post.metadata.get("data")
    if not raw_data:
        log.warning("morning-brief: frontmatter round-trip — `data:` key missing")
        return False
    try:
        payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except json.JSONDecodeError as e:
        log.warning(
            "morning-brief: frontmatter round-trip — JSON parse failed: %s", e,
        )
        return False
    if not isinstance(payload, dict):
        log.warning(
            "morning-brief: frontmatter round-trip — payload not a dict: %s",
            type(payload).__name__,
        )
        return False

    # Re-construct a MorningBrief from the deserialised payload and
    # compare. Pydantic model equality is structural.
    try:
        deserialised = MorningBrief(
            date=str(payload.get("date", "")),
            source=str(payload.get("source", "")),
            needsYou=[
                BriefRow(**r) for r in payload.get("needsYou", [])
                if isinstance(r, dict)
            ],
            sectorThisWeek=[
                BriefRow(**r) for r in payload.get("sectorThisWeek", [])
                if isinstance(r, dict)
            ],
            antonSuggests=str(payload.get("antonSuggests", "")),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "morning-brief: frontmatter round-trip — construct failed: %s", e,
        )
        return False

    if deserialised != in_memory_brief:
        log.warning(
            "morning-brief: frontmatter round-trip — diverged "
            "(in-memory != deserialised)",
        )
        return False
    return True


def _run_pipeline(
    vault_root: Path, the_date: date_cls, days_lookback: int,
    model: str, ollama_url: str,
) -> tuple[MorningBrief, ContextCounts, OllamaState]:
    """Orchestrate gather -> classify -> suggest -> assemble.

    Returns the assembled in-memory `MorningBrief` + the context
    counts + the `ollama_state` (`"ok"`, `"unreachable"`, or
    `"fallback"`).

    Lane state semantics:
      * `"ok"`: Ollama health check passed AND both LLM calls
        succeeded.
      * `"unreachable"`: Ollama health check failed; both stages
        routed through the deterministic fallback path.
      * `"fallback"`: Ollama health check passed but at least one
        LLM call raised `OllamaError` mid-flight (transport hiccup,
        timeout, malformed response); the corresponding stage fell
        back to the deterministic path.

    The synthesis stage 3 is SHORT-CIRCUITED to the deterministic
    fallback when BOTH `needs_you` AND `sector_news` from
    `gather_context` are empty — Iron Law clause 3 (no LLM
    fabrication of filler).
    """
    # Stage 1 — Gather context (vault-only, no LLM, no network)
    active_sectors, profile_context = _load_profile_context(vault_root)
    ctx = gather_context(
        vault_root,
        today=the_date,
        days_lookback=days_lookback,
        active_sectors=active_sectors,
    )
    actions_gathered = len(ctx.needs_you)
    sector_news_count = len(ctx.sector_news)

    # Health-check Ollama
    client = OllamaClient(base_url=ollama_url)
    health_state = _check_ollama(client)

    # Stage 2 — Classify actions (LLM #1 OR fallback)
    if health_state == "unreachable":
        needs_you = _fallback_classify(ctx.needs_you, today=the_date)
        ollama_state: OllamaState = "unreachable"
    else:
        try:
            needs_you = _classify_actions(
                ctx.needs_you, today=the_date, client=client, model=model,
            )
            ollama_state = "ok"
        except OllamaError as e:
            log.warning("morning-brief: classify_actions raised: %s", e)
            needs_you = _fallback_classify(ctx.needs_you, today=the_date)
            ollama_state = "fallback"

    # Stage 3 — Compose suggestion (LLM #2 OR fallback)
    # Short-circuit to fallback when both inputs empty (Iron Law clause 3).
    if not needs_you and not ctx.sector_news:
        suggests = _fallback_suggest(needs_you, ctx.sector_news)
    elif ollama_state == "unreachable":
        suggests = _fallback_suggest(needs_you, ctx.sector_news)
    else:
        try:
            suggests = _anton_suggests(
                needs_you, ctx.sector_news,
                profile_context=profile_context,
                client=client, model=model,
            )
        except OllamaError as e:
            log.warning("morning-brief: anton_suggests raised: %s", e)
            suggests = _fallback_suggest(needs_you, ctx.sector_news)
            # If LLM #1 succeeded but LLM #2 fell back, the lane state
            # is "fallback" (a partial degradation).
            if ollama_state == "ok":
                ollama_state = "fallback"

    # Stage 4 — Assemble (writer call is handled by the route after we return)
    full_date = the_date.strftime("%a · %d %b %Y · UTC")
    brief = MorningBrief(
        date=full_date,
        source=f"Generated · Local Ollama {model}",
        needsYou=needs_you,
        sectorThisWeek=ctx.sector_news,
        antonSuggests=suggests,
    )
    counts = ContextCounts(
        actions_gathered=actions_gathered,
        actions_classified=len(needs_you),
        sector_news=sector_news_count,
    )
    return brief, counts, ollama_state


# ── route ───────────────────────────────────────────────────────────────────


@router.post("/morning-brief", response_model=MorningBriefResult)
@anton_skill("morning-brief", capture=False)
def run_workflow_morning_brief(req: MorningBriefRequest) -> MorningBriefResult:
    """Run an on-demand morning-brief regeneration. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks`` sensitivity/MNPI gate → 403,
    lifecycle, dedup, ctx.result). ``capture=False``: morning-brief DELIBERATELY
    does not #76-capture — the SKILL itself writes the daily brief artefact and
    the SKILL.md omits ``captures_to_vault``. The wrapper would not capture
    anyway (captures_to_vault is None); the flag is defense-in-depth.

    The local-LLM pipeline (gather → classify → suggest → assemble, with the
    deterministic fallback when Ollama is unreachable) is UNCHANGED —
    behaviour-identical, NOT moved to the L3 ``llm()`` helper. ``run_id`` now
    reuses the request-boundary id (#59); ``duration_ms`` is unchanged. The 500
    pipeline / 500 write contracts pass through as inner body HTTPExceptions, and
    the ``_resolve_date`` 422 stays a body-helper refusal.

    PRECEDENCE (operator-accepted 2026-06-08, governance-first): the wrapper runs
    the sensitivity/MNPI gate before this body, so a request that is BOTH a bad
    date AND MNPI now returns 403 instead of 422. Single-fault contracts
    (bad-date → 422, MNPI → 403) are unchanged."""
    the_date = _resolve_date(req.date)
    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()

    # Stage A — Run the pipeline (gather + classify + suggest + assemble). The
    # writer call is deferred to Stage B so dry_run can skip it cleanly.
    try:
        brief, counts, ollama_state = _run_pipeline(
            VAULT,
            the_date=the_date,
            days_lookback=req.days_lookback,
            model=req.model,
            ollama_url=req.ollama_url,
        )
    except Exception as e:  # noqa: BLE001 — pipeline errors map to 500
        log.error(
            "morning-brief: pipeline failed: %s", e, exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"morning-brief pipeline failed: {e}",
        )

    # Stage B — Write artefact (skipped if dry_run=True).
    note_path_str: Optional[str] = None
    frontmatter_data_complete: Optional[bool] = None
    if not req.dry_run:
        try:
            written = write_brief(VAULT, brief, the_date)
        except Exception as e:  # noqa: BLE001
            log.error(
                "morning-brief: write_brief failed: %s", e, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"morning-brief write failed: {e}",
            )
        # Vault-relative path for the dashboard's "open the brief" chip
        try:
            note_path_str = str(written.relative_to(VAULT).as_posix())
        except ValueError:
            note_path_str = str(written)
        # Iron Law clause 1 — byte-faithful frontmatter round-trip
        frontmatter_data_complete = _verify_frontmatter_data_roundtrip(
            written, brief,
        )

    return MorningBriefResult(
        status="ok",
        run_id=run_id,
        ollama_state=ollama_state,
        input_echo=InputEcho(
            date=the_date.isoformat(),
            days_lookback=req.days_lookback,
            model=req.model,
            dry_run=req.dry_run,
        ),
        context_counts=counts,
        brief=brief,
        note_path=note_path_str,
        frontmatter_data_complete=frontmatter_data_complete,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# Convenience export for tests that need to monkey-patch the brief_path
# helper without importing the writer module directly.
_brief_path = brief_path
