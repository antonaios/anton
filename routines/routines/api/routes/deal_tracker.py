"""Deal-tracker skill bridge route (#21 — fourth SKILL.md migration).

``POST /api/workflows/deal-tracker`` — extracts one M&A deal record from an
article body (Ollama qwen3:14b, JSON-mode), validates the Iron Law's two
clauses, dedupes against the existing tracker workbook, and appends a row.
Returns a structured :class:`DealTrackerResult`.

The handler:

  1. Reads the skill registry for governance metadata (sensitivity, scope,
     cost caps) — no inlined constants.
  2. Wraps the in-process extract + append in the real ``tool_call_hooks``
     context manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope: any``,
     ``sensitivity: internal``) the guard is a structural NO-OP for the
     common case; the only firing path is the cross-skill MNPI gate.
  3. Calls ``extract.extract_deal`` + ``workbook.append_deal`` directly
     (no subprocess — both are fast in-process operations). The extract
     dominates wall-clock (~5-10s warm Ollama, ~30s cold) but stays well
     under the 60s ceiling. openpyxl append is sub-second.
  4. Enforces Iron Law clause 1 at the route boundary: if
     ``deal.target_company`` is empty after extraction, returns 422 + the
     warning, REFUSING to append. (The CLI warns and proceeds; the route is
     stricter — the on-demand operator-paste path must not pollute the
     tracker with non-deals.) Iron Law clause 2 (no-computed-multiples) is
     enforced by the extractor's SYSTEM_PROMPT; the route surfaces any
     non-null multiple in the response for Anton to verify against the
     source text.

The CLI path (``deal-tracker add``) is untouched and continues to talk
directly to ``extract.extract_deal`` + ``workbook.append_deal``; this route
is the on-demand operator surface (dashboard tile + Cmd-K). The sector-news
pipeline's Stage 3b auto-feed also keeps calling ``workbook.append_deal``
directly — neither the CLI nor the auto-feed flows through this route.
"""

from __future__ import annotations

import logging
import ntpath
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.dealtracker import extract as _extract
from routines.dealtracker import workbook as _workbook
from routines.dealtracker.workbook import CANONICAL_WORKBOOK_PATH
from routines.hooks.central_guards import _path_is_allowed
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# Canonical precedent tracker (post 2026-06-01 retarget). The live file is at
# an absolute path OUTSIDE the vault (the Corporate Finance research drive);
# operator override via the request body's optional ``workbook_path``,
# sandboxed to the allowed write roots (F-2). Tests monkeypatch THIS default
# to ``tmp_path`` (tmp dirs are outside the sandbox by design).
_DEFAULT_WORKBOOK_PATH = CANONICAL_WORKBOOK_PATH

# Reserved DOS device basenames — reserved on Windows even WITH an extension
# and in any directory (``NUL.xlsx`` writes to the null device, ``COM1.xlsx``
# to a serial port). ``Path.resolve()`` does NOT reject these, and they pass
# both the ``.xlsx`` suffix and the allowed-root prefix check, so screen them
# explicitly (codex-5.5 F-2 round 1).
_WIN_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


# ── request / response models ────────────────────────────────────────────────


class DealTrackerRequest(BaseModel):
    """On-demand deal-tracker request from the dashboard or Cmd-K.

    Operator pastes the article body inline (``text``) along with the source
    ``url`` (provenance only — the routine does NOT fetch). Empty ``text`` is
    a 422 (provenance required). ``workbook_path`` is an optional override
    for testing / a non-default tracker location."""

    url: str = ""
    text: str
    # Optional override for a non-default tracker location. SANDBOXED: the
    # route refuses any path outside the central allowed write roots (F-2 —
    # a verbatim caller path is an arbitrary-file-write primitive).
    workbook_path: Optional[str] = None
    dry_run: bool = False
    # workspace fields conventional across all skill routes (#61). For this
    # any-scope, internal skill they pass through the central guard without
    # effect (except for MNPI inputs, which the guard refuses).
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class DealPreview(BaseModel):
    """Compact preview of the extracted record for the response. Mirrors the
    fields Anton's chat bubble surfaces — full 18-column row lives on the
    Excel sheet (post 2026-06-01 lean schema retarget; see
    ``routines.dealtracker.schema.COLUMNS``). ``None`` values mean "source
    did not state" (Iron Law clause 2 — never inferred)."""

    target_company: str
    bidder_company: str
    seller_company: str
    announced_date: Optional[str] = None  # ISO
    enterprise_value_m: Optional[float] = None
    currency: str = ""
    reported_revenue_multiple_y1: Optional[float] = None
    reported_ebit_multiple_y1: Optional[float] = None
    reported_ebitda_multiple_y1: Optional[float] = None
    target_sector: str = ""
    deal_description: str = ""
    source_url: str = ""


class DealTrackerResult(BaseModel):
    """Structured deal-tracker result. status ∈ {appended, skipped_duplicate,
    dry_run}; ``row`` populated on ``appended``; ``existing_row`` on
    ``skipped_duplicate``. ``warnings`` surfaces operator-visible gaps (EV
    not stated, no source URL, unverified multiples)."""

    status: Literal["appended", "skipped_duplicate", "dry_run"]
    run_id: str
    deal: DealPreview
    workbook_path: str
    row: Optional[int] = None
    existing_row: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int = 0


# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_workbook_path(req: DealTrackerRequest) -> Path:
    """Pick the workbook path. Override → sandbox it; otherwise the canonical
    precedent tracker (absolute path on the Corporate Finance research drive,
    NOT inside the vault).

    F-2 (SEV-1): a verbatim caller-supplied path turned this route into an
    arbitrary-file-write primitive (``append_deal`` mkdirs + writes wherever
    the path points, UNC included). The override survives — the operator may
    legitimately keep a side tracker under the research drive — but it must
    resolve under a central allowed write root (``_path_is_allowed``: rejects
    UNC/device namespaces outright, collapses ``..`` before the prefix check)
    and carry the ``.xlsx`` suffix. ``Path.resolve()`` runs FIRST so the check
    sees the real target (relative / drive-relative forms resolve against the
    bridge cwd → refused; symlinked escapes resolve to their target)."""
    if not req.workbook_path:
        return _DEFAULT_WORKBOOK_PATH
    # Reject UNC / device / extended-length namespaces on the RAW string,
    # before ``resolve()`` — resolving ``\\server\share\...`` can stall on a
    # network lookup for a host the attacker chose.
    if req.workbook_path.replace("\\", "/").startswith("//"):
        raise HTTPException(
            status_code=422,
            detail="workbook_path must be a plain local path (no UNC/device namespaces)",
        )
    # Reject NTFS alternate-data-stream syntax: a ``:`` in any component
    # BEYOND the drive letter. ``…\memo.docx:tracker.xlsx`` has a lexical
    # ``.xlsx`` suffix and resolves under an allowed root, but Windows writes
    # it as an ADS hanging off ``memo.docx`` — a hidden write primitive against
    # an arbitrary existing file inside the sandbox. Checked on the RAW string
    # (resolve()'s handling of a stream colon varies by Windows version)
    # (codex-5.5 F-2 round 1).
    if ":" in ntpath.splitdrive(req.workbook_path)[1]:
        raise HTTPException(
            status_code=422,
            detail="workbook_path must not contain ':' (NTFS stream syntax)",
        )
    try:
        resolved = Path(req.workbook_path).resolve()
    except (OSError, ValueError):
        # Unparseable / reserved-device forms — refuse, never fall through
        # to the canonical file (the caller asked for somewhere specific).
        raise HTTPException(
            status_code=422,
            detail="workbook_path is not a resolvable local path",
        )
    # Reject reserved DOS device names (NUL.xlsx / COM1.xlsx / …) in ANY
    # component — they pass resolve()/suffix/allowlist but the write goes to
    # the device, not a file.
    for part in resolved.parts:
        stem = part.rstrip(" .").split(".", 1)[0].strip().lower()
        if stem in _WIN_RESERVED_DEVICE_NAMES:
            raise HTTPException(
                status_code=422,
                detail="workbook_path uses a reserved Windows device name",
            )
    if resolved.suffix.lower() != ".xlsx":
        raise HTTPException(
            status_code=422,
            detail="workbook_path must point at a .xlsx workbook",
        )
    if not _path_is_allowed(str(resolved)):
        raise HTTPException(
            status_code=422,
            detail=(
                "workbook_path is outside the allowed write roots — "
                "the tracker override must stay under the research-drive / "
                "vault write sandbox"
            ),
        )
    return resolved


def _deal_preview(deal: Any) -> DealPreview:
    """Build the response preview from a routine ``DealRecord``. Date fields
    serialise to ISO strings (or ``None``)."""
    return DealPreview(
        target_company=deal.target_company,
        bidder_company=deal.bidder_company,
        seller_company=deal.seller_company,
        announced_date=deal.announced_date.isoformat() if deal.announced_date else None,
        enterprise_value_m=deal.enterprise_value_m,
        currency=deal.currency,
        reported_revenue_multiple_y1=deal.reported_revenue_multiple_y1,
        reported_ebit_multiple_y1=deal.reported_ebit_multiple_y1,
        reported_ebitda_multiple_y1=deal.reported_ebitda_multiple_y1,
        target_sector=deal.target_sector,
        deal_description=deal.deal_description,
        source_url=deal.source_url,
    )


def _collect_warnings(deal: Any) -> list[str]:
    """Anton-visible warnings — gaps the operator should know about. NOT
    refusal triggers (those raise); just surface chips."""
    out: list[str] = []
    if not deal.source_url:
        out.append("no source URL captured — provenance incomplete")
    if deal.enterprise_value_m is None:
        out.append("EV not stated in source")
    if deal.announced_date is None:
        out.append("announced_date not stated in source — dedupe skipped")
    return out


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/deal-tracker", response_model=DealTrackerResult)
@anton_skill("deal-tracker")
def run_workflow_deal_tracker(req: DealTrackerRequest) -> DealTrackerResult:
    """Extract one M&A deal record and append it to the tracker workbook.

    See module docstring for the gate / error contract. The two Iron Law
    clauses are enforced at the route boundary (clause 1: target_company
    non-empty → 422 if not; clause 2: no inferred multiples → enforced by
    the extractor's SYSTEM_PROMPT, surfaced in the response for Anton).

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks``, lifecycle, dedup,
    ``SkillScopeRefused``→403). This body is just the extract + append.
    Behaviour-identical; ``run_id`` now reuses the request-boundary id (#59).
    The custom ``emit_deal_capture`` (#43) stays IN-BODY — deal-tracker does
    NOT declare ``captures_to_vault``, so the wrapper never #76-captures."""
    if not req.text or not req.text.strip():
        raise HTTPException(
            status_code=422,
            detail="text is required — paste the article body (the routine does not fetch URLs)",
        )

    workbook_path = _resolve_workbook_path(req)
    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()

    # Stage 1 — extract (Ollama qwen3:14b JSON-mode). The CLIENT is
    # instantiated here rather than module-level so tests can patch
    # ``extract.extract_deal`` directly without faking a client.
    client = OllamaClient()
    try:
        deal = _extract.extract_deal(
            text=req.text, source_url=req.url, client=client,
        )
    except OllamaError as e:
        # Surface verbatim — drift between models would silently
        # change extraction shape, so the route does NOT retry on a
        # different model. Operator action: ensure Ollama running.
        raise HTTPException(status_code=502, detail=f"extraction failed: {e}")
    except ValueError as e:
        # Empty text (shouldn't reach here — guarded above — but the
        # extractor double-checks).
        raise HTTPException(status_code=422, detail=str(e))

    deal.extracted_by_run_id = run_id

    # Stage 2 — validate (Iron Law clause 1). Route is stricter than
    # the CLI: refuse the append on empty target. The CLI warns and
    # proceeds because the operator may have a reason at the shell;
    # the on-demand operator-paste path must not pollute the tracker
    # with non-deals.
    if not deal.target_company:
        raise HTTPException(
            status_code=422,
            detail=(
                "no target_company extracted — likely not an M&A "
                "announcement. Re-paste a cleaner article excerpt "
                "or confirm via the CLI if this is intentional."
            ),
        )

    warnings = _collect_warnings(deal)

    # Dry-run: skip the workbook write but still surface the preview.
    if req.dry_run:
        return DealTrackerResult(
            status="dry_run",
            run_id=run_id,
            deal=_deal_preview(deal),
            workbook_path=str(workbook_path),
            warnings=warnings,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

    # Stages 3-4 — dedupe + append (openpyxl, sub-second).
    try:
        append_result = _workbook.append_deal(workbook_path, deal)
    except PermissionError as e:
        # Workbook locked (operator has Excel open on it).
        raise HTTPException(
            status_code=409,
            detail=f"workbook locked: {e} — close Excel and retry",
        )
    except Exception as e:  # noqa: BLE001 — append errors map to 500
        log.error("deal-tracker append failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"workbook append failed: {e}")

    status = append_result.get("status")
    if status == "appended":
        result = DealTrackerResult(
            status="appended",
            run_id=run_id,
            deal=_deal_preview(deal),
            workbook_path=str(workbook_path),
            row=int(append_result["row"]),
            warnings=warnings,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        # #43 — best-effort vault enrichment for the dashboard /deal
        # quick-action: the THIRD non-comps ingestion path (alongside
        # the CLI `add` and the sector-news auto-feed). Every non-comps
        # ingestion path enriches; comps owns its own capture and never
        # routes here. A miss logs; it never fails the operator's append.
        try:
            from routines.dealtracker.capture import emit_deal_capture

            emit_deal_capture(
                deal, vault_root=VAULT, run_id=run_id, workbook_path=workbook_path,
            )
        except Exception as e:  # noqa: BLE001 — capture is best-effort
            log.warning("deal-tracker route: deal-capture emit failed: %s", e)
    elif status == "skipped_duplicate":
        result = DealTrackerResult(
            status="skipped_duplicate",
            run_id=run_id,
            deal=_deal_preview(deal),
            workbook_path=str(workbook_path),
            existing_row=int(append_result["existing_row"]),
            warnings=warnings,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    else:  # pragma: no cover — append_deal only returns the two above
        raise HTTPException(
            status_code=500,
            detail=f"unexpected append status: {status!r}",
        )

    return result
