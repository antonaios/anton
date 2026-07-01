"""Step 4 — extract structured earnings fields from a results announcement.

Local Ollama (qwen3:14b) with JSON-mode, mirroring
:mod:`routines.dealtracker.extract`. Bridge-phase sensitivity rule
(#no-mnpi-to-cloud — was cited as §5.4):
extraction stays LOCAL — the announcement is public, but the WRITE target is the
operator-confidential vault, so the whole loop runs on the local model. No MNPI
to cloud.

Returns a partially-populated :class:`ExtractedEarnings`. Per CLAUDE.md §5.1 the
model never does arithmetic the source didn't state: margins/multiples come back
``None`` unless the announcement gives them, rather than being LLM-computed. The
deterministic compare step (:mod:`routines.earnings.compare`) is where every
delta vs consensus / prior period is calculated in Python.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from routines.earnings.report import DivisionLine, ExtractedEarnings, KPILine
from routines.shared.ollama_client import OllamaClient, parse_json_response

log = logging.getLogger(__name__)


DEFAULT_MODEL = "qwen3:14b"

# Keep input bounded — an RNS results announcement + tables can be long, and
# qwen3:14b quality drops on noise. 12k chars covers a typical results release
# headline + KPIs + outlook without the full notes-to-accounts tail.
_MAX_CHARS = 12000


SYSTEM_PROMPT = """\
You extract structured fields from a public company's quarterly / interim / full-year
results announcement (an RNS, press release, or investor-relations page).

Rules:
- company_name + ticker: the ISSUER this announcement is FOR — the company whose
  results these are (e.g. "Whitbread plc", "WTB.L"). Take them verbatim from the
  announcement header / RNS issuer line. This is used to confirm we fetched the
  right company; if you are unsure, return your best read rather than null.
- If a field is not stated in the source, return null. Do NOT infer, estimate, or
  compute a figure the announcement did not state explicitly. This is critical:
  a fabricated number is worse than a missing one.
- Do NOT compute margins or growth rates yourself. Only populate ebitda_margin /
  revenue_yoy if the announcement states them explicitly.
- Do NOT do unit arithmetic yourself, and NEVER drop the unit. Every monetary
  figure MUST carry its unit suffix EXACTLY as the announcement states it —
  "£142.0m" → "142m", "£1.4bn" → "1.4bn", "£950k" → "950k". A bare number with no
  unit (e.g. just "1.4") is REJECTED by the system as ambiguous (it can't tell
  millions from billions), so the figure would be LOST. The system normalises
  every figure to millions deterministically; you must NOT multiply billions out
  to millions yourself.
- ebitda_margin / revenue_yoy: report the percentage EXACTLY as the announcement
  states it, KEEPING the percent sign ("19.7%", "+8%"). Do NOT convert it to a
  fraction yourself — the system divides by 100 deterministically. Keep the sign.
  A bare number with NO "%" (e.g. "8") is ambiguous (8% or 800%?) and is DROPPED
  by the system, so the figure would be lost — ALWAYS include the "%".
- currency: the ISO-ish reporting currency code for the headline figures
  (GBP / USD / EUR / …). If mixed or unclear, return "".
- reported_date: the date the results were published/announced (ISO YYYY-MM-DD).
- fiscal_year + fiscal_period: the period these results COVER. fiscal_period is one
  of "Q1","Q2","Q3","Q4","H1","H2","9M","FY". fiscal_year is the 4-digit fiscal year.
- guidance: a one-sentence paraphrase of the outlook/guidance statement, or "".
- guidance_change: "reiterated" | "raised" | "lowered" | "" — only if the
  announcement makes the direction explicit (e.g. "raises FY guidance"). Else "".
- consensus_*: ONLY populate if the announcement itself references analyst
  consensus ("ahead of consensus of £139m"). Otherwise null.
- kpis: the operating KPIs the company leads on (like-for-like sales, site count,
  occupancy, RevPAR, ARPU, churn, etc.). Preserve the announcement's own phrasing
  in value (e.g. "+4.2%", "187"). Include consensus per-KPI only if stated.
- divisions: revenue split by segment/division if the announcement breaks it out.
- commentary: 2-4 sentences paraphrasing management's commentary on the results
  and outlook. Paraphrase; do not invent quotes.
- next_reporting_date: ISO date of the next scheduled update if the announcement
  states one (e.g. "next trading update: 15 July 2026"), else null.

Reply with a single JSON object only. No markdown fences, no preamble.
"""


SCHEMA_HINT = """\
Return a JSON object with this exact shape (use null for unknown fields). Monetary
figures MUST keep a unit suffix ("142m" / "1.4bn" / "950k") — a bare unit-less
number is rejected as ambiguous. The system normalises to millions. Margins/growth
are reported as the stated PERCENT STRING ("19.7%"); the system divides by 100.
{
  "company_name": "the issuer these results are for, e.g. Whitbread plc",
  "ticker": "the issuer ticker, e.g. WTB.L (or empty if not stated)",
  "fiscal_year": <4-digit year or null>,
  "fiscal_period": "Q1|Q2|Q3|Q4|H1|H2|9M|FY or null",
  "reported_date": "YYYY-MM-DD or null",
  "currency": "GBP|USD|EUR|... or empty",
  "revenue_m": <unit-suffixed string e.g. "142m" or "1.4bn" — never a bare number — or null>,
  "revenue_yoy": <"+8%" exactly as stated, only if stated, else null>,
  "ebitda_m": <unit-suffixed e.g. "28m" or "1.1bn" or null>,
  "ebitda_margin": <"19.7%" exactly as stated, only if stated, else null>,
  "ebit_m": <unit-suffixed e.g. "23m" or "0.9bn" or null>,
  "net_income_m": <unit-suffixed e.g. "19m" or null>,
  "eps": <per-share number e.g. 0.42 or null>,
  "guidance": "one-sentence paraphrase or empty",
  "guidance_change": "reiterated|raised|lowered or empty",
  "consensus_revenue_m": <unit-suffixed e.g. "139m" or "1.4bn" or null>,
  "consensus_ebitda_m": <unit-suffixed e.g. "28m" or null>,
  "consensus_eps": <per-share number or null>,
  "kpis": [ {"name": "...", "value": "...", "consensus": "... or empty", "unit": "... or empty"} ],
  "divisions": [ {"name": "...", "revenue_m": <unit-suffixed e.g. "42m" or null>, "revenue_yoy": <"+8%" as stated or null>, "note": "..."} ],
  "commentary": "2-4 sentence paraphrase",
  "next_reporting_date": "YYYY-MM-DD or null"
}
"""


def extract_earnings(
    *,
    text: str,
    source_url: str,
    client: OllamaClient,
    model: str = DEFAULT_MODEL,
) -> ExtractedEarnings:
    """Run local extraction over an announcement's markdown/text body.

    Raises ``ValueError`` on empty input; propagates ``OllamaError`` on transport
    / unrecoverable JSON parse failure (the caller treats that as "couldn't read
    the announcement this run" and lets the cron re-fire). Always returns an
    :class:`ExtractedEarnings` on success even if sparsely populated — the
    pipeline decides whether it has published by checking
    :meth:`ExtractedEarnings.has_headline_numbers`.
    """
    if not text or not text.strip():
        raise ValueError("empty text passed to extract_earnings")

    body = text.strip()
    if len(body) > _MAX_CHARS:
        body = body[:_MAX_CHARS] + "\n\n[... truncated ...]"

    prompt = f"{SCHEMA_HINT}\n\nAnnouncement:\n{body}"
    resp = client.chat(
        model=model, prompt=prompt, system=SYSTEM_PROMPT,
        json_mode=True, temperature=0.1, max_tokens=1800,
    )
    data = parse_json_response(resp.content)
    return normalise(data, source_url=source_url)


def normalise(data: dict[str, Any], *, source_url: str) -> ExtractedEarnings:
    """Build an :class:`ExtractedEarnings` from the model's JSON. Defensive about
    types (the local model occasionally returns numbers-as-strings or stray
    nesting).

    Raises :class:`AmbiguousMonetaryError` (a ``ValueError``) when a headline
    monetary field arrived as a BARE unit-less number — Python owns unit scaling
    (§5.1) and a bare ``1.4`` could be £1.4m or £1.4bn, so we refuse to guess
    rather than silently record a 1000x-wrong figure (#44 Codex SEV-1). The caller
    treats the raised extraction as ``extract_failed`` → audited ``partial``."""
    return ExtractedEarnings(
        company_name=_str(data.get("company_name")),
        ticker=_str(data.get("ticker")),
        fiscal_year=_int(data.get("fiscal_year")),
        fiscal_period=_period(data.get("fiscal_period")),
        reported_date=_parse_date(data.get("reported_date")),
        currency=_str(data.get("currency")).upper(),
        revenue_m=_money_m(data.get("revenue_m"), field="revenue_m"),
        revenue_yoy=_frac(data.get("revenue_yoy")),
        ebitda_m=_money_m(data.get("ebitda_m"), field="ebitda_m"),
        ebitda_margin=_frac(data.get("ebitda_margin")),
        ebit_m=_money_m(data.get("ebit_m"), field="ebit_m"),
        net_income_m=_money_m(data.get("net_income_m"), field="net_income_m"),
        eps=_num(data.get("eps")),
        guidance=_str(data.get("guidance")),
        guidance_change=_guidance_change(data.get("guidance_change")),
        consensus_revenue_m=_money_m_soft(data.get("consensus_revenue_m"), field="consensus_revenue_m"),
        consensus_ebitda_m=_money_m_soft(data.get("consensus_ebitda_m"), field="consensus_ebitda_m"),
        consensus_eps=_num(data.get("consensus_eps")),
        kpis=_kpis(data.get("kpis")),
        divisions=_divisions(data.get("divisions")),
        commentary=_str(data.get("commentary")),
        next_reporting_date=_parse_date(data.get("next_reporting_date")),
        source_url=source_url,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Coercion helpers
# ─────────────────────────────────────────────────────────────────────────────


_VALID_PERIODS = {"Q1", "Q2", "Q3", "Q4", "H1", "H2", "9M", "FY"}
_VALID_GUIDANCE = {"reiterated", "raised", "lowered"}


def _str(v: Any) -> str:
    return "" if v is None else str(v).strip()


class AmbiguousMonetaryError(ValueError):
    """A monetary field arrived as a BARE unit-less number. Python (not the model)
    owns unit scaling (§5.1), and a bare ``1.4`` is indistinguishable between
    £1.4m and £1.4bn — recording it as millions would risk a silent 1000x
    corruption. We refuse to guess and raise so the whole extraction is rejected
    (→ ``extract_failed`` → audited ``partial``) rather than capturing a wrong
    figure (#44 Codex SEV-1)."""


# Unit suffix → multiplier to MILLIONS. The system (NOT the model) owns this
# scaling per CLAUDE.md §5.1 — the model reports the figure with its stated unit,
# Python normalises deterministically. Order matters: longer suffixes first so
# "bn" is matched before "b".
_UNIT_MULTIPLIERS: tuple[tuple[str, float], ...] = (
    ("bln", 1000.0), ("bn", 1000.0), ("b", 1000.0),
    ("mln", 1.0), ("mn", 1.0), ("m", 1.0),
    ("k", 0.001),
)

# Spelled-out units accepted in the ``{amount, unit}`` object form.
_UNIT_WORDS: dict[str, float] = {
    "bn": 1000.0, "bln": 1000.0, "b": 1000.0, "billion": 1000.0, "billions": 1000.0,
    "m": 1.0, "mn": 1.0, "mln": 1.0, "mm": 1.0, "million": 1.0, "millions": 1.0,
    "k": 0.001, "thousand": 0.001, "thousands": 0.001,
}


def _clean_numeric(s: str) -> str:
    return s.replace(",", "").replace("£", "").replace("$", "").replace("€", "").replace("%", "").strip()


def _money_m(v: Any, *, field: str) -> Optional[float]:
    """Parse a MONETARY figure → millions, REQUIRING an explicit unit. Accepts a
    unit-suffixed string ("1.4bn" → 1400.0, "142m" → 142.0, "950k" → 0.95) or a
    ``{"amount": 1.4, "unit": "bn"}`` object. A BARE unit-less number (int/float,
    or a numeric string with no recognised suffix) raises
    :class:`AmbiguousMonetaryError` — Python owns scaling and we never guess
    millions-vs-billions (#44 Codex SEV-1). Returns None for a genuinely absent
    field (null / "") or non-numeric junk (unparseable, not ambiguous)."""
    if v is None or v == "":
        return None
    # {amount, unit} object form.
    if isinstance(v, dict):
        amount = v.get("amount", v.get("value"))
        if amount is None or amount == "":
            return None
        try:
            amt = float(_clean_numeric(str(amount)))
        except (TypeError, ValueError):
            return None
        mult = _UNIT_WORDS.get(str(v.get("unit") or "").strip().lower().rstrip("."))
        if mult is None:
            raise AmbiguousMonetaryError(f"{field}: {{amount, unit}} with no/unknown unit {v.get('unit')!r}")
        return amt * mult
    # A bare numeric (no unit) is ambiguous — reject.
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        raise AmbiguousMonetaryError(f"{field}: bare numeric {v!r} has no unit (need a unit suffix or {{amount, unit}})")
    s = _clean_numeric(str(v))
    if not s:
        return None
    low = s.lower()
    for suffix, m in _UNIT_MULTIPLIERS:
        if low.endswith(suffix) and len(low) > len(suffix):
            try:
                return float(s[: -len(suffix)].strip()) * m
            except (TypeError, ValueError):
                return None
    # A numeric string with NO unit suffix is ambiguous → reject; non-numeric junk
    # is just unparseable → None (never a fabricated figure).
    try:
        float(s)
    except (TypeError, ValueError):
        return None
    raise AmbiguousMonetaryError(f"{field}: bare numeric string {v!r} has no unit (need a unit suffix or {{amount, unit}})")


def _money_m_soft(v: Any, *, field: str) -> Optional[float]:
    """Like :func:`_money_m` but DROPS an ambiguous bare value to None instead of
    raising. For SECONDARY monetary fields (consensus baselines, division
    breakdowns) a missing unit must not abort the whole extraction the way a bare
    HEADLINE actual does — we just lose that one comparison/breakdown, never the
    valid capture of the headline numbers (#44 Codex re-review SEV-3)."""
    try:
        return _money_m(v, field=field)
    except AmbiguousMonetaryError:
        log.info("earnings extract: %s had no unit — dropping the figure (capture continues)", field)
        return None


def _num(v: Any) -> Optional[float]:
    """Lenient numeric parse for NON-monetary, already-absolute fields (eps,
    consensus_eps, fiscal_year). A bare number is taken as-is — these are
    per-share / year values that carry no millions/billions unit, so the
    monetary ambiguity (#44 Codex SEV-1) does not apply. Returns None on anything
    unparseable — never a guess."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = _clean_numeric(str(v))
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _frac(v: Any) -> Optional[float]:
    """Parse a FRACTION (margin OR growth rate) from an EXPLICIT percent string
    only ("19.7%" → 0.197, "+8%" → 0.08, "+800%" → 8.0). A BARE number — with no
    "%" — is AMBIGUOUS in BOTH directions: ``1.2`` could be the fraction 1.2
    (=120%) or a dropped-"%" 1.2% (=0.012); ``8`` could be +8% or +800%. There is
    no safe bare range, so we DROP every unit-less value to None rather than guess
    (consistent with the bare-monetary rule — Python never invents a figure the
    source didn't unambiguously state, and the prompt requires the "%"). This
    kills the false +800% / -1800% material alerts AND the 1.2%→120% class of
    silent corruption (#44 Codex SEV-1/2). Returns None on unparseable input."""
    if v is None or v == "":
        return None
    s = str(v).replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip()
    if not s.endswith("%"):
        return None   # no explicit "%" → ambiguous fraction-vs-dropped-percent → drop
    s = s[:-1].strip()
    if not s:
        return None
    try:
        return float(s) / 100.0
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return None if n is None else int(n)


def _period(v: Any) -> str:
    s = _str(v).upper().replace(" ", "")
    return s if s in _VALID_PERIODS else ""


def _guidance_change(v: Any) -> str:
    s = _str(v).lower()
    return s if s in _VALID_GUIDANCE else ""


def _parse_date(v: Any) -> Optional[date]:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str) and v.strip():
        try:
            return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _kpis(v: Any) -> list[KPILine]:
    if not isinstance(v, list):
        return []
    out: list[KPILine] = []
    for row in v:
        if not isinstance(row, dict):
            continue
        name = _str(row.get("name"))
        if not name:
            continue
        out.append(KPILine(
            name=name,
            value=_str(row.get("value")),
            consensus=_str(row.get("consensus")),
            unit=_str(row.get("unit")),
        ))
    return out


def _divisions(v: Any) -> list[DivisionLine]:
    if not isinstance(v, list):
        return []
    out: list[DivisionLine] = []
    for row in v:
        if not isinstance(row, dict):
            continue
        name = _str(row.get("name"))
        if not name:
            continue
        # Division revenue is a secondary breakdown — a bare unit-less value drops
        # to None (the line keeps its name/note) rather than failing the whole
        # capture the way a bare HEADLINE figure does (#44 Codex SEV-1).
        out.append(DivisionLine(
            name=name,
            revenue_m=_money_m_soft(row.get("revenue_m"), field=f"division[{name}].revenue_m"),
            revenue_yoy=_frac(row.get("revenue_yoy")),
            note=_str(row.get("note")),
        ))
    return out


__all__ = ["DEFAULT_MODEL", "AmbiguousMonetaryError", "extract_earnings", "normalise"]
