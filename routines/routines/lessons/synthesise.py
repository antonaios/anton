"""Cluster lessons via BERTopic (reusing the learning-loop clusterer) and
generate Registers/Lessons.md entries for each flagged pattern + cluster.

Reuses ``routines.learning.cluster.cluster_events`` so the routines share
one BERTopic config rather than duplicating it. LessonItems are adapted
to FeedbackEvents at the boundary (text + project as artifact_kind),
clusters come back, then we map back to LessonItems via text equality.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from routines.lessons.schema import LessonCluster, LessonItem, LessonPattern
from routines.shared.ollama_client import OllamaClient, OllamaError, parse_json_response

log = logging.getLogger(__name__)


DEFAULT_MODEL = "qwen3:14b"


# ── Clustering (Mode B) ───────────────────────────────────────────────────


def cluster_lessons(items: list[LessonItem], *, min_cluster_size: int = 2) -> list[LessonCluster]:
    """BERTopic-cluster LessonItems across projects.

    Trivially returns ``[]`` when fewer than ``min_cluster_size`` items
    exist or when all items are from a single project (cross-project
    clustering needs ≥ 2 distinct projects to be meaningful).
    """
    if len(items) < min_cluster_size:
        return []
    if len({it.project for it in items}) < 2:
        log.info("lessons: only one project has lessons — skipping cross-project clustering")
        return []

    from routines.learning.cluster import cluster_events
    from routines.learning.schema import FeedbackEvent

    fake_events = [
        FeedbackEvent(
            timestamp="",
            text=it.text,
            source="lessons-learned",
            prior_artifact=it.project,
            prior_artifact_kind=it.project,
        )
        for it in items
    ]
    raw_clusters = cluster_events(fake_events, min_cluster_size=min_cluster_size)

    text_to_item = {it.text: it for it in items}
    out: list[LessonCluster] = []
    for c in raw_clusters:
        members = [text_to_item[ev.text] for ev in c.events if ev.text in text_to_item]
        if len(members) < min_cluster_size:
            continue
        projects = sorted({m.project for m in members})
        if len(projects) < 2:
            continue   # single-project echo chamber, not a cross-project pattern
        out.append(LessonCluster(theme="(unlabeled)", items=members, projects=projects))
    return out


# ── LLM labelling — clusters and patterns ─────────────────────────────────


_CLUSTER_SYSTEM = """\
You are reviewing recurring lessons that emerged across multiple closed
M&A deals. Your job is to spot what's a genuinely repeatable pattern vs
a one-off project-specific anecdote, and draft a Registers/Lessons.md
entry the operator can accept.

Each input is a cluster of lesson bullets from 2+ projects. Return
strict JSON only:

{
  "theme": "<3-7 word label>",
  "slug": "<short kebab slug, e.g. 'lesson-skill-fallback-markdown'>",
  "pattern": "<1-2 sentences naming the pattern in operator-neutral terms>",
  "why_it_matters": "<1-2 sentences>",
  "what_to_do": "<1-2 sentences — the operational rule or default>",
  "rationale": "<1 sentence on why this clusters across the projects shown>"
}

Rules:
- en-GB spelling.
- If the cluster is actually noise / disparate / project-specific, return
  {"theme": "(reject)", "slug": "", "pattern": "", "why_it_matters": "",
   "what_to_do": "no clear cross-project pattern", "rationale": ""}.
"""


_PATTERN_SYSTEM = """\
You are formalising an operator-flagged pattern from a single project's
Lessons Learned file into a Registers/Lessons.md entry. The operator
already decided this is worth promoting; your job is to phrase it well.

Input has: pattern_text (raw bullet), project, proposed_slug (may be
empty). Return strict JSON only:

{
  "slug": "<short kebab slug>",
  "title": "<3-6 word title>",
  "pattern": "<1-2 sentences>",
  "why_it_matters": "<1-2 sentences>",
  "what_to_do": "<1-2 sentences — the operational rule>"
}

Rules:
- en-GB spelling.
- Keep the slug if proposed_slug is set and well-formed.
- Don't restate the bullet verbatim — extract the underlying rule.
"""


def label_cluster(c: LessonCluster, *, client: OllamaClient, model: str = DEFAULT_MODEL) -> None:
    """Populate ``c.theme`` and ``c.proposed_entry_markdown``."""
    samples = "\n".join(f"- [{it.project}] {it.text}" for it in c.items[:8])
    projects = ", ".join(c.projects)
    user = (
        f"Cluster of {c.size} lessons across projects: {projects}\n\n"
        f"Bullets:\n{samples}\n"
    )
    info = _llm_json(client, model, user, _CLUSTER_SYSTEM)
    theme = info.get("theme") or "(unlabeled)"
    c.theme = theme
    if theme in ("(reject)", "(unlabeled)"):
        return
    c.proposed_entry_markdown = _render_register_entry(
        slug=info.get("slug") or _slug_from_theme(theme),
        title=theme,
        first_seen_projects=c.projects,
        pattern=info.get("pattern") or "",
        why_it_matters=info.get("why_it_matters") or "",
        what_to_do=info.get("what_to_do") or "",
    )


def label_pattern(p: LessonPattern, *, client: OllamaClient, model: str = DEFAULT_MODEL) -> str | None:
    """Render a Registers/Lessons.md entry for an already-flagged pattern.

    Returns the markdown block or ``None`` on LLM failure."""
    user = (
        f"Pattern from project {p.project}:\n"
        f"- text: {p.text}\n"
        f"- proposed_slug: {p.proposed_slug or '(none)'}\n"
    )
    info = _llm_json(client, model, user, _PATTERN_SYSTEM)
    if not info:
        return None
    return _render_register_entry(
        slug=info.get("slug") or p.proposed_slug or _slug_from_theme(info.get("title") or p.text),
        title=info.get("title") or "(untitled)",
        first_seen_projects=[p.project],
        pattern=info.get("pattern") or "",
        why_it_matters=info.get("why_it_matters") or "",
        what_to_do=info.get("what_to_do") or "",
    )


def _llm_json(client: OllamaClient, model: str, user: str, system: str) -> dict[str, Any]:
    try:
        resp = client.chat(model=model, prompt=user, system=system, json_mode=True)
    except OllamaError as e:
        log.warning("lessons: LLM call failed: %s", e)
        return {}
    try:
        return parse_json_response(resp.content) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("lessons: JSON parse failed: %s", e)
        return {}


def _slug_from_theme(theme: str) -> str:
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in theme.lower())
    safe = "-".join(filter(None, safe.split("-")))
    return f"lesson-{safe}"[:64] if safe else "lesson-unlabelled"


def _render_register_entry(
    *, slug: str, title: str, first_seen_projects: list[str],
    pattern: str, why_it_matters: str, what_to_do: str,
) -> str:
    first_seen = ", ".join(f"[[Projects/{p}]]" for p in first_seen_projects) or "—"
    today = date.today().isoformat()
    return (
        f"## {slug} — {title}\n"
        f"- **First seen:** {first_seen}\n"
        f"- **First promoted:** {today}\n"
        f"- **Pattern:** {pattern}\n"
        f"- **Why it matters:** {why_it_matters}\n"
        f"- **What to do about it:** {what_to_do}\n"
        f"- **Cross-references:** \n"
    )


# Suppress unused-import warning
_ = json
