"""Build a markdown proposal from feedback clusters.

For each cluster, ask local qwen3:14b to:
  1. Name the theme in 3-6 words.
  2. Suggest which template / skill / routine to amend.
  3. Draft the section heading + 1-3 sentences explaining what should
     be added.

The output is a self-contained markdown file the operator reviews in
Obsidian and applies (or doesn't). Each cluster includes verbatim
sample queries so the operator can see why the proposal exists.

This module never edits templates. Auto-mutation is explicitly out of
scope — same human-in-the-loop pattern as memory promotion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from routines.learning.schema import FeedbackCluster, ProposalDoc

log = logging.getLogger(__name__)


_THEME_SYSTEM = """\
You are reviewing recurring follow-up questions an M&A operator has been
asking after Claude delivered a routine output (company profile, IC memo,
sector read, etc.). Your job is to spot what the templates / skills are
missing and propose a concrete, narrow change.

You are given a cluster of similar follow-up questions plus the artifact
kinds they followed. Return strict JSON only:

{
  "theme": "<3-7 word label>",
  "target": "<best guess of which template/skill to amend, e.g. 'Templates/company-profile.md', 'Templates/ic-memo.md', 'equity-research workflow'>",
  "section_heading": "<a heading the operator should add to the target, e.g. 'Capital expenditure'>",
  "what_to_add": "<1-3 sentences specifying exactly what content/data the section should contain — be concrete>",
  "rationale": "<1 sentence — why this is the right call, citing the recurrence count>"
}

Rules:
- Use en-GB spelling.
- Don't restate the questions — propose the FIX.
- If the cluster looks ambiguous or the questions don't actually share a
  theme, return {"theme": "(reject)", "target": "", "section_heading": "",
  "what_to_add": "no clear pattern", "rationale": ""}.
- Prefer "Templates/<name>.md" as the target when the questions all
  follow the same kind of deliverable.
- For company-research patterns, the target is almost always
  Templates/company-profile.md.
"""


def build_proposal(
    clusters: list[FeedbackCluster],
    *,
    client=None,
    model: str = "qwen3:14b",
) -> ProposalDoc:
    """Return a ProposalDoc with LLM-named themes per cluster.

    The actual markdown rendering happens in `render_markdown()` — split
    so callers can serialise the doc to JSON for audit purposes too.
    """
    if not clusters:
        return ProposalDoc(generated_at=datetime.now(timezone.utc), clusters=[])

    if client is None:
        from routines.shared.ollama_client import OllamaClient
        client = OllamaClient()

    enriched: list[FeedbackCluster] = []
    for c in clusters:
        try:
            info = _label_cluster(c, client=client, model=model)
        except Exception as e:  # noqa: BLE001
            log.warning("learning: label_cluster failed: %s", e)
            info = {"theme": "(unlabeled)", "rationale": "", "target": "", "section_heading": "", "what_to_add": ""}
        # Stash the LLM output on the cluster via centroid_text (json-encoded).
        c.theme = info.get("theme") or "(unlabeled)"
        c.centroid_text = json.dumps(info)
        enriched.append(c)

    return ProposalDoc(generated_at=datetime.now(timezone.utc), clusters=enriched)


def _label_cluster(c: FeedbackCluster, *, client, model: str) -> dict:
    from routines.shared.ollama_client import parse_json_response

    samples = "\n".join(f"- {ev.text}" for ev in c.events[:8])
    artifact_kinds = ", ".join(c.artifact_kinds) or "unknown"
    user_msg = (
        f"Cluster of {c.size} follow-up questions following these artifact kinds: {artifact_kinds}\n\n"
        f"Questions:\n{samples}\n"
    )
    resp = client.chat(model=model, prompt=user_msg, system=_THEME_SYSTEM, json_mode=True)
    return parse_json_response(resp.content)


# ── Markdown writer ───────────────────────────────────────────────────────


def render_markdown(doc: ProposalDoc) -> str:
    """Render a ProposalDoc as a markdown file ready to land in
    `Routines/learning/<date>-template-evolution.md`."""
    date_str = doc.generated_at.date().isoformat()
    accepted = [c for c in doc.clusters if c.theme != "(reject)" and c.theme != "(unlabeled)"]
    rejected = [c for c in doc.clusters if c.theme == "(reject)" or c.theme == "(unlabeled)"]

    out: list[str] = []
    out.append("---")
    out.append("type: learning-proposal")
    out.append("sensitivity: internal")
    out.append(f"date: {date_str}")
    out.append(f"clusters: {len(doc.clusters)}")
    out.append(f"accepted: {len(accepted)}")
    out.append("status: pending-review")
    out.append("tags: [learning, self-improvement, template-evolution, routines]")
    out.append("---")
    out.append("")
    out.append(f"# Template-evolution proposals · {date_str}")
    out.append("")
    out.append(
        "Patterns detected in recent Claude Code session logs and explicit "
        "operator feedback. Each cluster below shows a recurring follow-up "
        "question theme and proposes a concrete template/skill change. "
        "**Nothing is auto-applied** — review, edit, apply the changes you "
        "agree with."
    )
    out.append("")
    out.append("## Operator action")
    out.append("")
    out.append(
        "1. Read each proposal. Look at the sample queries to confirm the "
        "pattern is real.\n"
        "2. For accepted proposals, edit the named template / skill (or ask "
        "Claude to do it).\n"
        "3. Change this file's frontmatter `status:` to `applied` or "
        "`rejected` (with notes on rejections — that signal feeds back into "
        "the next scan)."
    )
    out.append("")

    if not accepted and not rejected:
        out.append("_No clusters with enough recurrence yet. Come back next week._")
        return "\n".join(out)

    if accepted:
        out.append("---")
        out.append("")
        out.append(f"## Accepted patterns ({len(accepted)})")
        out.append("")
        for i, c in enumerate(accepted, 1):
            try:
                info = json.loads(c.centroid_text or "{}")
            except json.JSONDecodeError:
                info = {}
            out.append(f"### {i}. {c.theme}")
            out.append("")
            out.append(f"- **Recurrence:** {c.size} events across {len(c.artifact_kinds) or 1} artifact kind(s)")
            if c.artifact_kinds:
                out.append(f"- **Artifact kinds:** {', '.join(c.artifact_kinds)}")
            target = info.get("target") or "(unknown — operator to decide)"
            heading = info.get("section_heading") or "—"
            what = info.get("what_to_add") or "—"
            rationale = info.get("rationale") or ""
            out.append(f"- **Target:** `{target}`")
            out.append(f"- **Proposed section heading:** _{heading}_")
            out.append("")
            out.append(f"**What to add:** {what}")
            if rationale:
                out.append("")
                out.append(f"_Rationale:_ {rationale}")
            out.append("")
            out.append("<details><summary>Sample queries (verbatim)</summary>")
            out.append("")
            for ev in c.events[:6]:
                src = "scan" if ev.source == "scan" else "note"
                date_part = (ev.timestamp or "")[:10]
                out.append(f"- _[{src} · {date_part}]_ {ev.text}")
            if c.size > 6:
                out.append(f"- … and {c.size - 6} more")
            out.append("")
            out.append("</details>")
            out.append("")

    if rejected:
        out.append("---")
        out.append("")
        out.append(f"## Skipped (no clear pattern, {len(rejected)})")
        out.append("")
        for c in rejected:
            out.append(f"- {c.size} events: " + ", ".join(f"_{ev.text[:60]}_" for ev in c.events[:3]))
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "_Generated by `routines.learning.propose` — fully local. No "
        "session-log content was uploaded anywhere; the LLM call ran on "
        "local Ollama qwen3:14b._"
    )

    return "\n".join(out)


def write_proposal(vault_root: Path, doc: ProposalDoc) -> Path:
    """Atomic-write the proposal under `Routines/learning/`. Returns path."""
    date_str = doc.generated_at.date().isoformat()
    path = vault_root / "Routines" / "learning" / f"{date_str}-template-evolution.md"
    md = render_markdown(doc)
    # F-4 (codex r1 SEV-1 → r2 SEV-2): the legacy "helper not on this branch"
    # direct-write fallback is GONE — vault_writer ships in this package, so
    # an ImportError means a broken install, and any fallback here is a
    # chokepoint bypass. Fail closed: let import/policy errors propagate.
    from routines.shared.vault_writer import atomic_write  # lazy: heavy deps

    atomic_write(path, md, vault_root=vault_root)
    doc.markdown_path = str(path.relative_to(vault_root).as_posix())
    return path


# Suppress unused-import warning.
_ = asdict
_ = Optional
