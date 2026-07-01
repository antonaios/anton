"""Lessons-suggest skill bridge route (#21 — ninth SKILL.md migration).

``POST /api/workflows/lessons-suggest`` — fires a deterministic
frontmatter-driven match of every entry in ``Registers/Lessons.md``
against a target project's industry / sector / subsector tuple, ranks
by specificity (subsector=3, sector=2, industry=1, agnostic=1), and
returns the top-N as a list of Suggestion records PLUS the
paste-ready markdown bullets string. The handler:

  1. Reads the skill registry for governance metadata (sensitivity,
     scope, cost caps) — no inlined constants.
  2. Wraps the in-process call in the real ``tool_call_hooks`` context
     manager so ``enforce_skill_sensitivity`` (#61) fires on the
     ``@before_tool_call`` path. For this skill (``workspace_scope:
     project``, ``sensitivity: internal``) the guard fires on every
     call; for the common case where the caller passes
     ``workspace_type="project"`` it's a NO-OP. The two refuse paths
     are: (a) workspace_type != "project" -> SkillScopeRefused -> 403;
     (b) workspace_sensitivity=MNPI -> the §5.2 cross-skill gate.
  3. Resolves the inputs BEFORE calling the matcher: if ``project`` is
     passed, dispatch to ``suggest_for_project``; otherwise dispatch
     to ``suggest`` with the explicit overrides. Either entry point
     returns ``list[Suggestion]``.
  4. Computes the ``matched_context`` echo (the ``_norm``-applied
     form of each input) at the route layer — Iron Law clause 2 is a
     route-layer responsibility (the routine consumes the normalised
     form internally but doesn't surface it).
  5. Computes ``register_state`` (``exists``, ``entries_parsed``,
     ``entries_with_sector_context``) by re-parsing the register —
     this is cheap (sub-second) and gives the operator a sanity-check
     surface that the routine alone doesn't provide.

The existing CLI (``lessons-learned suggest --project <X>``) +
``routines.lessons.cli`` are UNTOUCHED. This is the on-demand
operator-pulled path with a §14 descriptor + a Cmd-K-reachable route.
The OTHER lessons command (``lessons-learned scan``) is the LLM-
driven proposal flow; out of scope here.
"""

from __future__ import annotations

import logging
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.lessons import suggest as _suggest_mod
from routines.shared import audit
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


OutputFormat = Literal["bullets", "verbose"]


# ── request / response models ───────────────────────────────────────────────


class LessonsSuggestRequest(BaseModel):
    """On-demand lessons-suggest request from the dashboard or Cmd-K.

    Either ``project`` is passed (the matcher reads
    industry/sector/subsector from ``Projects/<project>/00 Brief.md``
    frontmatter), OR the explicit ``industry`` / ``sector`` /
    ``subsector`` overrides are passed. Both paths return the same
    response shape; the route surfaces which path was taken via the
    ``input_echo`` block.

    The ``format`` flag is parity with the CLI's
    ``lessons-learned suggest --format`` flag; the JSON response
    carries the full structured payload regardless. The flag is
    surfaced via ``input_echo.format`` so a downstream consumer can
    inspect it; the bullets string is rendered identically for
    both formats (the verbose variant simply means the operator
    asked for the per-row score + reason to be surfaced alongside
    the bullets, which the structured response already provides
    in ``suggestions[].score`` + ``suggestions[].reason``)."""

    project: Optional[str] = None
    industry: Optional[str] = None
    sector: Optional[str] = None
    subsector: Optional[str] = None
    limit: int = Field(default=10, gt=0)
    format: OutputFormat = "bullets"
    # workspace fields are conventional across all skill routes (#61). For
    # this project-scope, internal skill the central guard fires on every
    # call; the common case (workspace_type="project") is a NO-OP. Default
    # workspace_type to "project" so the dashboard's default request body
    # passes the guard.
    workspace_type: Literal["project", "bd", "general"] = "project"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class SuggestionOut(BaseModel):
    """One ranked Suggestion surfaced in the response. Mirrors the
    routine's :class:`Suggestion` dataclass; every field is required so
    the Iron Law's score>0 + reason-populated clauses cannot be
    silently violated (a row with empty reason would fail pydantic
    validation at construction)."""

    slug: str = Field(..., min_length=1)
    title: str
    score: int = Field(..., gt=0)
    reason: str = Field(..., min_length=1)
    wikilink: str


class InputEcho(BaseModel):
    """Verbatim echo of the request inputs so the operator can confirm
    what the matcher actually consumed."""

    project: Optional[str] = None
    industry: Optional[str] = None
    sector: Optional[str] = None
    subsector: Optional[str] = None
    limit: int
    format: OutputFormat


class MatchedContext(BaseModel):
    """The ``_norm``-applied form of each input — Iron Law clause 2.

    ``[[Sectors/Telecoms]]`` -> ``"telecoms"``; ``None``/empty input
    -> ``""``. The operator sanity-checks this against the input echo
    to debug rank surprises (typos, wikilink-wrapper unwraps, etc.)."""

    industry_norm: str = ""
    sector_norm: str = ""
    subsector_norm: str = ""


class RegisterState(BaseModel):
    """The corpus the matcher saw — operator sanity-check surface."""

    path: str = "Registers/Lessons.md"
    exists: bool
    entries_parsed: int
    entries_with_sector_context: int


class LessonsSuggestCounts(BaseModel):
    """Headline counts — operator sanity-check shape."""

    total_entries: int          # parsed from the register
    scored: int                 # entries with score > 0 BEFORE limit truncation
    returned: int               # AFTER limit truncation


class LessonsSuggestResult(BaseModel):
    """Structured lessons-suggest result. Pure-return data — no file
    write. Cousin shape to bd-decay + actions-decay (deterministic
    sweep + structured response). The bullets string is the deliverable
    the operator pastes into §6 of the brief."""

    status: Literal["ok", "error"]
    run_id: str
    input_echo: InputEcho
    matched_context: MatchedContext
    register_state: RegisterState
    suggestions: list[SuggestionOut] = Field(default_factory=list)
    bullets: str
    counts: LessonsSuggestCounts
    duration_ms: int = 0
    error: Optional[str] = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _wikilink(slug: str) -> str:
    """Return the stable Obsidian wikilink for a lesson slug.

    The wikilink IS the citation — every Suggestion carries this
    reference; the operator clicks through to read the full lesson
    body. The format is verbatim per the routine's
    ``render_brief_bullets`` (line 119 of suggest.py)."""
    return f"[[Registers/Lessons#{slug}]]"


def _suggestion_out(s: _suggest_mod.Suggestion) -> SuggestionOut:
    """Map the routine's ``Suggestion`` dataclass to the response
    shape. The routine's filter at line 100 of suggest.py guarantees
    ``score > 0``; ``reason`` is populated by line 102. Pydantic
    validators on :class:`SuggestionOut` re-assert these as a
    defence-in-depth check (drift in the routine would raise here)."""
    return SuggestionOut(
        slug=s.lesson.slug,
        title=s.lesson.title,
        score=s.score,
        reason=s.reason,
        wikilink=_wikilink(s.lesson.slug),
    )


def _matched_context(
    industry: Optional[str],
    sector: Optional[str],
    subsector: Optional[str],
) -> MatchedContext:
    """Apply ``_norm`` to each input and surface the result.

    Iron Law clause 2 — the response MUST surface the normalised form
    so the operator can debug rank surprises. ``_norm`` lowercases +
    strips wikilink wrappers (``[[Sectors/Telecoms]]`` -> ``telecoms``);
    ``None``/empty input -> ``""``."""
    return MatchedContext(
        industry_norm=_suggest_mod._norm(industry),
        sector_norm=_suggest_mod._norm(sector),
        subsector_norm=_suggest_mod._norm(subsector),
    )


def _compute_register_state(vault_root) -> RegisterState:
    """Re-parse the register at the route layer to surface ``exists``
    + ``entries_parsed`` + ``entries_with_sector_context`` so the
    operator can sanity-check the corpus the matcher saw.

    The routine's ``suggest`` runs the same parse internally but
    doesn't expose these counts; re-parsing here is sub-second and
    keeps ``suggest.py`` untouched. If the register is missing,
    ``_parse_register`` returns ``[]`` and we report ``exists: false``
    + zeros."""
    path = vault_root / _suggest_mod.REGISTER_RELPATH
    if not path.exists():
        return RegisterState(exists=False, entries_parsed=0, entries_with_sector_context=0)
    entries = _suggest_mod._parse_register(vault_root)
    _suggest_mod._annotate_with_sector_context(entries, vault_root)
    with_ctx = sum(
        1 for e in entries
        if e.industries or e.sectors or e.subsectors
    )
    return RegisterState(
        exists=True,
        entries_parsed=len(entries),
        entries_with_sector_context=with_ctx,
    )


def _resolve_suggestions(
    vault_root,
    project: Optional[str],
    industry: Optional[str],
    sector: Optional[str],
    subsector: Optional[str],
    limit: int,
) -> tuple[list[_suggest_mod.Suggestion], Optional[str], Optional[str], Optional[str]]:
    """Dispatch to ``suggest_for_project`` or ``suggest``.

    Returns ``(suggestions, industry_used, sector_used, subsector_used)``
    so the route can re-compute the matched-context echo against the
    INPUTS the matcher actually consumed (when ``project`` is passed,
    these come from the brief frontmatter, not the request body)."""
    if project:
        # Read the brief metadata so the matched-context echo reflects
        # the brief's values (not the request body's, which were None
        # by definition when project was passed).
        brief_meta = _suggest_mod._load_brief_metadata(vault_root, project)
        if brief_meta is None:
            raise FileNotFoundError(
                f"Project brief not found: Projects/{project}/{_suggest_mod.BRIEF_FILENAME}"
            )
        suggestions = _suggest_mod.suggest_for_project(
            vault_root, project, limit=limit,
        )
        return (
            suggestions,
            brief_meta.get("industry"),
            brief_meta.get("sector"),
            brief_meta.get("subsector"),
        )
    suggestions = _suggest_mod.suggest(
        vault_root,
        industry=industry,
        sector=sector,
        subsector=subsector,
        limit=limit,
    )
    return suggestions, industry, sector, subsector


# ── route ───────────────────────────────────────────────────────────────────


@router.post("/lessons-suggest", response_model=LessonsSuggestResult)
@anton_skill("lessons-suggest")
def run_workflow_lessons_suggest(req: LessonsSuggestRequest) -> LessonsSuggestResult:
    """Run a lessons-suggest match on demand. See module docstring.

    #63/#21 — migrated onto ``@anton_skill``: the wrapper owns the governance
    jacket (registry metadata, ``tool_call_hooks``, lifecycle, dedup,
    ``SkillScopeRefused``→403). This body is just the match. Behaviour-identical."""
    # Either project OR at least one of industry/sector/subsector must be
    # passed. The CLI enforces the same precondition (line 202 of
    # lessons/cli.py).
    if not req.project and not (req.industry or req.sector or req.subsector):
        raise HTTPException(
            status_code=422,
            detail=(
                "must pass either 'project' OR at least one of "
                "'industry' / 'sector' / 'subsector'"
            ),
        )

    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()

    # Stage A — corpus visibility (Iron Law sanity-check
    # surface). Compute BEFORE the matcher fires so a missing
    # register or zero-entry register surfaces honestly even
    # when the matcher returns [].
    register_state = _compute_register_state(VAULT)

    # Stage B — fire the matcher. Either entry point returns
    # list[Suggestion]; the route captures the effective
    # (industry, sector, subsector) for the matched_context
    # echo (when project is passed, these come from the brief
    # frontmatter, not the request body).
    try:
        suggestions, ind_used, sec_used, sub_used = _resolve_suggestions(
            VAULT,
            project=req.project,
            industry=req.industry,
            sector=req.sector,
            subsector=req.subsector,
            limit=req.limit,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001 — matcher errors map to 500
        log.error("lessons-suggest matcher failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"lessons-suggest matcher failed: {e}",
        )

    # Stage C — Iron Law clause 1 (defensive). The routine
    # filters score==0 at line 100 of suggest.py; if any
    # zero-score row leaked through, refuse it at the route
    # boundary. Pydantic also enforces ``score > 0`` on
    # SuggestionOut so this is belt-and-braces.
    for s in suggestions:
        if s.score <= 0:
            log.warning(
                "lessons-suggest: zero-score row leaked from routine "
                "(drift): slug=%s reason=%s",
                s.lesson.slug, s.reason,
            )
        if not s.reason:
            log.warning(
                "lessons-suggest: empty-reason row leaked from routine "
                "(drift): slug=%s score=%s",
                s.lesson.slug, s.score,
            )
    kept = [s for s in suggestions if s.score > 0 and s.reason]

    # Stage D — render bullets via the routine's renderer.
    # Iron Law clause 3 — the bullets string is byte-identical
    # to a direct call of render_brief_bullets; no Anton-side
    # reformatting.
    bullets = _suggest_mod.render_brief_bullets(kept)

    # Stage E — matched-context echo (Iron Law clause 2).
    matched_ctx = _matched_context(ind_used, sec_used, sub_used)

    # Stage F — counts. ``total_entries`` is the register
    # parse count; ``scored`` would require re-running the
    # scorer without the limit (cheap but redundant). Surface
    # the kept count instead and note in the schema that this
    # is post-limit. The operator can re-fire with a higher
    # limit if they want the full scored count.
    counts = LessonsSuggestCounts(
        total_entries=register_state.entries_parsed,
        scored=len(kept),
        returned=len(kept),
    )

    return LessonsSuggestResult(
        status="ok",
        run_id=run_id,
        input_echo=InputEcho(
            project=req.project,
            industry=req.industry,
            sector=req.sector,
            subsector=req.subsector,
            limit=req.limit,
            format=req.format,
        ),
        matched_context=matched_ctx,
        register_state=register_state,
        suggestions=[_suggestion_out(s) for s in kept],
        bullets=bullets,
        counts=counts,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
