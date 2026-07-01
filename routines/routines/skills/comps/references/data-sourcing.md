# Data sourcing — Stage 2 (acquire WITH SOURCES)

> Loaded on demand by Anton during the Stage 2 acquisition + verification loop.

## The source contract

Every populated cell carries a Source. No exception. The Source cell takes
ONE of three forms:

| Form | Meaning | Example |
|---|---|---|
| URL | Direct citation to the source page | `https://www.ihgplc.com/-/media/.../fy25-results.pdf` |
| `<provider>:<as_of_date>` | Provider feed pulled at this date | `openbb-yfinance:2026-06-01` |
| `<connector>:<as_of_date>` | Institutional connector | `factset:2026-06-01` |
| `tracker:<deal_id>` | Canonical tracker reference for CoTrans | `tracker:PT-2026-04-15-abc` |
| `operator-approved:<date>` | Operator-confirmed assumption (no external source) | `operator-approved:2026-06-01` |

The pre-stamp guard refuses any blank Source cell. The mechanical test
asserts this on every populated row.

## CoCo data → source map

| Field | Required source type | Default path |
|---|---|---|
| `market_cap_m` / `price` / `currency` | Markets provider | `provider.get_quotes()` → `<provider>:<as_of>` |
| `revenue_lfy_m` / `ebitda_lfy_m` | Filing OR provider fundamentals | `provider.get_fundamentals()` if clean; IR scrape fallback if missing |
| `net_debt_m` | Filing / IR / results release | Firecrawl scrape of the company's IR `/results` page; cited URL. **DO NOT estimate as EV-mktcap residual** (that's an identity, not a source). |
| `revenue_lfy1_m` / `ebitda_lfy1_m` | Connector consensus OR operator approval | Connector if licensed → `<connector>:<as_of>`. Otherwise surface to operator as an assumption with **three choices (Q2, decided 2026-06-02): (a) `blank` leave empty; (b) `self_historical` apply the company's own realised LFY-over-LFY-1 growth → `operator-approved:self-historical-growth:<date>`; (c) `operator_input` supply `growth_rate` + a REQUIRED `justification` (un-justified rates are refused, HTTP 422) → `operator-approved:growth=<r>;<justification>:<date>`.** **DO NOT silently fill from a Yahoo consensus scrape.** |

## CoTrans data → source map

| Field | Required source type | Default path |
|---|---|---|
| `announced_date` / `target` / `acquirer` / `country` | Press release / tracker | Always from a citable source |
| `buyer_type` (tracker "Acquirer type") | Classified at acquisition (Q5) | `"Strategic"` (corporate/trade) or `"Financial"` (PE/sponsor/fund) via `classify_acquirer_type()`; **ambiguous → blank, flagged for operator review (never guessed)**. Stamped into the template's "Buyer Type"/"Acquirer type" column when present. |
| `ev_m` / `ev_revenue_x` / `ev_ebitda_x` | Tracker OR deep-research | `tracker:<deal_id>` if in tracker; cited press release URL otherwise. **No control-premium adjustment (Q4)** — multiples are stamped as reported; the operator applies any control/minority judgment in Excel. |
| `strategic_commentary` | Deep-research synthesis | Operator-editable post-stamp; the source URL stays in the Source cell. **Control-premium context belongs here (Q4):** when a deal carries a known control premium, note it in Strategic Commentary rather than adjusting the multiple. |

**Deal-status filter (Q6, decided 2026-06-02):** the CoTrans candidate pool defaults to `announced_or_closed`. Deep-research-sourced deals carry no explicit status → treated as **announced** (included). A future Mergermarket import supplies a closed/completed status + closed date. Explicit `terminated`/`withdrawn`/`rumoured`/`lapsed` rows are excluded.

## Currency / unit mismatch flags

Two distinct mismatches to flag (never silently resolve):

1. **Trading currency ≠ FS currency.** The provider returns the trading
   currency (`Quote.currency`); the filing returns the functional currency
   (`Fundamentals.currency`). Common cases:
   - JDW.L: trading GBp / FS GBP — **unit mismatch** (penny vs pound), divide by 100 OR stamp in p — operator chooses.
   - ACS.PA: trading EUR / FS EUR — clean.
   - HBM.SW: trading CHF / FS USD — true currency mismatch, surface to operator.

2. **Reporting period mismatch.** The target's FY ends in May; a peer's FY ends in December. Both LFY values are "last full year" but the calendar overlap is partial. Surface the FY-end column (H YE) in the stamp so the operator can see the mismatch; do not normalise.

## LFY+1 — the special case

LFY+1 (forward-year forecast) is the only figure with a HARDER source rule:
**connector OR operator-approved.** Why:

- Yahoo/OpenBB return a forward-consensus number, but it's a SCRAPE of broker
  consensus — not a single citable source, and the source-quality is the
  median broker, which the operator's IC memo can't credibly cite.
- A real connector (CapIQ / FactSet / LSEG / PitchBook) gives a contracted
  consensus feed that IS citable as `<connector>:<as_of>`.
- Without a connector, the safe path is: surface the Yahoo candidate +
  the assumption type to the operator; operator approves → Source cell
  carries `operator-approved:<date>`. Never silently fill.

The brief calls this `lfy1_approved_unless_connector` and it's a declared
guardrail in the frontmatter.

## When to surface for operator approval

ALWAYS surface (and pause Stage 2) for:
- Currency mismatch (FS vs trading, OR unit p vs £)
- Reporting period mismatch where the operator's IC memo will need to comment
- ANY unsourced figure (no URL, no provider, no connector) — surface as
  "candidate is X (from Yahoo consensus); approve or re-source?"
- LFY+1 not from a connector

NEVER silently fill in:
- A residual computed from an identity (net debt = EV − mkt cap is not a source)
- A figure cited as "see filing" or "in the deck" without a URL or page reference
- A broker-consensus scrape as the canonical LFY+1

## Cross-refs

- [no-invented-sources](<vault>/CLAUDE.md#no-invented-sources)
- `populate-template.md` — Stage 3 stamp (uses these Source values)
- `peer-identification.md` — Stage 1 (sources the peer + deal LIST)
