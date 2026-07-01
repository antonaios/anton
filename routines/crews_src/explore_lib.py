"""Pure logic for the ``/explore`` DeepDive crew (#33) — STDLIB ONLY.

Everything the explore roles do that ISN'T a MetaGPT/LLM call lives here:
vault filesystem scanning, frontmatter ticker/sector resolution, the
bridge HTTP calls (urllib — no third-party dep), the synthetic fan-in
pack/unpack, and the per-role prompt builders.

WHY A SEPARATE MODULE: ``explore_crew.py`` imports ``metagpt`` at module top
(it must, to subclass Role/Action), so it can only be exercised by the live
Ollama smoke. This module imports **only the standard library**, so the
bridge venv's pytest suite can load it by file path (the same trick the
boundary parity test uses) and unit-test the real logic — including the
load-bearing rule that the FinancialAnalyst returns ``null`` rather than
inventing numbers when no public ticker resolves.

Runs in the crew venv (Python 3.11) at runtime; loadable anywhere for tests.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Generic vault-scan primitives now live in the SHARED module so /debate (#36)
# can reuse them (integration: operator decision 2). Re-exported below so
# explore_lib's public API is unchanged — explore_crew + its tests keep calling
# explore_lib.vault_root / scan_vault_for_target / split_frontmatter / etc.
from _shared.vault_scan import (
    SKIP_DIRS,
    iter_vault_markdown,
    read_text,
    scan_vault_for_target,
    split_frontmatter,
    to_wikilink,
    vault_root,
)

# ════════════════════════════════════════════════════════════════════════════
# Environment / paths — resolved with env override, machine-default fallback.
# ════════════════════════════════════════════════════════════════════════════

# A token that may legitimately be sent to the markets tool (which ultimately
# reaches EXTERNAL data providers). Identical to markets.py::_TICKER_PATTERN —
# the egress gate: only a value matching this ever leaves the box.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")
# Uppercase words that look ticker-shaped but are exchange/structure labels.
_TICKER_DENYLIST = frozenset({
    "LSE", "NYSE", "NASDAQ", "AMEX", "OTC", "LON", "ADR", "ADS", "GDR",
    "PLC", "LTD", "INC", "CORP", "AG", "SA", "NV", "SE", "GMBH", "USD",
    "GBP", "EUR", "EU", "UK", "US",
})

_SECTOR_CONTEXT_CHARS = 6_000      # cap sector context fed to the LLM


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def bridge_base_url() -> str:
    """Local bridge base URL the FinancialAnalyst calls (no trailing slash).

    ``AGENTIC_API_HOST`` / ``AGENTIC_API_PORT`` mirror app.py's bind defaults
    (127.0.0.1:8765). FAIL-CLOSED TO LOOPBACK: a non-loopback host is ignored
    and forced back to 127.0.0.1 — the crew must only ever reach the
    same-machine bridge, so a stray ``AGENTIC_API_HOST=some.remote`` can never
    route crew traffic (even a public ticker) off the box (the bridge itself
    binds loopback-only; this is the matching egress side)."""
    raw = (os.environ.get("AGENTIC_API_HOST", "127.0.0.1").strip().lower()
           or "127.0.0.1")
    if raw not in _LOOPBACK_HOSTS:
        raw = "127.0.0.1"
    host = "[::1]" if raw == "::1" else "127.0.0.1"   # localhost → IPv4 loopback
    # Port must be purely numeric — a value like "8765@evil.com" would turn
    # "127.0.0.1:<port>" into a userinfo@host authority and send traffic
    # off-box; reject anything non-digit back to the default (codex SEV-2).
    port = os.environ.get("AGENTIC_API_PORT", "8765").strip()
    if not port.isdigit():
        port = "8765"
    return f"http://{host}:{port}"


def crew_sensitivity() -> str:
    """Tier the bridge resolved before launch (env-injected by the proxy)."""
    return os.environ.get("ANTON_CREW_SENSITIVITY", "unknown")


# ════════════════════════════════════════════════════════════════════════════
# Frontmatter-derived resolvers (explore-specific: ticker + sector). The generic
# read_text / split_frontmatter / to_wikilink primitives are imported from
# _shared.vault_scan above (and re-exported in __all__ for back-compat).
# ════════════════════════════════════════════════════════════════════════════


def extract_ticker(frontmatter: dict[str, str]) -> str | None:
    """First valid PUBLIC ticker from a ``ticker:`` frontmatter value.

    e.g. ``"VOD.L (LSE) · VOD (NASDAQ ADR)"`` → ``"VOD.L"``. Prefers an
    exchange-qualified (dotted) symbol, then the first pattern-valid,
    non-denylisted token. Returns ``None`` when nothing qualifies — the
    signal the FinancialAnalyst uses to return ``null`` instead of guessing.

    TRUST BOUNDARY: the ``ticker:`` frontmatter field is operator-designated
    PUBLIC metadata — the only thing eligible to reach the markets tool. We
    validate its SHAPE here (and again in :func:`is_valid_ticker` before any
    network call), mirroring the markets endpoint's own ``_TICKER_PATTERN``
    contract; we deliberately do not (cannot) distinguish a real US symbol
    from a same-shaped codename, so the field's public-ness is trusted exactly
    as the rest of the platform trusts it.
    """
    raw = frontmatter.get("ticker") or ""
    # Tokenise on anything that isn't part of a ticker glyph.
    tokens = re.split(r"[^A-Za-z0-9.^=-]+", raw)
    candidates = [
        t.upper() for t in tokens
        if t and _TICKER_PATTERN.fullmatch(t.upper())
        and t.upper() not in _TICKER_DENYLIST
        and any(c.isalpha() for c in t)        # exclude bare numbers/years
    ]
    if not candidates:
        return None
    for c in candidates:
        if "." in c:                            # prefer VOD.L over VOD
            return c
    return candidates[0]


def is_valid_ticker(symbol: str | None) -> bool:
    """True iff ``symbol`` is safe to send to the markets egress tool."""
    return bool(
        symbol
        and _TICKER_PATTERN.fullmatch(symbol)
        and symbol not in _TICKER_DENYLIST
        and any(c.isalpha() for c in symbol)
    )


def extract_sector_slug(frontmatter: dict[str, str]) -> str | None:
    """Sector slug from a ``sector: "[[Sectors/telecoms/_Index]]"`` value."""
    raw = frontmatter.get("sector") or ""
    m = re.search(r"\[\[\s*Sectors/([^/\]]+)", raw)
    if m:
        return m.group(1).strip()
    # Bare ``sector: telecoms`` fallback.
    bare = raw.strip().strip('"').strip("'")
    if bare and "[[" not in bare and "/" not in bare:
        return bare
    return None


# ════════════════════════════════════════════════════════════════════════════
# Vault scanning (explore-specific; the generic iter_vault_markdown / excerpt /
# scan_vault_for_target are imported from _shared.vault_scan above).
# ════════════════════════════════════════════════════════════════════════════


def find_company_note(root: Path, target: str) -> dict[str, Any] | None:
    """Best ``Companies/<name>.md`` match for ``target`` (substring, case-
    insensitive, either direction). Returns ``{path, wikilink, frontmatter}``
    or ``None``."""
    term_l = (target or "").strip().lower()
    if not term_l:
        return None
    companies = root / "Companies"
    if not companies.is_dir():
        return None
    best: tuple[int, Path, dict[str, str]] | None = None
    for path in companies.glob("*.md"):
        stem_l = path.stem.lower()
        fm, _body = split_frontmatter(read_text(path))
        name_l = (fm.get("name") or "").lower()
        score = 0
        if term_l == stem_l or term_l == name_l:
            score = 3                                   # exact
        elif term_l in stem_l or term_l in name_l:
            score = 2                                   # target ⊂ note
        elif stem_l in term_l or (name_l and name_l in term_l):
            score = 1                                   # note ⊂ target
        if score and (best is None or score > best[0]):
            best = (score, path, fm)
    if best is None:
        return None
    _score, path, fm = best
    return {
        "path": path.as_posix(),
        "wikilink": to_wikilink(root, path),
        "frontmatter": fm,
    }


def resolve_sector_slug(root: Path, target: str) -> str | None:
    """If ``target`` is itself a sector, its slug. Matches ``Sectors/<slug>/``
    dirs and ``Sectors/<Name>.md`` root notes, case-insensitively."""
    term_l = (target or "").strip().lower()
    sectors = root / "Sectors"
    if not term_l or not sectors.is_dir():
        return None
    for child in sectors.iterdir():
        if child.is_dir() and child.name.lower() == term_l:
            return child.name
        if child.is_file() and child.suffix.lower() == ".md" \
                and child.stem.lower() == term_l:
            return child.stem
    return None


def read_sector_context(
    root: Path, slug: str, max_chars: int = _SECTOR_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Gather sector knowledge for ``slug``: the ``_Index`` + key detail files
    (+ any per-sector newsletters), bounded to ``max_chars``.

    Reads ``Sectors/<slug>/_Index.md`` and the detail files that carry
    positioning/risk signal, then ``Resources/Newsletters/<slug>/*.md`` if the
    operator has populated them (empty in the vault today — see the build
    summary's path-discrepancy note). Returns ``{slug, sources: [wikilink],
    context: str}``.
    """
    out_sources: list[str] = []
    chunks: list[str] = []
    sector_dir = root / "Sectors" / slug
    candidates: list[Path] = []
    if sector_dir.is_dir():
        for name in ("_Index.md", "Dynamics.md", "Competitive.md",
                     "Issues.md", "Regulatory.md", "Valuation.md", "Comps.md"):
            p = sector_dir / name
            if p.is_file():
                candidates.append(p)
    root_note = root / "Sectors" / f"{slug}.md"
    if root_note.is_file():
        candidates.append(root_note)
    news_dir = root / "Resources" / "Newsletters" / slug
    if news_dir.is_dir():
        candidates.extend(sorted(news_dir.glob("*.md"), reverse=True)[:3])

    budget = max_chars
    for p in candidates:
        if budget <= 0:
            break
        _fm, body = split_frontmatter(read_text(p))
        snippet = (body or "").strip()[:budget]
        if not snippet:
            continue
        out_sources.append(to_wikilink(root, p))
        chunks.append(f"### {to_wikilink(root, p)}\n{snippet}")
        budget -= len(snippet)
    return {
        "slug": slug,
        "sources": out_sources,
        "context": "\n\n".join(chunks),
    }


# ════════════════════════════════════════════════════════════════════════════
# Bridge HTTP (urllib — best-effort; never raises to the caller)
# ════════════════════════════════════════════════════════════════════════════


def _http_json(
    url: str, *, method: str, payload: dict[str, Any] | None, timeout: float,
) -> dict[str, Any] | None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback only
            raw = resp.read().decode("utf-8", errors="ignore")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def fetch_comps(
    base_url: str, ticker: str, *, peers_limit: int = 8, years: int = 5,
    timeout: float = 45.0,
) -> dict[str, Any] | None:
    """POST ``/api/workflows/comps`` — target + peers multiples. The ONLY value
    sent is the validated public ``ticker`` (never the target name)."""
    if not is_valid_ticker(ticker):
        return None
    return _http_json(
        f"{base_url}/api/workflows/comps",
        method="POST",
        payload={
            "symbol": ticker,
            "peers_limit": peers_limit,
            "years": years,
            "write_note": False,        # explore never writes — chat-only
        },
        timeout=timeout,
    )


def fetch_peers(
    base_url: str, ticker: str, *, limit: int = 8, timeout: float = 30.0,
) -> dict[str, Any] | None:
    """GET ``/api/markets/peers`` — sector/industry/peer profile."""
    if not is_valid_ticker(ticker):
        return None
    qs = urllib.parse.urlencode({"symbol": ticker, "limit": limit})
    return _http_json(
        f"{base_url}/api/markets/peers?{qs}",
        method="GET", payload=None, timeout=timeout,
    )


def has_market_data(
    comps: dict[str, Any] | None, peers: dict[str, Any] | None,
) -> bool:
    """True iff the tools returned USABLE data — a successful-but-empty payload
    (``{"rows": []}`` / ``{"peers": []}``) is NOT data and routes to the
    null/no-LLM branch (codex SEV-2). Requires a non-empty LIST, so a
    malformed non-list ``rows``/``peers`` is also treated as no-data (codex
    SEV-3) rather than reaching the renderer."""
    rows = (comps or {}).get("rows") if isinstance(comps, dict) else None
    plist = (peers or {}).get("peers") if isinstance(peers, dict) else None
    return (isinstance(rows, list) and len(rows) > 0) or \
           (isinstance(plist, list) and len(plist) > 0)


def _fmt_num(value: Any, suffix: str = "") -> str:
    """Format an engine figure for display VERBATIM (None → ``n/a``). Numbers
    print via Python's shortest round-trip repr — NO rounding, no %/×100 — so
    the figure the Synthesist and operator see is EXACTLY the engine's value
    (codex SEV-2: 2dp rounding would silently alter e.g. a 0.0749 yield)."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value}{suffix}"
    return str(value)


# Multiples shown for the target; (label, payload key, unit-suffix).
_FIN_FIELDS = (
    ("EV/EBITDA", "ev_ebitda", "x"),
    ("P/E", "pe", "x"),
    ("EBITDA margin", "ebitda_margin", ""),
    ("rev growth 5y CAGR", "revenue_growth_5y_cagr", ""),
    ("net debt/EBITDA", "net_debt_ebitda", "x"),
    ("div yield", "dividend_yield", ""),
)


def render_financial_summary(
    target: str, ticker: str,
    comps: dict[str, Any] | None, peers: dict[str, Any] | None,
) -> str:
    """DETERMINISTIC financial summary built in CODE from the engine payload —
    NO LLM touches these numbers (constitution: no-llm-maths / no-invented-
    sources, codex SEV-1). Every figure is copied verbatim from ``comps`` /
    ``peers``; a missing field renders ``n/a``, never an estimate."""
    comps = comps or {}
    # Normalise to a list of dict rows so a malformed non-list/non-dict payload
    # can never crash on indexing (codex SEV-3).
    rows = [r for r in (comps.get("rows") or []) if isinstance(r, dict)]
    tgt_sym = comps.get("target_symbol") or ticker
    provider = comps.get("provider") or "engine"
    parts: list[str] = []

    # Resolve the target by symbol; else fall back to the first row (the
    # /comps contract is target-first). Label the line with the SELECTED row's
    # OWN symbol — never tgt_sym — so a non-matching fallback can't be
    # mislabelled as the target (codex SEV-2).
    target_row = next(
        (r for r in rows if r.get("symbol") == tgt_sym),
        rows[0] if rows else None,
    )
    if target_row:
        sym = target_row.get("symbol") or tgt_sym
        name = target_row.get("name") or sym
        metrics = " · ".join(
            f"{lbl} {_fmt_num(target_row.get(key), suf)}"
            for lbl, key, suf in _FIN_FIELDS
        )
        parts.append(
            f"Trading multiples (source: engine /comps via {provider}; "
            f"figures verbatim, not model-generated):"
        )
        parts.append(f"- {name} ({sym}): {metrics}")
        peer_count = 0
        # Exclude the selected target row by IDENTITY (not symbol) so it never
        # also appears as a peer when its symbol differs from tgt_sym.
        for r in rows:
            if r is target_row:
                continue
            if peer_count >= 5:
                break
            pname = r.get("name") or r.get("symbol")
            parts.append(
                f"- peer {pname} ({r.get('symbol')}): "
                f"EV/EBITDA {_fmt_num(r.get('ev_ebitda'), 'x')} · "
                f"P/E {_fmt_num(r.get('pe'), 'x')}"
            )
            peer_count += 1

    plist = peers.get("peers") if isinstance(peers, dict) else None
    if isinstance(plist, list) and plist and isinstance(plist[0], dict):
        sect = plist[0].get("sector")
        ind = plist[0].get("industry")
        if sect or ind:
            parts.append(
                f"Peer-set classification (source: /markets/peers): "
                f"sector={sect or 'n/a'}, industry={ind or 'n/a'}"
            )
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════════════
# Synthetic fan-in: analyst envelopes + the DeepDiveReady pack
# ════════════════════════════════════════════════════════════════════════════

# Action class names the Coordinator watches; mapped to the canonical role key.
ANALYST_ACTION_ROLE = {
    "ScanVault": "vault",
    "AnalyzeFinancials": "financial",
    "AnalyzeIndustry": "industry",
}
REQUIRED_ANALYSTS = frozenset(ANALYST_ACTION_ROLE.values())


def pack_analyst(role_key: str, target: str, payload: dict[str, Any]) -> str:
    """One analyst's output as a JSON line (code-built, so always valid JSON —
    the LLM only fills the free-text fields inside ``payload``)."""
    return json.dumps({"role": role_key, "target": target, **payload})


def unpack_analyst(content: str) -> dict[str, Any]:
    """Parse a :func:`pack_analyst` line; ``{}`` on anything malformed."""
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def is_valid_analyst_payload(payload: Any, role_key: str) -> bool:
    """True iff ``payload`` is a non-empty analyst envelope that self-reports
    the EXPECTED role. The Coordinator uses this so a malformed/empty message
    (``unpack_analyst`` → ``{}``, or a role mislabel) does NOT count toward the
    fan-in — otherwise it could publish DeepDiveReady with missing data and
    return an OK memo instead of a structured error (codex SEV-2)."""
    return (
        isinstance(payload, dict)
        and bool(payload)
        and payload.get("role") == role_key
    )


def all_analysts_reported(role_keys: set[str]) -> bool:
    """The synthetic JOIN predicate — true once all three analysts emitted."""
    return REQUIRED_ANALYSTS.issubset(role_keys)


def pack_deepdive(target: str, collected: dict[str, dict[str, Any]]) -> str:
    """The Coordinator's DeepDiveReady payload: the three analyst outputs,
    keyed by role, with the target threaded through for the Synthesist."""
    return json.dumps({
        "target": target,
        "vault": collected.get("vault"),
        "financial": collected.get("financial"),
        "industry": collected.get("industry"),
    })


def unpack_deepdive(content: str) -> dict[str, Any]:
    """Parse the DeepDiveReady payload for the Synthesist."""
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


# ════════════════════════════════════════════════════════════════════════════
# Prompt builders (the LLM NARRATES already-gathered data; it never invents
# numbers or citations — every figure/cite is supplied in the prompt).
# ════════════════════════════════════════════════════════════════════════════


def vault_prompt(target: str, scan: dict[str, Any]) -> str:
    hits = scan.get("hits") or []
    if not hits:
        return (
            f"There are NO existing vault notes about '{target}'. In ONE "
            f"sentence, state plainly that the vault has nothing on '{target}' "
            f"yet. Do not invent facts or citations."
        )
    lines = "\n".join(
        f"- {h['wikilink']} ({h['where']}): {h['excerpt']}" for h in hits
    )
    return (
        f"You are a vault archaeologist. Below are the vault notes that mention "
        f"'{target}', each with a [[wikilink]] and an excerpt:\n\n{lines}\n\n"
        f"Summarise what the vault already knows about '{target}' in 4-7 "
        f"bullet points. CITE the relevant [[wikilink]] inline on each bullet, "
        f"using ONLY the wikilinks listed above — never invent a link or a "
        f"fact not present in the excerpts. Plain text bullets only."
    )


def industry_prompt(target: str, sector_ctx: dict[str, Any]) -> str:
    context = (sector_ctx or {}).get("context") or ""
    slug = (sector_ctx or {}).get("slug")
    if not context:
        return (
            f"There is no sector note in the vault for '{target}'. In ONE "
            f"sentence, state that no sector mapping exists yet. Do not invent."
        )
    return (
        f"You are an industry analyst. '{target}' maps to the vault sector "
        f"'{slug}'. Below is the sector knowledge on file:\n\n{context[:6000]}"
        f"\n\nIn 4-6 bullets, give the sector POSITIONING and the key RISKS "
        f"relevant to '{target}'. Cite the [[wikilink]] sources shown in the "
        f"headers above where relevant. Use only what's written above — do not "
        f"invent facts. Plain text bullets only."
    )


def synthesis_prompt(deepdive: dict[str, Any]) -> str:
    """The final memo prompt — feeds the three role outputs (with per-role
    attribution) to the Synthesist."""
    target = deepdive.get("target", "the target")

    def _section(label: str, obj: Any) -> str:
        if not obj:
            return f"## {label}\n(no contribution)"
        summary = obj.get("summary") if isinstance(obj, dict) else None
        return f"## {label}\n{summary or '(no summary)'}"

    vault = _section("VaultArchaeologist", deepdive.get("vault"))
    fin = _section("FinancialAnalyst", deepdive.get("financial"))
    ind = _section("IndustryAnalyst", deepdive.get("industry"))
    return (
        f"You are the synthesist for a DeepDive on '{target}'. Three analysts "
        f"reported:\n\n{vault}\n\n{fin}\n\n{ind}\n\n"
        f"Write ONE concise memo with these four sections, attributing claims "
        f"to the role that surfaced them and preserving their [[wikilinks]]:\n"
        f"  **What we know** — the established picture.\n"
        f"  **What's odd / interesting** — tensions, surprises, gaps.\n"
        f"  **Open questions** — what we still don't know.\n"
        f"  **Next actions** — concrete next steps.\n"
        f"Do not invent facts, numbers, or citations beyond what the analysts "
        f"provided. Keep any financial figures EXACTLY as written by the "
        f"FinancialAnalyst — never recompute, round, or restate them. Markdown, "
        f"no preamble."
    )


# ════════════════════════════════════════════════════════════════════════════
# Conclusion capture (#captures-to-vault-crews) — a one-line takeaway + the note
# it lands on. The headline is the crew's OWN structured conclusion (grammar-
# constrained via ollama_structured_chat, not re-derived at capture time); the
# target is resolved against the REAL vault (existing company note vs sector).
# ════════════════════════════════════════════════════════════════════════════

HEADLINE_SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}},
    "required": ["headline"],
}


def headline_prompt(target: str, memo: str) -> str:
    """One-line takeaway from the DeepDive memo — the crew's own conclusion."""
    return (
        f"Below is a DeepDive memo on '{target}'. In ONE sentence (max ~25 "
        f"words), state the single most decision-useful takeaway about "
        f"'{target}'. Use ONLY what the memo says — do not invent facts or "
        f"numbers.\n\nMEMO:\n{memo}"
    )


def resolve_capture_target(root: Path, target: str) -> tuple[str, str]:
    """Resolve the vault note a DeepDive conclusion lands on.

    ``Companies/<note>.md`` when ``target`` matches an EXISTING company note
    (lands on that note, not a near-miss new one); ``Topics/<slug>.md`` when it
    resolves to a sector; else default to a ``Companies/<target>.md`` note
    (companies are the common case — operator decision). Returns
    ``(target_note, subject)`` — ``subject`` keys the proposal filename. Pure
    filesystem; never raises (a scan miss → the default)."""
    try:
        company = find_company_note(root, target)
        if company:
            stem = Path(str(company.get("path") or "")).stem or target
            return f"Companies/{stem}.md", stem
        slug = resolve_sector_slug(root, target)
        if slug:
            return f"Topics/{slug}.md", slug
    except OSError:
        pass
    return f"Companies/{target}.md", target


__all__ = [
    "SKIP_DIRS",
    "vault_root",
    "bridge_base_url",
    "crew_sensitivity",
    "read_text",
    "split_frontmatter",
    "to_wikilink",
    "extract_ticker",
    "is_valid_ticker",
    "extract_sector_slug",
    "iter_vault_markdown",
    "scan_vault_for_target",
    "find_company_note",
    "resolve_sector_slug",
    "read_sector_context",
    "fetch_comps",
    "fetch_peers",
    "has_market_data",
    "render_financial_summary",
    "ANALYST_ACTION_ROLE",
    "REQUIRED_ANALYSTS",
    "pack_analyst",
    "unpack_analyst",
    "is_valid_analyst_payload",
    "all_analysts_reported",
    "pack_deepdive",
    "unpack_deepdive",
    "vault_prompt",
    "industry_prompt",
    "synthesis_prompt",
    "headline_prompt",
    "HEADLINE_SCHEMA",
    "resolve_capture_target",
]
