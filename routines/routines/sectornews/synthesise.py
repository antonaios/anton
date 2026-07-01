"""Newsletter synthesis: turn ranked scored items into a markdown newsletter.

Two paths:
    - LLM synthesis via qwen3:14b (default — produces narrative grouping)
    - Plain ranked list (fallback, if LLM fails)

The newsletter format mirrors the morning-note style: top items as
narrative paragraphs grouped by theme, then a "Other items worth a
look" appendix listing the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from routines.sectornews.score import ScoredItem
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import serialise_note

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are drafting a sector newsletter for a UK M&A / corporate-development
professional. The reader scans the whole thing in 3-5 minutes.

Style:
- Investor-grade, hedge-light, M&A-literate.
- Group items into 2-4 themes if patterns are visible (e.g. "M&A activity",
  "Earnings", "Regulatory", "Strategic moves"). If items are scattered,
  use a single "Items" section.
- For each item: 2-3 sentence summary, then `[source]` link inline.
- Cite sources as `[source](URL)` markdown links — NOT wikilinks (the
  links are external, not vault-internal).
- No filler ("In this week's newsletter…"), no boilerplate sign-offs.
- British English.
- Preserve specifics: numbers, dates, company names, deal values.
- Do not invent specifics that aren't in the source snippets.

Reply with the newsletter body only — no frontmatter, no title (the caller
adds those).
"""


def synthesise_newsletter(
    sector_name: str,
    scored: list[ScoredItem],
    *,
    client: OllamaClient,
    top_n: int = 10,
    composite_threshold: float = 4.0,
    model: str = "qwen3:14b",
) -> str:
    """Produce the markdown body of the newsletter (without frontmatter)."""
    survivors = sorted(
        [s for s in scored if s.composite >= composite_threshold],
        key=lambda s: s.composite,
        reverse=True,
    )[:top_n]

    if not survivors:
        return (
            f"## {sector_name} — no material items this period\n\n"
            f"No items above the relevance/materiality composite threshold "
            f"({composite_threshold}). Try widening the time window or "
            f"adding explicit sources to `Sectors/{sector_name}.md`.\n"
        )

    # Build the LLM input: enumerate items with their snippets
    items_block_lines: list[str] = []
    for i, s in enumerate(survivors, 1):
        items_block_lines.append(
            f"{i}. **{s.item.title}**\n"
            f"   URL: {s.item.url}\n"
            f"   Snippet: {s.item.snippet[:400]}\n"
            f"   Relevance: {s.relevance}/10 · Materiality: {s.materiality}/10\n"
            f"   Why: {s.rationale}\n"
        )
    items_block = "\n".join(items_block_lines)

    prompt = (
        f"Sector: {sector_name}\n"
        f"Period: last 7 days (today is {date.today().isoformat()})\n\n"
        f"Items (already deduped + scored, ranked by composite score):\n\n"
        f"{items_block}\n\n"
        "Draft the newsletter body now."
    )

    try:
        resp = client.chat(
            model=model, prompt=prompt, system=SYSTEM_PROMPT,
            temperature=0.3, max_tokens=2000,
        )
        body = resp.content.strip()
    except OllamaError as e:
        logger.warning("synthesis failed; falling back to ranked list: %s", e)
        body = _fallback_list(survivors)

    # Always append a "Other items" section listing the rest
    rest = sorted(
        [s for s in scored if s not in survivors],
        key=lambda s: s.composite,
        reverse=True,
    )
    if rest:
        body += "\n\n## Other items worth a look\n\n"
        for s in rest[:15]:
            body += f"- [{s.item.title}]({s.item.url}) — {s.rationale}\n"

    return body


def _fallback_list(survivors: list[ScoredItem]) -> str:
    """If LLM synthesis fails, produce a deterministic ranked list."""
    out = ["## Top items"]
    for s in survivors:
        out.append(
            f"- **[{s.item.title}]({s.item.url})** "
            f"(relevance {s.relevance}/10, materiality {s.materiality}/10) — "
            f"{s.rationale}"
        )
    return "\n".join(out)


def build_full_newsletter_md(
    *,
    sector_name: str,
    body: str,
    sources_used: list[str],
    composite_scores: list[float],
    run_id: str,
) -> str:
    """Wrap the synthesised body with frontmatter for vault storage."""
    metadata: dict[str, Any] = {
        "type": "newsletter",
        "sector": sector_name,
        "date": date.today().isoformat(),
        "sensitivity": "internal",
        "run-id": run_id,
        "sources-count": len(sources_used),
        "items-scored-mean-composite": (
            round(sum(composite_scores) / len(composite_scores), 2)
            if composite_scores else 0.0
        ),
        "tags": ["newsletter", "sector-news", sector_name.lower()],
        "tldr": f"Auto-generated sector newsletter for {sector_name} ({date.today().isoformat()}).",
    }
    title_block = f"# {sector_name} sector news — {date.today().isoformat()}\n\n"
    return serialise_note(metadata, title_block + body)
