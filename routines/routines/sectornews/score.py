"""Relevance + materiality scoring for sector-news items.

One Ollama call per item via qwen3:8b (the local "haiku" — fast + cheap):
returns {relevance: 0-10, materiality: 0-10, rationale: ""}.

Why one model call per item rather than batch:
    - Batch prompts often mis-attribute scores in qwen3 outputs
    - Per-item is sequential but qwen3:8b warm is ~5s/call; with N=10
      items per sector that's <1 min total
    - Easier to log / debug per-item
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from routines.sectornews.firecrawl_client import FCResult
from routines.shared.ollama_client import OllamaClient, OllamaError, parse_json_response

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are scoring news items for a UK M&A / corporate-development professional.

For each item, output strict JSON only with this shape:
{
  "relevance": <integer 0-10>,
  "materiality": <integer 0-10>,
  "rationale": "<1 sentence>"
}

- "relevance" = how relevant is this to the sector and the operator's lens
  (M&A, corporate finance, sector consolidation, valuation context)?
  10 = directly material; 5 = adjacent; 0 = off-topic.
- "materiality" = how big a deal is the news itself? Major M&A announcement,
  earnings beat/miss, regulatory shift = high. Routine product launch, blog
  post = low.
- Both should be calibrated: most items will land 4-7. Reserve 9-10 for
  genuinely deal-changing news.

Rules:
- Output ONLY the JSON object. No prose preamble, no markdown fences.
- Be honest about low-quality / promotional content (relevance and
  materiality both low).
"""


@dataclass
class ScoredItem:
    item: FCResult
    relevance: int
    materiality: int
    rationale: str

    @property
    def composite(self) -> float:
        """Geometric-mean-ish composite. Both must be reasonable for a high
        composite. Keeps low-quality high-materiality and high-relevance noise
        out of the top of the newsletter."""
        return (self.relevance * self.materiality) ** 0.5


def score_items(
    items: list[FCResult],
    sector_name: str,
    aliases: list[str],
    *,
    client: OllamaClient,
    model: str = "qwen3:8b",
) -> list[ScoredItem]:
    """Score every item; return list aligned to input order. Items that fail
    to score get (0, 0, "score failed: <reason>")."""
    out: list[ScoredItem] = []
    sector_context = sector_name
    if aliases:
        sector_context += " (sub-sectors: " + ", ".join(aliases[:5]) + ")"

    for item in items:
        prompt = (
            f"Sector context: {sector_context}\n\n"
            f"News item:\n"
            f"  title: {item.title}\n"
            f"  url: {item.url}\n"
            f"  snippet: {item.snippet[:600]}\n"
        )
        try:
            resp = client.chat(
                model=model, prompt=prompt, system=SYSTEM_PROMPT,
                json_mode=True, temperature=0.1, max_tokens=200,
            )
            data = parse_json_response(resp.content)
            out.append(ScoredItem(
                item=item,
                relevance=int(data.get("relevance", 0)),
                materiality=int(data.get("materiality", 0)),
                rationale=str(data.get("rationale", "")).strip(),
            ))
        except (OllamaError, ValueError, KeyError) as e:
            logger.warning("score failed for %s: %s", item.url, e)
            out.append(ScoredItem(
                item=item, relevance=0, materiality=0,
                rationale=f"score failed: {e}",
            ))

    return out
