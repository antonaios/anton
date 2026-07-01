"""Gather context for one chat turn.

Three jobs, all pure (no LLM here):

  1. **History** — load the deal's ``_chat.md`` and truncate to the last N
     turns for the LLM window (``load_history``).
  2. **Sensitivity gating** — read the deal's ``00 Brief.md`` frontmatter
     ``sensitivity:`` and decide the lane: ``confidential`` / ``MNPI`` force
     the LOCAL Ollama lane (CLAUDE.md §4); public/internal MAY use cloud in
     the enterprise tier. Exposed as ``resolve_sensitivity`` +
     ``gate_lane`` (the latter returns ``"local"`` / ``"cloud"``).
  3. **Recall** — run project-filtered recall over the deal's vault folder
     and shape the hits into ``ChatSource`` records (``fetch_sources``).

The synthesise step takes history + sources + the user message and calls the
local LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import frontmatter

from routines.project_chat.reader import load_history as _load_full_history
from routines.project_chat.schema import ChatSource, ChatTurn
from routines.shared.routing import pick_lane

log = logging.getLogger(__name__)


# Recall hits over ~300 chars get clipped for the citation excerpt — long
# enough to be useful, short enough not to bloat the LLM context window.
_EXCERPT_MAX = 300

# Cross-project relaxed-scope sensitivity ceiling (operator decision 2026-06-04;
# OUTSTANDING #42 v2). When the chat ``cross_projects`` toggle is ON, recall
# widens beyond the current deal to the WHOLE vault — but everything OUTSIDE the
# current deal's folder is admitted ONLY if its sensitivity is ``≤ internal``
# (public / internal). Confidential + MNPI from OTHER deals or the general vault
# are EXCLUDED — siloed to their own deal's chat. The HARD FLOOR (never surface
# another deal's MNPI) is subsumed by this cap. A note with no ``sensitivity:``
# frontmatter is treated by recall as ``internal`` (``retrieve._passes_filter``)
# → admissible, matching the operator's intent. The current deal's own folder is
# UNAFFECTED — it always retrieves at full tier (a separate, unfiltered bucket).
_CROSS_PROJECT_CEILING = "internal"

# Valid sensitivity tiers (lowercase canonical). Unknown / missing → the
# safe default below.
_SENSITIVITY_TIERS = ("public", "internal", "confidential", "mnpi")
# Default when the brief is absent / has no sensitivity field. Conservative:
# treat an unlabelled deal as confidential so chat stays LOCAL by default
# (CLAUDE.md §4 — "when unsure of routing: default to a more restrictive lane").
_DEFAULT_SENSITIVITY = "confidential"

Lane = Literal["local", "cloud"]


@dataclass
class ChatContext:
    """Everything synthesise needs for one turn."""

    project: str
    message: str
    history: list[ChatTurn] = field(default_factory=list)
    sources: list[ChatSource] = field(default_factory=list)
    sensitivity: str = _DEFAULT_SENSITIVITY
    lane: Lane = "local"


# ── History ───────────────────────────────────────────────────────────────


def load_history(
    vault_root: Path, project: str, *, history_turns: int = 6,
) -> list[ChatTurn]:
    """Load the deal's chat history, truncated to the last ``history_turns``.

    ``history_turns <= 0`` returns an empty list (no prior context). The full
    log is read first (so the on-disk file is the single source of truth) then
    the tail is sliced for the LLM window.
    """
    full = _load_full_history(vault_root, project)
    if history_turns <= 0:
        return []
    return full[-history_turns:]


# ── Sensitivity gating ─────────────────────────────────────────────────────


def _brief_path(vault_root: Path, project: str) -> Path:
    return vault_root / "Projects" / project / "00 Brief.md"


def resolve_sensitivity(vault_root: Path, project: str) -> str:
    """Read ``Projects/<project>/00 Brief.md`` frontmatter ``sensitivity:``.

    Returns one of ``public`` / ``internal`` / ``confidential`` / ``MNPI``.
    A missing brief, missing field, or unrecognised value resolves to the
    conservative default (``confidential``) so an unlabelled deal stays local.
    ``MNPI`` is returned upper-cased to match the routing enum.
    """
    brief = _brief_path(vault_root, project)
    if not brief.is_file():
        return _DEFAULT_SENSITIVITY
    try:
        post = frontmatter.load(str(brief))
        raw = post.metadata.get("sensitivity")
    except Exception as e:  # noqa: BLE001 — never crash on a YAML wart
        log.warning("project-chat: brief frontmatter parse failed for %s: %s", brief, e)
        return _DEFAULT_SENSITIVITY
    if raw is None:
        return _DEFAULT_SENSITIVITY
    val = str(raw).strip().lower()
    if val not in _SENSITIVITY_TIERS:
        return _DEFAULT_SENSITIVITY
    return "MNPI" if val == "mnpi" else val


def gate_lane(sensitivity: str) -> Lane:
    """Decide the chat lane for a sensitivity tier.

    Delegates to the shared sensitivity router (``routing.pick_lane`` with the
    ``synthesis`` task type — chat is Opus-grade synthesis). Any ``ollama*``
    lane collapses to ``"local"``; anything else is ``"cloud"``. Per CLAUDE.md
    §4 this returns ``"local"`` for ``confidential`` / ``MNPI`` in the bridge
    tier (and MNPI in every tier).
    """
    sens = sensitivity if sensitivity in ("public", "internal", "confidential", "MNPI") \
        else _DEFAULT_SENSITIVITY
    lane = pick_lane("synthesis", sens)  # type: ignore[arg-type]
    return "local" if lane.startswith("ollama") else "cloud"


# ── Recall ─────────────────────────────────────────────────────────────────


def _recall_filters(rtv, project: str, *, cross_projects: bool) -> list:
    """Build the recall ``Filter`` bucket(s) for one chat turn.

    STRICT (``cross_projects`` False — v1 default): a SINGLE bucket scoped to
    ``Projects/<project>/`` (project frontmatter match + path prefix). Byte-
    identical to the original v1 filter — no cross-project leak.

    RELAXED (``cross_projects`` True — operator decision 2026-06-04): TWO
    disjoint buckets, both queried and merged by score in ``fetch_sources``:

      * **bucket A** — the current deal's own folder, at FULL tier (the exact
        strict filter above, unchanged). The current deal is never restricted.
      * **bucket B** — the REST of the vault (``exclude_path_prefix`` drops the
        current deal folder, which bucket A already covers at full tier),
        capped at ``≤ internal`` via ``sensitivity_max`` so confidential / MNPI
        from another deal or the general vault can NEVER surface here (the
        server-side cap — the model never even sees capped-out content).

    The two buckets are path-disjoint by construction (A is only ``Projects/
    <project>/``; B excludes exactly that prefix), so the union never double-
    counts the current deal.
    """
    deal_prefix = f"Projects/{project}/"
    buckets = [rtv.Filter(project=project, path_prefix=deal_prefix)]
    if cross_projects:
        buckets.append(rtv.Filter(
            sensitivity_max=_CROSS_PROJECT_CEILING,
            exclude_path_prefix=deal_prefix,
        ))
    return buckets


def fetch_sources(
    vault_root: Path,
    project: str,
    message: str,
    *,
    client,  # routines.shared.ollama_client.OllamaClient (duck-typed for tests)
    limit: int = 8,
    cross_projects: bool = False,
) -> list[ChatSource]:
    """Run recall for one chat turn and shape the hits into ``ChatSource`` records.

    ``cross_projects`` False (v1 default): STRICTLY scoped to ``Projects/
    <project>/`` — a single project-filtered query, no cross-project leak.

    ``cross_projects`` True: widen to the WHOLE vault under the two-bucket rule
    (see ``_recall_filters``) — the current deal at full tier PLUS the rest of
    the vault capped at ``≤ internal``. Each bucket is queried for up to
    ``limit`` hits; the union is de-duplicated by path (keeping the higher
    score) and re-ranked by score, then truncated to ``limit``.

    Each surviving hit becomes a ``ChatSource`` (path + score + clipped
    excerpt). On any recall failure (e.g. no index) we log and skip that bucket
    — chat still answers, just with fewer / no citations.
    """
    if not message.strip():
        return []
    try:
        from routines.recall import retrieve as rtv
    except ImportError as e:  # pragma: no cover — deps always present in venv
        log.warning("project-chat: recall package not importable: %s", e)
        return []

    # path -> best NoteHit across buckets (higher score wins on a dup, though the
    # buckets are path-disjoint so a real collision shouldn't occur).
    best_by_path: dict[str, object] = {}
    for f in _recall_filters(rtv, project, cross_projects=cross_projects):
        try:
            hits = rtv.query(
                message, vault_root=vault_root, client=client, filter_=f, limit=limit,
            )
        except Exception as e:  # noqa: BLE001 — citations are best-effort
            log.warning("project-chat: recall query failed for %s: %s", project, e)
            continue
        for h in hits:
            prev = best_by_path.get(h.path)
            if prev is None or float(h.score) > float(prev.score):  # type: ignore[attr-defined]
                best_by_path[h.path] = h

    merged = sorted(
        best_by_path.values(), key=lambda h: float(h.score), reverse=True,  # type: ignore[attr-defined]
    )[:limit]

    sources: list[ChatSource] = []
    for h in merged:
        excerpt = (getattr(h, "best_chunk_text", "") or getattr(h, "body_excerpt", "") or "").strip()
        if len(excerpt) > _EXCERPT_MAX:
            excerpt = excerpt[:_EXCERPT_MAX].rstrip() + "…"
        sources.append(ChatSource(
            path=str(h.path),  # type: ignore[attr-defined]
            score=float(h.score),  # type: ignore[attr-defined]
            excerpt=excerpt,
        ))
    return sources


def gather_context(
    vault_root: Path,
    project: str,
    message: str,
    *,
    client,
    history_turns: int = 6,
    recall_limit: int = 8,
    cross_projects: bool = False,
) -> ChatContext:
    """Assemble the full per-turn context bundle (history + sensitivity + sources).

    ``cross_projects`` is forwarded to ``fetch_sources`` — when True, recall
    widens to the whole vault under the ``≤ internal`` out-of-deal cap. The
    sensitivity LANE gating (which LLM the turn routes to) is independent of it:
    it always reads the CURRENT deal's own tier, so a confidential deal stays on
    local Ollama whether or not cross-project recall is on.
    """
    sensitivity = resolve_sensitivity(vault_root, project)
    return ChatContext(
        project=project,
        message=message,
        history=load_history(vault_root, project, history_turns=history_turns),
        sources=fetch_sources(
            vault_root, project, message,
            client=client, limit=recall_limit, cross_projects=cross_projects,
        ),
        sensitivity=sensitivity,
        lane=gate_lane(sensitivity),
    )
