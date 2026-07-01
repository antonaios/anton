"""Pure helpers for the CIMTriage crew (#32) — NO metagpt, NO pypdf imports.

Runs in the isolated crew venv (``<repo>\\crews\\.venv``, Python 3.11),
but imports ONLY the stdlib so the bridge-side test suite (Python 3.14, no
metagpt) can load it by path and unit-test every pure transform — the same
discipline ``_shared/boundary.py`` follows.

What lives here (everything the triage roles do that is NOT an LLM call):

  * :class:`PageIndex` — the crew's "in-memory index". The bridge extracts the
    CIM to page-tagged text (it owns ``pypdf``; the crew venv has no PDF lib —
    see the #32 build notes) and hands the pages in via ``CrewInput.args``; the
    Ingestor builds a :class:`PageIndex` and the four analyst roles
    ``retrieve()`` page-tagged passages from it (keyword-overlap scoring, no
    embeddings — the crew venv has no ``llama_index`` either, so the spec's
    ``metagpt.rag`` vector index is not buildable; this is the documented
    substitute).
  * finding converters — turn each analyst LLM's schema-constrained JSON reply
    into structured rows (``RedFlag``/``Opportunity``/``KeyMetric``/questions).
    The LLM narrates; CODE owns the structure, so a chatty model can never drop
    or reshape a finding silently.
  * :func:`render_memo` — deterministically renders the 1-page memo from the
    structured findings + the Summariser's narrative. The numbers in the tables
    are the ones the analysts extracted, verbatim — never re-derived
    ([no-llm-maths]).
  * path/entity helpers — slug + the per-deal relative output path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ════════════════════════════════════════════════════════════════════════════
# Page index — the crew's "in-memory index" (keyword-overlap retrieval)
# ════════════════════════════════════════════════════════════════════════════

# Bound the prompt the analysts see: never feed more than this many chars of any
# single page, nor more than this many retrieved pages. CIMs run 20-100 pages;
# qwen3:14b's context is finite and the whole document rarely fits, so each
# analyst retrieves only the pages its keyword set hits.
_MAX_PASSAGE_CHARS = 1800
_DEFAULT_TOP_K = 6
# A token may carry internal $ % . - (so "$1.2m", "co-invest", "ebitda" survive);
# edge punctuation is stripped on tokenisation so "revenue." matches "revenue".
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9$%.\-]*")


@dataclass(frozen=True)
class Page:
    """One page of the CIM: a 1-based page number + its extracted text."""

    page: int
    text: str


def normalise_pages(raw_pages: Iterable[Any]) -> list[Page]:
    """Coerce the bridge-supplied ``args["pages"]`` into ``Page`` objects.

    Accepts the wire shape ``[{"page": 1, "text": "..."}, ...]``. Tolerant:
    a missing/garbage page number falls back to positional order (1-based);
    non-dict / empty-text entries are dropped. Never raises — a malformed page
    must not crash ingestion, only shrink the index."""
    out: list[Page] = []
    for i, p in enumerate(raw_pages or [], start=1):
        if not isinstance(p, dict):
            continue
        text = str(p.get("text") or "").strip()
        if not text:
            continue
        num = p.get("page")
        try:
            page_no = int(num)
            if page_no <= 0:
                page_no = i
        except (TypeError, ValueError):
            page_no = i
        out.append(Page(page=page_no, text=text))
    return out


_EDGE_PUNCT = ".,;:!?()[]{}\"'`"


def _tokenise(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD_RE.findall(text):
        t = w.lower().strip(_EDGE_PUNCT)
        if t:
            out.append(t)
    return out


class PageIndex:
    """Tiny keyword-overlap index over the CIM's pages.

    Deliberately NOT a vector store: the crew venv has no ``llama_index`` /
    embedding model, and a CIM triage is keyword-shaped (an analyst hunts for
    "customer concentration", "related party", "EBITDA"). ``retrieve`` scores
    each page by how often the query keywords appear and returns the top-k as
    page-tagged passages the analyst LLM can cite by number."""

    def __init__(self, pages: list[Page]):
        self.pages = pages
        # Pre-tokenise once; analysts each retrieve against the same index.
        self._tokens: list[list[str]] = [_tokenise(p.text) for p in pages]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def retrieve(
        self,
        query_terms: Iterable[str],
        *,
        top_k: int = _DEFAULT_TOP_K,
        max_passage_chars: int = _MAX_PASSAGE_CHARS,
    ) -> list[Page]:
        """Top-k pages by keyword-overlap score, in descending relevance.

        A query term may be a phrase ("related party"); it is matched both as a
        whole substring (weighted heavily) and by its individual word hits, so
        multi-word risk cues score above incidental single-word matches. Ties
        break toward earlier pages (cover/summary pages tend to lead). Returns
        page-tagged passages truncated to ``max_passage_chars``."""
        terms = [t.strip().lower() for t in query_terms if t and t.strip()]
        if not terms or not self.pages:
            return []
        scored: list[tuple[float, int, Page]] = []
        for idx, page in enumerate(self.pages):
            toks = self._tokens[idx]
            tokset = set(toks)
            hay = page.text.lower()
            score = 0.0
            for term in terms:
                if " " in term:
                    # Phrase: strong signal when the whole phrase appears, plus
                    # a weak signal per constituent word so partial hits count.
                    score += 5.0 * hay.count(term)
                    score += sum(0.5 for w in term.split() if w in tokset)
                else:
                    score += float(toks.count(term))
            if score > 0:
                scored.append((score, -idx, page))  # -idx → earlier page wins ties
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [
            Page(page=p.page, text=p.text[:max_passage_chars])
            for _, _, p in scored[: max(1, top_k)]
        ]


def format_passages(passages: list[Page]) -> str:
    """Render retrieved pages as a page-tagged block for an analyst prompt."""
    if not passages:
        return "(no relevant pages found)"
    return "\n\n".join(f"[page {p.page}]\n{p.text}" for p in passages)


# Per-analyst keyword sets (spec §3 role definitions). Kept here, not in the
# crew module, so the retrieval contract is unit-testable without metagpt.
RED_FLAG_TERMS = [
    "customer concentration", "top customer", "largest customer", "reliance",
    "leverage", "net debt", "covenant", "gearing", "indebtedness",
    "declining margin", "margin compression", "gross margin", "ebitda margin",
    "related party", "related-party", "affiliate transaction",
    "governance", "board", "control weakness", "litigation", "contingent",
    "audit qualification", "qualified opinion", "going concern", "restatement",
]
OPPORTUNITY_TERMS = [
    "total addressable market", "tam", "market size", "growth segment",
    "cagr", "growth rate", "expansion", "new product", "product line",
    "acquisition", "recent investment", "capex", "geographic", "new market",
    "cross-sell", "upsell", "pipeline", "backlog",
]
KEY_METRIC_TERMS = [
    "revenue", "turnover", "ebitda", "ebit", "gross profit", "net income",
    "margin", "growth", "customers", "customer count", "arr", "recurring",
    "geographies", "countries", "headcount", "employees",
]
QUESTION_TERMS = (
    RED_FLAG_TERMS[:6] + KEY_METRIC_TERMS[:6] + OPPORTUNITY_TERMS[:6]
)


# ════════════════════════════════════════════════════════════════════════════
# Finding converters — schema-constrained JSON → structured rows
# ════════════════════════════════════════════════════════════════════════════

_SEVERITIES = ("high", "med", "low")
_SEV_RANK = {"high": 0, "med": 1, "low": 2}
_SEV_ALIASES = {
    "high": "high", "hi": "high", "critical": "high", "severe": "high",
    "medium": "med", "med": "med", "moderate": "med", "mid": "med",
    "low": "low", "minor": "low", "lo": "low",
}


@dataclass
class RedFlag:
    claim: str
    page: int | None
    severity: str  # high | med | low


@dataclass
class Opportunity:
    claim: str
    page: int | None


@dataclass
class KeyMetric:
    metric: str
    value: str
    page: int | None


# A "nothing found" answer the model emits as a row instead of the requested
# NONE sentinel — must NOT render as a real finding ("1 low red flag" on a clean
# CIM). Filtered from red-flag + opportunity claims (NOT from key-metric values,
# where "n/a" is a legitimate "not disclosed" datum).
# Conservative on purpose: only the absence-OF-FINDINGS phrasings, so a REAL
# flag that happens to start with "No" ("No succession plan", "No audited
# accounts for FY23") is kept — the false-negative (a missed sentinel) is a
# cosmetic extra row; the false-positive (a dropped real flag) loses signal.
_NEGATIVE_SENTINEL_RE = re.compile(
    r"^\s*("
    r"none|n/?a|nil"
    r"|no\s+(?:material\s+|clear\s+|significant\s+|apparent\s+|obvious\s+|notable\s+|major\s+|specific\s+)?"
    r"(?:red\s+flags?|opportunit\w*|concerns?|issues?|findings?|risks?)\b"
    r"|(?:none|nothing)\s+(?:identified|found|evident|noted|of\s+note|to\s+report)\b"
    r"|not\s+(?:applicable|disclosed|identified|found|evident)\b"
    r")",
    re.IGNORECASE,
)


def _is_negative_sentinel(claim: str) -> bool:
    """True when a finding claim is really a 'nothing found' answer the model
    emitted as a row instead of the requested NONE sentinel."""
    return bool(_NEGATIVE_SENTINEL_RE.match(claim or ""))


def _parse_page(token: str) -> int | None:
    """Pull a page number out of a cell like ``p.12`` / ``page 12`` / ``12`` /
    ``?``. Returns ``None`` when the model couldn't cite one."""
    if token is None:
        return None
    m = re.search(r"\d+", str(token))
    if not m:
        return None
    try:
        n = int(m.group())
    except ValueError:
        return None
    return n if n > 0 else None


def _coerce_page(value: Any) -> int | None:
    """Coerce a schema-supplied page (``integer``|``null``, or defensively a
    string like ``p.12`` / ``?``) to a positive int or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    return _parse_page(str(value))


# ── JSON output schemas (the Ollama `format` grammar-constraint) ──────────────
# qwen3:14b IGNORES a free-text "output pipe rows" / "output JSON" instruction on
# a dense CIM (smoke 2026-06-15 — it writes a markdown analysis, 0 rows parse).
# Constraining the decode to these schemas FORCES well-formed output; the analyst
# roles pass the matching schema to ``ollama_config.ollama_structured_chat``.

RED_FLAGS_SCHEMA = {
    "type": "object",
    "properties": {"red_flags": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["high", "med", "low"]},
            "page": {"type": ["integer", "null"]},
            "claim": {"type": "string"},
        },
        "required": ["severity", "claim"],
    }}},
    "required": ["red_flags"],
}

OPPORTUNITIES_SCHEMA = {
    "type": "object",
    "properties": {"opportunities": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "page": {"type": ["integer", "null"]},
            "claim": {"type": "string"},
        },
        "required": ["claim"],
    }}},
    "required": ["opportunities"],
}

KEY_METRICS_SCHEMA = {
    "type": "object",
    "properties": {"metrics": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "value": {"type": "string"},
            "page": {"type": ["integer", "null"]},
        },
        "required": ["metric", "value"],
    }}},
    "required": ["metrics"],
}

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"],
}


def red_flags_from_json(data: dict) -> list[RedFlag]:
    """Build sorted ``RedFlag`` rows from the schema-constrained model object
    (``{"red_flags": [{"severity","page","claim"}]}``). The schema guarantees the
    shape; this still drops blank / "no red flags" claims (the model may emit an
    absence-of-findings sentence even under constraint) and normalises severity
    (unknown → ``med``). CODE owns the structure."""
    flags: list[RedFlag] = []
    for raw in (data or {}).get("red_flags") or []:
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim") or "").strip()
        if not claim or _is_negative_sentinel(claim):
            continue
        sev = _SEV_ALIASES.get(str(raw.get("severity") or "").strip().lower(), "med")
        flags.append(RedFlag(claim=claim, page=_coerce_page(raw.get("page")), severity=sev))
    return sort_red_flags(flags)


def sort_red_flags(flags: list[RedFlag]) -> list[RedFlag]:
    """High → med → low; within a severity, cited pages before un-cited, by
    page order. Stable + deterministic for the rendered table."""
    return sorted(
        flags,
        key=lambda f: (_SEV_RANK.get(f.severity, 9),
                       f.page if f.page is not None else 10**9),
    )


def opportunities_from_json(data: dict) -> list[Opportunity]:
    """Build ``Opportunity`` rows from the schema-constrained object
    (``{"opportunities": [{"page","claim"}]}``); drops blank / "none" claims."""
    out: list[Opportunity] = []
    for raw in (data or {}).get("opportunities") or []:
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim") or "").strip()
        if not claim or _is_negative_sentinel(claim):
            continue
        out.append(Opportunity(claim=claim, page=_coerce_page(raw.get("page"))))
    return out


def key_metrics_from_json(data: dict) -> list[KeyMetric]:
    """Build ``KeyMetric`` rows from the schema-constrained object
    (``{"metrics": [{"metric","value","page"}]}``). Values are kept VERBATIM as
    the model extracted them — never reformatted or recomputed ([no-llm-maths]);
    a missing value becomes ``n/a`` (a legitimate "not disclosed" datum)."""
    out: list[KeyMetric] = []
    for raw in (data or {}).get("metrics") or []:
        if not isinstance(raw, dict):
            continue
        metric = str(raw.get("metric") or "").strip()
        if not metric:
            continue
        value = str(raw.get("value") or "").strip() or "n/a"
        out.append(KeyMetric(metric=metric, value=value, page=_coerce_page(raw.get("page"))))
    return out


def questions_from_json(data: dict, *, max_q: int = 15) -> list[str]:
    """Build the DD-questions list from the schema-constrained object
    (``{"questions": ["..."]}``). De-duplicated, order-preserving, clamped to
    ``max_q``; drops too-short fragments. The schema guarantees an array of
    strings but NOT their content, so a leading list marker the model may emit
    *inside* a string is stripped — but ONLY the UNAMBIGUOUS ones: a numbered
    marker (``"1. "``, ``"2) "``, ``"(3) "``) or an asterisk bullet (``"* "``),
    and ONLY when a letter follows. A leading ``-`` is NEVER stripped: it is
    sign-ambiguous (bullet vs. minus), so stripping it could flip a reported
    figure's sign (``"- 5% ..."``, ``"- USD 5m ..."``). The render adds its own
    ``"N. "`` numbering, so a surviving marker is at worst a cosmetic
    double-bullet, never a wrong number (codex review 2026-06-15; FOLLOW-UP:
    monitor how often the model emits dash bullets). CODE owns the shape. A
    trailing ``?`` is NOT required: under the grammar constraint every item is a
    question by construction, and a sharp DD ask may legitimately be phrased
    without one."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (data or {}).get("questions") or []:
        q = str(raw or "").strip()
        # Strip an UNAMBIGUOUS leading list marker — a numbered marker ("1. ",
        # "2) ", "(3) ") or an asterisk bullet ("* ") — and ONLY when a (unicode)
        # LETTER follows, so a value lead ("5% ...", "$5m ...") keeps its marker
        # rather than being mauled. A leading "-" is NEVER matched: it is
        # sign-ambiguous (bullet vs. minus) and stripping it could flip a figure's
        # sign ("- 5% ..." -> "5% ..."). The render adds its own "N. " numbering,
        # so a surviving marker is at worst a cosmetic double-bullet, never a
        # wrong number (operator decision + codex review 2026-06-15).
        q = re.sub(r"^\s*(?:\*|\(?\d{1,2}[.)])\s+(?=[^\W\d_])", "", q).strip()
        if len(q) < 8:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= max_q:
            break
    return out


# ════════════════════════════════════════════════════════════════════════════
# Entity + output path
# ════════════════════════════════════════════════════════════════════════════

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug_entity(name: str) -> str:
    """Filesystem-safe entity folder name. Plain dashed slug, no "Project "
    prefix (per [[workspace-write-policy]] §2a folder convention). Empty →
    ``unknown-entity`` so a path can always be built."""
    s = _SLUG_RE.sub("-", str(name or "").strip()).strip("-")
    return s or "unknown-entity"


# Cover-page noise that is never the deal/company name.
_ENTITY_STOPWORDS = {
    "confidential", "information", "memorandum", "private", "draft", "strictly",
    "project", "teaser", "presentation", "overview", "introduction", "contents",
    "cim", "for", "discussion", "purposes", "only", "the", "and",
}


def infer_entity(pages: list[Page], *, explicit: str | None = None,
                 fallback: str | None = None) -> str:
    """Best-effort deal/entity name for the output folder.

    Precedence: an explicit operator-supplied name → a heuristic read of the
    cover page → the caller's fallback (the PDF filename stem) → ``"unknown
    entity"``. The heuristic is deliberately conservative: CIM covers are noisy
    ("STRICTLY CONFIDENTIAL — Project Atlas"), so a wrong guess should defer to
    the fallback rather than invent. The bridge re-sanitises whatever this
    returns before it touches the filesystem."""
    if explicit and explicit.strip():
        return explicit.strip()
    guess = _entity_from_cover(pages)
    if guess:
        return guess
    if fallback and fallback.strip():
        return fallback.strip()
    return "unknown entity"


def _entity_from_cover(pages: list[Page]) -> str | None:
    """Scan the first couple of pages for a plausible name line: short, mostly
    title-/upper-case, not pure boilerplate. Returns ``None`` when nothing
    clears the bar (caller falls back)."""
    for page in pages[:2]:
        for raw in page.text.splitlines():
            line = raw.strip(" \t-—•*#")
            if not (3 <= len(line) <= 60):
                continue
            words = line.split()
            if not (1 <= len(words) <= 6):
                continue
            alpha_words = [w for w in words if any(c.isalpha() for c in w)]
            if not alpha_words:
                continue
            # Drop lines that are mostly boilerplate stopwords.
            meaningful = [w for w in alpha_words
                          if w.lower().strip(".,") not in _ENTITY_STOPWORDS]
            if not meaningful:
                continue
            # Require title-case / capitalised signal (a name, not a sentence).
            capish = sum(1 for w in alpha_words if w[:1].isupper())
            if capish >= max(1, len(alpha_words) - 1):
                return line
    return None


def triage_relative_path(entity: str, date_iso: str, run_id: str) -> str:
    """``<entity-slug>/triage-<YYYY-MM-DD>-<run-id>.md`` — relative to the BD
    write root the bridge anchors it under ([[workspace-write-policy]] §2a:
    ``<workspace-root>\\2. Business development\\<entity>\\``). POSIX
    separators; the bridge confines + policy-checks it before writing."""
    safe_run = _SLUG_RE.sub("-", str(run_id or "")).strip("-") or "run"
    return f"{slug_entity(entity)}/triage-{date_iso}-{safe_run}.md"


# ════════════════════════════════════════════════════════════════════════════
# Memo rendering — structure owned by code, prose by the Summariser LLM
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class TriageFindings:
    """The four analysts' structured output, assembled by the crew before the
    Summariser narrates. The renderer reads this — NOT the LLM's free text — so
    the tables are authoritative."""

    red_flags: list[RedFlag] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    key_metrics: list[KeyMetric] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def severity_counts(self) -> dict[str, int]:
        counts = {"high": 0, "med": 0, "low": 0}
        for f in self.red_flags:
            if f.severity in counts:
                counts[f.severity] += 1
        return counts


def _md_escape_cell(text: str) -> str:
    """Keep a table cell on one row: escape pipes, collapse newlines."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _page_label(page: int | None) -> str:
    return f"p.{page}" if page else "—"


def render_memo(
    *,
    entity: str,
    date_iso: str,
    run_id: str,
    sensitivity: str,
    page_count: int,
    findings: TriageFindings,
    narrative: str,
) -> str:
    """Render the full 1-page triage memo (frontmatter + narrative + the four
    structured sections). Deterministic given its inputs — unit-tested."""
    counts = findings.severity_counts()
    fm_lines = [
        "---",
        "type: cim-triage",
        f"entity: {entity}",
        f"date: {date_iso}",
        f"run_id: {run_id}",
        f"sensitivity: {sensitivity}",
        f"pages: {page_count}",
        f"red_flags_high: {counts['high']}",
        f"red_flags_med: {counts['med']}",
        f"red_flags_low: {counts['low']}",
        f"opportunities: {len(findings.opportunities)}",
        f"questions: {len(findings.questions)}",
        "generated_by: crew.triage (autonomous, local Ollama qwen3:14b + qwen3:8b)",
        "---",
        "",
    ]
    body: list[str] = [f"# CIM Triage — {entity}", ""]
    body.append(
        f"*Autonomous `/triage` (CIMTriage crew, local Ollama `qwen3:14b`/`8b`). "
        f"{date_iso} · run `{run_id}` · {page_count} page(s) ingested. "
        f"Operator review required before any reliance.*"
    )
    body.append("")

    body.append("## Summary")
    body.append("")
    body.append((narrative or "").strip() or "_(no summary produced)_")
    body.append("")

    # Red flags
    high, med, low = counts["high"], counts["med"], counts["low"]
    body.append(
        f"## Red flags ({len(findings.red_flags)} — {high} high / {med} med / {low} low)"
    )
    body.append("")
    if findings.red_flags:
        body.append("| Severity | Page | Claim |")
        body.append("| --- | --- | --- |")
        for f in findings.red_flags:
            body.append(
                f"| {f.severity} | {_page_label(f.page)} | {_md_escape_cell(f.claim)} |"
            )
    else:
        body.append("_None surfaced._")
    body.append("")

    # Opportunities
    body.append(f"## Opportunities ({len(findings.opportunities)})")
    body.append("")
    if findings.opportunities:
        for o in findings.opportunities:
            body.append(f"- ({_page_label(o.page)}) {_md_escape_cell(o.claim)}")
    else:
        body.append("_None surfaced._")
    body.append("")

    # Key metrics
    body.append("## Key metrics")
    body.append("")
    if findings.key_metrics:
        body.append("| Metric | Value (as stated) | Page |")
        body.append("| --- | --- | --- |")
        for m in findings.key_metrics:
            body.append(
                f"| {_md_escape_cell(m.metric)} | {_md_escape_cell(m.value)} "
                f"| {_page_label(m.page)} |"
            )
    else:
        body.append("_None extracted._")
    body.append("")

    # Questions
    body.append(f"## Questions for management ({len(findings.questions)})")
    body.append("")
    if findings.questions:
        for i, q in enumerate(findings.questions, start=1):
            body.append(f"{i}. {q}")
    else:
        body.append("_None generated._")
    body.append("")

    body.append("---")
    body.append(
        "*Figures are extracted **as stated** in the source document; no figure "
        "was computed or estimated by the model ([no-llm-maths]). This crew ran "
        "entirely on the local Ollama lane — no CIM content left this machine "
        "([no-mnpi-to-cloud]).*"
    )
    body.append("")
    return "\n".join(fm_lines) + "\n".join(body) + "\n"


__all__ = [
    "Page", "PageIndex", "normalise_pages", "format_passages",
    "RED_FLAG_TERMS", "OPPORTUNITY_TERMS", "KEY_METRIC_TERMS", "QUESTION_TERMS",
    "RED_FLAGS_SCHEMA", "OPPORTUNITIES_SCHEMA", "KEY_METRICS_SCHEMA", "QUESTIONS_SCHEMA",
    "RedFlag", "Opportunity", "KeyMetric",
    "red_flags_from_json", "sort_red_flags", "opportunities_from_json",
    "key_metrics_from_json", "questions_from_json",
    "slug_entity", "infer_entity", "triage_relative_path",
    "TriageFindings", "render_memo",
]
