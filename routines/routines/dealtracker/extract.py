"""LLM extraction of deal records from news articles or transcript snippets.

Uses local Ollama (qwen3:14b) with JSON-mode. Returns a partially-populated
DealRecord — many fields will be missing; downstream callers should treat
empty values as "not stated in source", not "zero". Per CLAUDE.md §5.1:
no LLM does the maths — multiples that aren't explicitly stated in the
source come back as None, not LLM-computed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from routines.dealtracker.schema import (
    DealRecord,
    build_deal_id,
    classify_acquirer_type,
)
from routines.shared.ollama_client import OllamaClient, OllamaError, parse_json_response
from routines.guards.injection import scan_ingested_text

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You extract structured M&A deal records from news articles or transcripts.

Rules:
- If a field is not stated in the source, return null. Do NOT infer or compute
  what the source did not state explicitly.
- Multiples (Revenue Multiple, EBIT Multiple, EBITDA Multiple): only populate
  if the source explicitly states them. Do NOT compute them from EV ÷ Revenue.
- Currency: the reporting currency of the EV / financials, not the deal
  consideration currency if those differ. If the article says "$5bn EV with
  €4bn revenue", reporting currency is mixed — leave currency blank and note
  in deal_description.
- Dates: ISO format YYYY-MM-DD. If only a month/year is given, use the first
  of the month. If no date stated, return null.
- Sectors: comma-separated list using Mergermarket-style categories
  (e.g. "Leisure,Real Estate", "Construction,Leisure,Real Estate").
- Strings should preserve specific names verbatim (target/bidder/seller).
- Acquirer type: classify the bidder as "Financial" (a private-equity firm,
  buyout fund, financial sponsor, or institutional financial investor) or
  "Strategic" (a corporate / trade buyer operating in the target's industry).
  If the source does not make the bidder's nature clear, return null — do NOT
  guess.
- The deal_description should be 3-6 sentences capturing the strategic
  rationale and any conditional / approval status.

Reply with a single JSON object only. No markdown fences, no preamble.
"""


SCHEMA_HINT = """\
Return a JSON object with this exact shape (use null for unknown fields):
{
  "announced_date": "YYYY-MM-DD or null",
  "completed_date": "YYYY-MM-DD or null",
  "target_company": "...",
  "target_description": "1-2 sentence paraphrase of what the target does",
  "target_sector": "Mergermarket-style sectors, comma-separated",
  "target_subsector": "more specific categorisation",
  "target_country": "...",
  "bidder_company": "...",
  "bidder_description": "1-2 sentence paraphrase",
  "bidder_sector": "...",
  "bidder_country": "...",
  "acquirer_type": "Strategic|Financial|null — Financial=PE/sponsor/fund, Strategic=corporate/trade buyer",
  "seller_company": "... or empty if not stated",
  "seller_description": "...",
  "seller_sector": "...",
  "seller_country": "...",
  "currency": "GBP|USD|EUR|... — reporting currency for EV/financials",
  "enterprise_value_m": <number in millions of currency above, or null>,
  "reported_y1_date": "YYYY-MM-DD or null — period for the financials below",
  "reported_revenue_m_y1": <number in millions, or null>,
  "reported_ebitda_m_y1": <number in millions, or null>,
  "reported_ebit_m_y1": <number in millions, or null>,
  "reported_revenue_multiple_y1": <number or null — only if explicitly stated>,
  "reported_ebit_multiple_y1": <number or null — only if explicitly stated>,
  "reported_ebitda_multiple_y1": <number or null — only if explicitly stated>,
  "deal_description": "3-6 sentence paraphrased description of the deal, including conditional / approval status",
  "deal_value_gbp_m": <number or null — GBP equivalent of deal value if stated>
}
"""


def extract_deal(
    *,
    text: str,
    source_url: str,
    client: OllamaClient,
    model: str = "qwen3:14b",
    subsector_slug: str = "",
    scan: bool = True,
) -> DealRecord:
    """Run LLM extraction; return a DealRecord. Raises OllamaError on
    transport / unrecoverable JSON parse failure. Always returns a DealRecord
    even if many fields are empty — caller decides whether to append based on
    completeness.

    ``subsector_slug`` is the sector-context the cron ran for (lowercase-
    hyphenated, derived from ``profile.md``'s ``sector_sub_lens``); the
    extractor stamps it onto the record. Non-cron paths (operator paste via
    the deal-tracker route) leave it blank.

    ``scan=False`` skips the injection screen — used by the sector-news auto-feed,
    which already screened the identical text at its own ingestion boundary
    (avoids a duplicate scan + audit row; #sec-injection-guard 3a).
    """
    if not text.strip():
        raise ValueError("empty text passed to extract_deal")

    # #sec-injection-guard 3a: scan the untrusted source text (detect-and-audit;
    # NEVER blocks) BEFORE truncation, so the guard sees the full submission.
    # scan=False when the caller already screened the identical text (the
    # sector-news auto-feed), to avoid a duplicate scan + audit row.
    if scan:
        scan_ingested_text(text, source="dealtracker:extract")

    # Truncate very long inputs (qwen3:14b context is generous but extraction
    # quality drops on noise). 8k chars covers a typical news article.
    if len(text) > 8000:
        text = text[:8000] + "\n\n[... truncated ...]"

    prompt = f"{SCHEMA_HINT}\n\nSource:\n{text}"
    resp = client.chat(
        model=model, prompt=prompt, system=SYSTEM_PROMPT,
        json_mode=True, temperature=0.1, max_tokens=1500,
    )
    data = parse_json_response(resp.content)

    return _normalise(
        data,
        source_url=source_url,
        source_excerpt=text[:500],
        subsector_slug=subsector_slug,
    )


def _normalise(
    data: dict[str, Any],
    *,
    source_url: str,
    source_excerpt: str,
    subsector_slug: str = "",
) -> DealRecord:
    """Build a DealRecord from the LLM's JSON response.

    Lean-schema-specific mapping rules (post 2026-06-01 WS2):
    * ``source`` mirrors ``source_url`` — the cron-fed path is auto-sourced
      by definition; operator-paste path also uses the URL as source.
    * ``deal_id`` is generated internally as ``PT-<announced-date>-<target-slug>``
      (NOT a Mergermarket ID). Empty when either date or target is missing.
    * ``strategic_commentary`` stays blank — deep-research / operator fills it
      via a later edit; the LLM extractor never populates it.
    * ``subsector_slug`` comes from the caller (sector context), NOT from the
      LLM response — there's no reliable subsector signal in the article body.
    """
    announced_date = _parse_date(data.get("announced_date"))
    target_company = _str(data.get("target_company"))
    return DealRecord(
        announced_date=announced_date,
        completed_date=_parse_date(data.get("completed_date")),
        target_company=target_company,
        target_description=_str(data.get("target_description")),
        target_sector=_str(data.get("target_sector")),
        target_subsector=_str(data.get("target_subsector")),
        subsector_slug=subsector_slug,
        target_country=_str(data.get("target_country")),
        bidder_company=_str(data.get("bidder_company")),
        bidder_description=_str(data.get("bidder_description")),
        bidder_sector=_str(data.get("bidder_sector")),
        bidder_country=_str(data.get("bidder_country")),
        # Acquirer type — classify from the LLM hint, falling back to a
        # deterministic keyword heuristic; blank when ambiguous (#21-comps Q5).
        acquirer_type=classify_acquirer_type(
            _str(data.get("bidder_company")),
            _str(data.get("bidder_description")),
            llm_hint=_str(data.get("acquirer_type")),
        ),
        seller_company=_str(data.get("seller_company")),
        seller_description=_str(data.get("seller_description")),
        seller_sector=_str(data.get("seller_sector")),
        seller_country=_str(data.get("seller_country")),
        currency=_str(data.get("currency")),
        enterprise_value_m=_num(data.get("enterprise_value_m")),
        reported_y1_date=_parse_date(data.get("reported_y1_date")),
        reported_revenue_m_y1=_num(data.get("reported_revenue_m_y1")),
        reported_ebitda_m_y1=_num(data.get("reported_ebitda_m_y1")),
        reported_ebit_m_y1=_num(data.get("reported_ebit_m_y1")),
        reported_revenue_multiple_y1=_num(data.get("reported_revenue_multiple_y1")),
        reported_ebit_multiple_y1=_num(data.get("reported_ebit_multiple_y1")),
        reported_ebitda_multiple_y1=_num(data.get("reported_ebitda_multiple_y1")),
        deal_description=_str(data.get("deal_description")),
        deal_value_gbp_m=_num(data.get("deal_value_gbp_m")),
        # Lean-schema additions — populated deterministically, NOT from LLM.
        strategic_commentary="",
        source=source_url,
        deal_id=build_deal_id(announced_date, target_company),
        # Provenance (unchanged).
        source_url=source_url,
        source_excerpt=source_excerpt,
    )


def _str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_date(v: Any) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str) and v.strip():
        s = v.strip()
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def looks_like_ma_announcement(title: str, snippet: str) -> bool:
    """Cheap pre-filter: does this news item look like an M&A announcement?
    Used by the sector-news -> deal-tracker auto-feed to avoid running the
    full extraction on irrelevant articles.
    """
    blob = f"{title} {snippet}".lower()
    triggers = [
        "acquir", "merg", "buyout", "takeover", "bid for", "offer for",
        "sells ", "sold to", "stake in", "agreed to acquire", "agreed to buy",
        "lbo", "leveraged buyout", "ipo", "listing", "spac",
    ]
    return any(t in blob for t in triggers)
