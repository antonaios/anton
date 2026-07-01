---
type: company-profile
memory_kind: procedural
produces_kind: semantic   # instances written from this template are semantic memory
subject: "[[Companies/]]"
project: "[[Projects/]]"
date: YYYY-MM-DD
sensitivity: public | internal | confidential
case: reference
sources:
  - "[[Source Register#]]"
tags: [company-profile]
tldr: {2 sentences capturing the load-bearing finding(s). Read first; details below.}
---

# {Company Name} — Company Profile

> Deep-research company profile. Distinct from `Templates/one-pager.md`, which is the 4-quadrant snapshot version. Produced when the user asks for "company profile", "company search", "deep dive", "research", or "what do we know about <X>". See `_claude/CLAUDE.md` §7 for shape selection.

---

## ⚡ MUST-KNOW NOW (last 14 days)

> Single most important live fact about this company that is dated within the last ~14 days. Surfaced here because it changes the framing for everything below. If there is no such fact, write "no material recent news" and keep the section.

{paraphrased single-paragraph summary of the most-recent material event, with a `(confirmed)` / `(self-reported)` / `(speculation)` certainty marker, and a `[[Source Register#xyz]]` link.}

---

## 1. Snapshot

{4–6 sentences capturing what the company is, scale, ownership, listing, the active strategic narrative, and the headline financial direction. Paraphrased, not pasted from anywhere. Sources behind each non-public claim.}

---

## 2. Financials and business overview

### 2.1 What the company is today

{1–2 paragraphs: business model, geographic footprint, customer base, ownership structure, listing status, fiscal year-end. Clarify any segments the financials below will reference.}

### 2.2 Geographic / segment split — most recent reported year

| Segment | Revenue ({ccy}m) | % of group | EBITDA(aL) ({ccy}m) | % of group | Margin | Capex / capital additions ({ccy}m) |
|---|---:|---:|---:|---:|---:|---:|
| | | | | | | |
| **Group / total** | | 100% | | 100% | | |

{1–2 sentences on which segment is the swing factor for group earnings, and which is the largest by capital deployed.}

### 2.3 Three-year financial track record

> **Minimum 3 fiscal years + most recent interim period.** Restate to continuing-operations basis if the company has had material disposals (footnote which years are restated). Include both reported and organic growth where the company discloses both.

| {ccy}m unless stated | FY-3 | FY-2 | FY-1 | H1 FY0 / latest |
|---|---:|---:|---:|---:|
| Total revenue | | | | |
| Service revenue / recurring revenue | | | | |
| Organic service revenue growth | | | | |
| {Sector EBITDA metric — EBITDAaL / EPRA EBITDA / underlying EBITDA / etc.} | | | | |
| Organic EBITDA growth | | | | |
| EBITDA margin | | | | |
| Operating profit / (loss) | | | | |
| Capital additions / capex | | | | |
| Capex intensity (% revenue) | | | | |
| Adjusted free cash flow | | | | |
| Net debt | | | | |
| Net debt / EBITDA leverage | | | | |

{2–3 paragraphs interpreting the trajectory. Not a recital of the numbers — the *story* the numbers tell. Where any one-off effects distort headlines (impairments, hyperinflation, FX, disposals), call them out specifically with quantification.}

### 2.4 Drivers in plain English

For each material segment, write 1 short paragraph explaining the operational drivers behind the numbers — not the numbers themselves. Examples of what to cover: regulatory changes, network/infrastructure investments, competitive intensity shifts, customer-experience initiatives, currency/hyperinflation accounting effects, FX translation drag.

- **{Segment 1}:** {what's actually happening on the ground that explains the trend}
- **{Segment 2}:**
- **{Segment 3}:**

### 2.5 What else you'd want to ask

{Bullet list of 6–10 specific information requests that go beyond the public disclosures and would materially change a view on the company. Examples: subscriber/ARPU breakdown, network coverage % by tech, tax structure deferred-tax detail, JV/associate dividend flows, lease and pension commitments, ROCE evolution, capital allocation framework, specific contingent liabilities. Sector-tailored.}

---

## 3. Strategy, goals and capex

### 3.1 Strategic framework

{If the company has stated strategic pillars or priorities under current leadership, lay them out in a 3-column table: pillar · translation · progress to date. Cite the strategic-review document or investor presentation. If the company doesn't articulate explicit pillars, infer from disclosure and label inferences as `(inferred)`.}

| Pillar | Translation | Latest progress |
|---|---|---|
| | | |
| | | |
| | | |

### 3.2 Specific commitments and goals

{Bullet list of concrete, time-bound commitments management has made publicly. Each bullet anchored to a source. Includes: capital-allocation policy (leverage corridor, buyback programmes, dividend policy), revenue/EBITDA growth targets, capex envelopes, M&A criteria, ESG / Net Zero commitments where material.}

### 3.3 Capex — three-year picture

| {ccy}m | FY-3 | FY-2 | FY-1 | H1 FY0 |
|---|---:|---:|---:|---:|
| Capital additions (continuing) | | | | |
| Plus: Spectrum / licences (cash) | | | | |
| Plus: Integration capital additions | | | | |
| **Total cash capex** | | | | |

{Notes on any unusual one-off items in the period, and on what year-on-year shape implies for forward run-rate.}

### 3.4 Capex by geography / segment

| Segment | FY-3 | FY-2 | FY-1 | Capex intensity FY-1 (% revenue) |
|---|---:|---:|---:|---:|
| | | | | |
| **Group** | | | | |

{2–3 read-outs on which segment is most capex-heavy, where it's stepping up, and which is being deprioritised.}

### 3.5 Capex by category

{Tangible vs intangible split (PP&E vs software/licences/development) where disclosed. Maintenance vs growth where disclosed. Be explicit when the split isn't disclosed and is being inferred.}

### 3.6 Capex management bar

{1 paragraph on how the company is approaching capital efficiency — AI/data-driven optimisation, prioritisation frameworks, network sharing, partner financing. Connects to synergy-modelling implications for any active M&A.}

### 3.7 Forward capex guidance

> Forward-looking. Pulls together what management has actually said about future capex — envelope, peak/trough, mix shift — distinct from the historical record in 3.3–3.6. If management has issued no explicit guidance, write "no specific guidance issued" and note when next update is expected (capital-markets day, FY results).

| Period | Guided capex envelope ({ccy}m) | Guidance source | Implied y-o-y | Notes |
|---|---:|---|---:|---|
| FY0 (current) | | | | |
| FY+1 | | | | |
| FY+2 | | | | |
| Mid-term (≥ FY+3) | | | | |

{1–2 paragraphs reading the trajectory: is capex peaking, stable, declining? Is the maintenance / growth mix shifting (e.g. growth-capex envelope rising while maintenance flat)? Cross-reference to §3.1 strategic pillars — is the spend backing the stated strategy? Flag any divergence between guidance and consensus-broker expectations.}

---

## 4. M&A activity — full lookback (≥4 years)

> Every material transaction the company has done in the lookback period, in three tables: Disposals, Mergers/JVs, Acquisitions. Plus a "rejected / abandoned" list and a final pattern-recognition section. The discipline: what does this company's deal-doing actually look like? What does it tell you about how their team will work?

### 4.1 Disposals

| Completion | Asset | Buyer | EV | Multiple | What it was | Strategic rationale | View |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

### 4.2 Mergers / JVs

| Completion / status | Transaction | Counterparty | EV / value | Strategic rationale | View |
|---|---|---|---|---|---|
| | | | | | |

### 4.3 Acquisitions

| Completion | Asset | Seller | EV | Multiple | Strategic rationale | View |
|---|---|---|---|---|---|---|
| | | | | | | |

### 4.4 Rejected / abandoned

- {Transaction} — {what was on the table, why it didn't proceed, what that tells you}.
- ...

### 4.5 The pattern

{4–6 numbered bullets that read out the strategic logic of the M&A programme. Examples of pattern types: "disposals at premium multiples to fund deleveraging", "in-market consolidation in core geographies", "infrastructure separation for multiples-arbitrage", "rotation between low-growth and high-growth markets", "capability bolt-ons in adjacent services". This is the most useful section for any future deal work — it tells you what the company's M&A team will say yes to.}

---

## 5. Specific deal case study

> Include this section *only* when the company has a single transaction big enough to warrant a deep dive (>10% of EV, transformational, or live and not yet closed). Otherwise omit. Structure:

### 5.1 Background
{Why was this deal done — market structure, strategic logic, parties, timeline pre-announcement.}

### 5.2 Deal mechanics
{Structure (cash / share / merger / JV); consideration; valuation implied; governance; any earn-outs, options or conditional elements.}

### 5.3 Synergies
{Run-rate £/€/$, cost vs revenue split, time to capture, NPV if disclosed, integration cost.}

### 5.4 Investment / capex commitments
{Any committed investment that won regulatory approval; behavioural commitments; consumer-protection conditions.}

### 5.5 Equity and debt structure at completion
{Pro-forma capital structure, leverage impact, equity injections required.}

### 5.6 Regulatory journey
{Timeline of competition / sector / national-security review milestones.}

### 5.7 Recent developments
{Any subsequent events: integration progress, follow-on transactions (e.g. minority buyouts), regulatory follow-through.}

### 5.8 What this tells you about the M&A team's mindset
{4 bullets — speed, price discipline, capital structure, synergy capture. This is the bridge from "what they did" to "what they care about".}

### 5.9 Likely interview / IC questions and answers
{3 questions you would expect to be asked about this deal, with a model answer for each. Treat as the user's own thinking aloud — not advice to a third party.}

---

## 6. Comparable trading multiples

> All multiples spot at end of most-recent period available, paraphrased from public sources. State date stamp and refresh expectation. Include sector-appropriate non-GAAP metrics (EBITDAaL for telcos, EPRA for REITs, underwriting metrics for insurers, etc.).

### 6.1 Snapshot — current trading multiples

| Company | Market cap | Net debt | EV | LTM revenue | LTM {sector EBITDA metric} | EV / EBITDA(aL) | EV / Revenue | P/E (trailing) | Dividend yield |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **{subject company}** | | | | | | | | | |
| {peer 1} | | | | | | | | | |
| {peer 2} | | | | | | | | | |
| {peer 3} | | | | | | | | | |
| {peer 4} | | | | | | | | | |
| {peer 5} | | | | | | | | | |
| {peer 6} | | | | | | | | | |

### 6.2 Why the multiples differ

{6–8 numbered factors that drive multiple dispersion in this sector. Examples for telecoms: market structure, growth profile, capex intensity / FCF conversion, balance sheet leverage, asset quality / portfolio mix, regulatory environment, currency exposure. For other sectors, replace with the appropriate factor list. Each factor explained with reference to specific peers in the table above.}

1.
2.
3.
4.

### 6.3 Why {subject company} trades where it does

{1 paragraph explaining the gap between the subject and the peer median — what narrative the market is pricing in. Tied to specific concerns / opportunities the company has surfaced or that markets are debating.}

### 6.4 The company's own deal multiples vs trading

{Useful cross-reference: company's trading multiple now vs the multiples its own M&A activity has cleared at. Tells you whether disposals are being done at premium / par / discount, and what that says about asset quality and bargaining position.}

---

## 7. Challenges and headwinds

### 7.1 Company-specific risks

{4–7 bullets, each its own short paragraph. Examples: turnaround execution risk in core market, integration risk in active deal, FX/hyperinflation exposure, capital-allocation tension, tail liabilities from prior transactions, key-person / CFO transition risk. Each anchored to specific facts, not generic concerns.}

### 7.2 Industry-wide / sector headwinds

{4–6 bullets capturing the sector-level dynamics affecting all players. For telecoms: market fragmentation, capital intensity, hyperscaler/OTT competition, regulation, spectrum costs, energy, geopolitics, Pillar 2 tax. For other sectors, the equivalent.}

### 7.3 The optimistic counter-narrative

{For balance: 4 bullets capturing the bull case on the sector + the company. Capex peaking, regulatory pivots, AI cost levers, structural growth pockets, infrastructure-asset multiples. Honest devil's-advocate, not boosterism.}

---

## 8. Sector-specific P&L and accounting nuances

> For any sector with established non-GAAP conventions (telecoms, REITs, banks, infrastructure, software/SaaS, etc.), the M&A and IC work depends on getting the conventions right. This section is the company-as-window-into-sector.

### 8.1 The basic shape of the P&L

```
Total revenue
  – {revenue split by recurring vs one-off / service vs equipment / etc.}
Cost of sales
  Gross profit
{Sector-specific opex categories}
                                  ----------
{Sector-specific EBITDA metric — EBITDAaL / EPRA EBITDA / underlying EBITDA}    ← THE KEY OPERATING METRIC
{Adjustments to get to reported EBITDA: lease depreciation, fair-value moves, etc.}
                                  ----------
[Reported EBITDA, comparison only]
Restructuring / one-offs
D&A of owned assets
Share of associates and JVs
Impairment charge
                                  ----------
Operating profit / (loss)
Investment income
Financing costs
                                  ----------
Profit before tax
Tax
                                  ----------
PAT continuing operations
Discontinued ops
                                  ----------
Net result
```

### 8.2 Sector-specific metrics that matter

{4–6 bullets covering the metrics that drive analyst and acquirer thinking in this sector. Examples for telecoms: service revenue vs total revenue, ARPU, customer KPIs (net adds, churn, base by product), capex intensity / OpFCF, spectrum and licences. For REITs: NAV per share, EPRA NTA, like-for-like rental growth, occupancy, WAULT, debt LTV, ICR. For banks: NIM, cost-income, CET1, NPL, ROTE. Pick the right list for the sector and explain *why* each metric matters for valuation.}

### 8.3 Performance-metric debate — which EBITDA?

{Short answer: which non-GAAP EBITDA metric does the sector and the company use, and why. For telecoms this is EBITDAaL. For REITs, EPRA EBITDA. For software, ARR-led metrics. Explain the gap between reported GAAP EBITDA and the non-GAAP convention, and why one is more economically meaningful.}

### 8.4 Major accounting wrinkles

{For telecoms: IFRS 16 mechanics, hyperinflation IAS 29, customer acquisition cost capitalisation, handset subsidies, equity-accounted JVs/associates. For REITs: fair-value vs cost model, depreciation under historical cost. Etc. Each wrinkle: 1 paragraph on what it does to the headline numbers and what the M&A team needs to adjust for in transaction work.}

### 8.5 Other sector-specific items

{Pension obligations, off-balance-sheet guarantees, regulatory capital, deferred tax assets, contingent considerations from prior deals — anything that isn't in EBITDA but affects valuation.}

---

## SOURCES

> Numbered superscripts in the body link here. Each source has a stable anchor in the project's `01 Source Register.md` (or in `Registers/Sources.md` if cross-project).

1. ...
2. ...
3. ...

---

*Generated {YYYY-MM-DD} from public / internal sources as cited. Sensitivity: **{tier}**. Routed via {lane}. Refresh comp multiples and any pending deal completions on the morning of any client / IC meeting.*
