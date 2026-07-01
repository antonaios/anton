"""Render and atomic-write the cross-project lessons proposal."""

from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

from routines.lessons.schema import LessonsProposal
from routines.shared.vault_writer import atomic_write


def proposal_path(vault_root: Path, the_date: date_cls) -> Path:
    return vault_root / "Routines" / "lessons-learned" / f"{the_date.isoformat()}-cross-project.md"


def write_proposal(vault_root: Path, doc: LessonsProposal, the_date: date_cls) -> Path:
    path = proposal_path(vault_root, the_date)
    atomic_write(path, render_markdown(doc, the_date), vault_root=vault_root)
    return path


def render_markdown(doc: LessonsProposal, the_date: date_cls) -> str:
    date_str = the_date.isoformat()
    out: list[str] = [
        "---",
        "type: lessons-proposal",
        "sensitivity: internal",
        f"date: {date_str}",
        f"projects_scanned: {len(doc.projects_scanned)}",
        f"closed: {doc.closed_count}",
        f"open: {doc.open_count}",
        f"patterns: {len(doc.patterns)}",
        f"clusters: {len(doc.clusters)}",
        "status: pending-review",
        "tags: [lessons, cross-project, routines]",
        "---",
        "",
        f"# Cross-project Lessons — proposed register additions · {date_str}",
        "",
        (
            "Patterns extracted from each project's `13 Lessons Learned.md`, "
            "ready for review and promotion to `Registers/Lessons.md`. **Nothing "
            "is auto-applied** — review, edit, and paste into the register the "
            "entries you agree with."
        ),
        "",
        f"_Projects scanned: {len(doc.projects_scanned)} ({doc.closed_count} closed, {doc.open_count} open)._",
        "",
        "## Operator action",
        "",
        "1. Read each proposed entry. The Mode A patterns are operator-flagged "
        "from individual projects (high confidence); Mode B clusters are "
        "cross-project themes inferred by BERTopic + qwen3:14b.",
        "2. Paste accepted entries into `Registers/Lessons.md` under `## Lessons`.",
        "3. Update this proposal's frontmatter `status:` to `applied` or "
        "`rejected` once handled.",
        "",
    ]

    if not doc.projects_scanned:
        out += [
            "---", "",
            "_No `13 Lessons Learned.md` files found under `Projects/` or `Archive/`. "
            "Routine is dormant until at least one project carries lessons._",
        ]
        return "\n".join(out)

    if not doc.patterns and not doc.clusters:
        out += [
            "---", "",
            "_Scanned projects had no patterns flagged for promotion and no "
            "cross-project clusters surfaced (Mode B needs ≥ 2 projects with "
            "overlapping themes). Come back once more projects close._",
        ]
        return "\n".join(out)

    # ── Mode A: operator-flagged patterns ─────────────────────────────────
    if doc.patterns:
        out += [
            "---", "",
            f"## Mode A — operator-flagged patterns ({len(doc.patterns)})", "",
            "These were already called out under a project's _Patterns worth "
            "promoting_ section. The proposed register entry below is the "
            "LLM's draft; edit before pasting.", "",
        ]
        for i, p in enumerate(doc.patterns, 1):
            out += [
                f"### {i}. From [[Projects/{p.project}]] ({p.project_status})", "",
                f"_Raw bullet:_ {p.text}",
                "",
            ]
            if p.proposed_slug:
                out.append(f"_Proposed slug:_ `{p.proposed_slug}`")
                out.append("")
            # The rendered register-entry markdown is attached on the proposal
            # via the same channel as clusters; see `synthesise.label_pattern`.

    # ── Mode B: cross-project clusters ────────────────────────────────────
    if doc.clusters:
        out += [
            "---", "",
            f"## Mode B — cross-project clusters ({len(doc.clusters)})", "",
            "BERTopic-discovered themes that recur across 2+ projects. Lower "
            "confidence than Mode A but higher cross-project leverage.", "",
        ]
        for i, c in enumerate(doc.clusters, 1):
            if c.theme in ("(reject)", "(unlabeled)"):
                continue
            out += [
                f"### {i}. {c.theme}", "",
                f"_Recurrence:_ {c.size} lesson(s) across "
                f"{len(c.projects)} project(s): {', '.join(c.projects)}", "",
            ]
            if c.proposed_entry_markdown:
                out += [
                    "**Proposed `Registers/Lessons.md` entry:**", "",
                    "```markdown",
                    c.proposed_entry_markdown.rstrip(),
                    "```", "",
                ]
            out += ["<details><summary>Sample bullets</summary>", ""]
            for it in c.items[:6]:
                out.append(f"- _[{it.project} · {it.section}]_ {it.text}")
            if c.size > 6:
                out.append(f"- … and {c.size - 6} more")
            out += ["", "</details>", ""]

    out += [
        "---", "",
        "_Generated by `routines.lessons` — fully local. LLM labelling ran on "
        "local Ollama qwen3:14b._",
    ]
    return "\n".join(out)
