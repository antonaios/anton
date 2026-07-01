---
type: investment-proposal
memory_kind: procedural
produces_kind: semantic   # governance submissions are durable artifacts — semantic memory
project: "[[Projects/]]"
target: "[[Companies/]]"
date: YYYY-MM-DD
sensitivity: confidential
deal-type: acquisition | minority-investment | jv | divestment | partnership-with-equity
case: base | bull | bear
sponsors: []  # senior sponsors of the deal — typically CEO/CFO/Strategy lead
prepared-by:
  - "[[People/]]"   # operator
  - "[[People/]]"   # head of corp dev or counterpart
governance-body:    # firm-specific — read from _claude/firm.md governance_body
governance-date:    # date of submission
sources:
  - "[[01 Source Register#]]"
engine-runs: []     # list of valuation engine run IDs that produced numbers in this memo
tags: [investment-proposal, ic, governance]
tldr: {2 sentences capturing the deal — what it is, recommendation, headline economics. Read first.}
---

# {Project Name} — Investment Proposal

> **Deliverable shape:** Corporate-side investment proposal — internal governance submission for an investment / acquisition / JV / minority stake / divestment decision. Modelled on DemoRetailer GFAB-style submission forms. Distinct from `Templates/ic-memo.md` (PE / buy-side IC memo flavour). See `_claude/CLAUDE.md` §7 for shape selection.
>
> **This template is the assembly point** for outputs from upstream skills. See "Section ↔ source" map at the bottom of this file for which skill / data source feeds which section.

---

## Cover sheet — Approval submission form

**To:** {governance_body — pulled from `_claude/firm.md`, e.g. "GFAB" / "IC" / "Investment Committee"}
**From:** {prepared-by team — typically Corporate Development / M&A team}
**Cc:** {cc list}
**Date:** YYYY-MM-DD
**Subject:** {Project Name} — Investment Proposal

---

### Sign-off received from

> Internal-team sign-offs required before submission. Tick when received. **If a team has not been approached, state why explicitly** — silence is not acceptable.

| Team | Status | Relevant contact(s) | Notes |
|---|---|---|---|
| **Legal** (Local / UK / cross-border counsel) | ☐ | | |
| **Finance / Accounting** (Group Finance) | ☐ | | |
| **Business / Operations Finance** (incl. relevant FLT approval for country-specific sign-offs) | ☐ | | |
| **Tax** | ☐ | | |
| **Treasury** | ☐ | | |
| **Other** (e.g. Risk, Compliance, Data Privacy, Procurement) | ☐ | | |

### Approver list

| Role | Name | Status |
|---|---|---|
| CEO | | ☐ |
| CFO | | ☐ |
| GC / Legal | | ☐ |
| {Sector / division CEO} | | ☐ |
| {Sponsor 1} | | ☐ |
| {Sponsor 2} | | ☐ |
| Corporate Development team | | ☐ |

---

### Cover-sheet Q&A summary

> 9-item Q&A summary on the cover sheet. Detail in the body memo below.

#### 1. What approval is being sought?
{1–3 sentence summary of the ask. State quantum, equity %, structure type. State "see body memo for detail" if material complexity.}

**Strategic rationale headline:**
1. {Rationale 1}
2. {Rationale 2}
3. {Rationale 3}

#### 2. When is the approval required?
{Timing — w/c date, deadline-driven event}

#### 3. What recommendation is being made?
{Concise recommendation — "Invest £Xm for ~Y% stake structured as follows: ..."}

See body memo for detail.

#### 4. Are there any alternative options available?
{Numbered list — including the "Don't do anything" / "Don't invest" option. State why each non-recommended option is not preferred.}

#### 5. Background to this request
{1–2 paragraph context — strategic backdrop, how the opportunity arose, prior commercial relationship if any}

#### 6. Does this accord with applicable group policies?
{Yes / No + relevant policy citations. If No, explain why this course of action is being sought given the divergence.}

#### 7. Accounting and reporting implications?
{Initial accounting view — IFRS treatment of consideration, equity vs financial-instrument classification, any complex items (warrants, earn-outs, contingent consideration, embedded derivatives). Flag "TBD" where Finance review pending.}

#### 8. Any other comments?
{Free text — anything that doesn't fit elsewhere but a Board approver should know.}

#### 9. Corporate structure diagram
{Pre- and post-deal structure. Embed as image or link to a diagram file in `Projects/<deal>/12 Outputs/`.}

---

## Body memo

> **Sponsors:** {senior sponsor names — typically Group CTO / CFO / Strategy lead / equivalent}
> **Prepared by:** {operator name + "Head of Corporate Development" or equivalent}

### {Project Name} — {1-line opportunity headline}

> Example structure: "Project Matrix — Opportunity to take a ~5% minority stake in Marketplacer for ~£6m"
> Format: `{Project codename} — {Action verb} {%/quantum} {target} for {consideration}`

### The Opportunity

- {1-paragraph description of what the deal is}
- {Final terms TBC — but the opportunity is to {action} for an amount in the region of {quantum} ({local currency equivalent if cross-border})}

**Strategic rationale for {acquisition / equity investment / JV} on top of any existing commercial relationship:**

1. **{Rationale headline 1}** — {1–2 sentences}
2. **{Rationale headline 2}** — {1–2 sentences}
3. **{Rationale headline 3}** — {1–2 sentences}
4. **{Rationale headline 4}** — {1–2 sentences}

> See accompanying summary investment case paper if the cover Q&A points here.

### Context — {firm}'s strategy and how this deal fits

- {Bullet on the firm's broader strategic agenda this deal supports}
- {Bullet on the specific context — selection of partner, prior commercial agreement, market backdrop}
- {Bullet on the timing trigger — fundraise window, exit window, regulatory clock}
- {Bullet on comparable precedents — has the firm done similar deals before? Have peers? What was the outcome?}

### Strategic rationale (detailed)

1. **{Rationale 1 headline.}** {Paragraph expanding the rationale with specifics — market size, market share, competitive context, links to research / diligence appendix.} → `[[Source Register#xyz]]`
2. **{Rationale 2 headline.}** {Paragraph.} → `[[Source Register#xyz]]`
3. **{Rationale 3 headline.}** {Paragraph.}
4. **{Rationale 4 headline.}** {Paragraph.}

### Strategic rationale — {secondary entity / subsidiary}

> Use this section if the deal has implications for a subsidiary or sister business unit (e.g. DemoRetailer/dunnhumby in Project Matrix). Skip if not applicable.

- {Implication 1 for the secondary entity}
- {Implication 2}

### The investment proposal

> Multiple scenarios where applicable — the original proposal as received, any revised proposal worked up alongside, comparison.

#### {Original / Initial Proposal}

- {Bullet describing the initial proposal terms}
- {Valuation, multiple, structure}
- {Why this structure was proposed}

#### {Revised Proposal}

- {Bullet describing the revised proposal}
- {Difference vs original — quantum, structure, mechanic}
- {Why the revision was triggered (e.g. counterparty constraint, new info)}

#### Economics consistency check

- {Quantum / discount / equity % implied by each scenario — show that the economics are or aren't consistent}

#### Comparison table

| | Original Proposal | Revised Proposal |
|---|---:|---:|
| Total investment | | |
| New shares issued | | |
| Existing shares transferred | | |
| Warrants / "free equity" | | |
| Total shares post-deal | | |
| Blended price / share | | |
| Round price / share | | |
| Discount vs round | | |
| Equity % (post-money, fully diluted) | | |

> If multiple share classes / pricing methods (undiluted vs diluted), spell out which is being used and footnote the rationale.

#### Recommendation on investment proposal

- {Recommended option}
- {Why}
- {Any side-letter / Letter of Intent components alongside the headline transaction}

---

### Due diligence

#### Financial & commercial

##### What is the target's Total Addressable Market (TAM)?

- {1-paragraph estimate with methodology referenced — bottoms-up / tops-down}
- {See Appendix for detailed TAM build}

##### Summary financials

> Key 3-year history + projection table. Pull from the target's plan / management info. **Engine-derived numbers (DCF / comps multiples) trace to engine run hashes — see `engine-runs:` in frontmatter.**

| {LCY}m unless stated | FY-3 | FY-2 | FY-1 | FY0 | FY1 | FY2 | FY3 | FY4 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Total revenue | | | | | | | | |
| YoY growth | | | | | | | | |
| {Recurring revenue / ARR / MRR — sector specific} | | | | | | | | |
| Gross profit | | | | | | | | |
| Margin % | | | | | | | | |
| EBITDA / EBITDAaL / EBITDAR | | | | | | | | |
| Margin % | | | | | | | | |
| Opening cash | | | | | | | | |
| Operating CF | | | | | | | | |
| Investing & financing CF | | | | | | | | |
| Closing cash | | | | | | | | |

> Note (if applicable): "Cash flow does not include cash in from proposed fundraise" — flag explicitly.

##### View on the financial model

- {1–2 paragraphs on whether the financial model is sophisticated, well-built, granular enough to diligence}
- {Granularity — built up by territory / customer / product? Top-down or bottoms-up?}

##### Revenue KPIs

> Specific to the company's revenue model. For SaaS / marketplace examples (Project Matrix shape):

**Revenue stream 1 — {e.g. SaaS recurring fees}**

- Definition.
- KPIs: {# customers · ARPU · churn · etc.}.
- Historical growth vs forecast.

**Revenue stream 2 — {e.g. Transaction Value commission}**

- Definition.
- KPIs: {# customers · TTV per customer · commission rate · etc.}.
- Historical growth vs forecast.

##### Does the sales pipeline support the revenue KPIs?

- {Bullet on benchmarking exercise — historical growth vs forecast vs commercial-contract implied growth}
- {Geographic split of forecast growth — home market vs new territories}
- {Customer concentration / contracted vs uncontracted forecast revenue}
- {Conclusion: top-line projections look reasonable / aggressive / conservative}

##### Cash burn and cash runway

| Pro-forma cash position | {LCY}m |
|---|---:|
| 1. Estimated cash today | |
| 2. Current fundraise (in) | |
| 3. Existing debt repayment (out) | |
| **Cash post fundraise** | |
| 4. Financing CF to cashflow positive | |
| 5. Operating CF to cashflow positive | |
| **Illustrative additional funding requirement** | |

- Annual cash burn ~{LCY}Xm (monthly burn ~{LCY}X.Xm).
- Runway today ~{X mths}; post-fundraise ~{Y mths}.
- {If applicable: "Business is envisaging additional fundraise in {year} to bridge to positive cash generation; illustrative {LCY}Xm requirement at our {%} stake ≈ {LCY}X.Xm of follow-on dilution-protection capital."}

##### Existing debt / venture debt / senior facilities

- {Facility provider, quantum, interest rate, maturity}
- {Refinance feasibility — has the company tested refinance terms? What were they?}
- {Cost of refinance vs run-the-business-as-is — flag any drag on EBITDA / FCF}

#### Risks

- **{Risk 1 — typically execution risk on the plan}** — {1–2 sentences on the risk and what gives or doesn't give comfort}.
- **{Risk 2 — typically market / cyclical}** — {…}.
- **{Risk 3 — typically counterparty / governance / cap table}** — {…}.
- **{Risk 4 — typically tail / regulatory}** — {…}.
- {Conclusion: net view — is the risk-adjusted return acceptable? What's the upside-vs-downside skew?}

#### Legal

- **Existing share options / cap table mechanics** — {who has options, dilution mechanics, whether the round is priced on diluted or undiluted basis, and which way that cuts for the firm}.
- **Material agreements being assumed** — {commercial contracts that survive change of control}.
- **Change-of-control / consent requirements** — {who needs to consent for the deal to proceed}.
- **Indemnities, warranties, MAC clauses, non-competes** — {key SPA / Subscription Agreement terms}.
- **Final legal documents to be agreed** — {status, no red flags / red flags identified}.

#### Technology *(if a tech investment / target has material tech)*

- {Tech team's diligence findings — leadership, roadmap, software development life cycle, architecture}.
- {Recommendation — proceed / pause / pass — with what oversight}.
- {Material delivery delays or red flags identified}.

#### Accounting

- {Accounting treatment of consideration — equity-method, FVTPL, FVOCI, trade investment}.
- {Treatment of warrants / contingent equity / earn-outs — likely IFRS classification, P&L vs equity treatment}.
- {Consolidation question — does the firm consolidate, equity-account, or hold as investment?}.
- {Goodwill / PPA implications if applicable}.
- {Status: TBD or finalised; Finance contact for follow-up}.

#### Tax

- {Tax treatment of the holding structure — withholding tax on dividends, capital gains on exit, transfer pricing on any commercial relationship}.
- {Group tax position — does the deal create / consume tax assets, change the group's effective rate, trigger any anti-avoidance considerations}.
- {Status: TBD or finalised}.

#### Treasury

- {Cash-flow timing — when is consideration paid, in what currency, against what triggers}.
- {FX and hedging — if cross-border, what's the FX exposure, when does it crystallise, hedging recommendation}.
- {Funding source — cash on hand, drawdown of existing facilities, new debt, equity, asset recycling}.

---

### Indicative timetable / next steps

| Key date | Step |
|---|---|
| | {Pre-submission alignment milestones} |
| | {Internal governance steps — finance review, legal sign-off, tax sign-off} |
| | **{Governance body}** approval submission |
| | {Counterparty Board / Shareholder approvals} |
| | {Final legal documents finalised} |
| | {Sign date} |
| | {Counterparty general meeting / EGM} |
| | {Expected payment date} |
| | {Allocation / completion date} |
| | {Post-completion announcements / disclosures} |

---

## Appendices

### Appendix 1 — Valuation

> **Three angles minimum.** Engine-driven where possible (DCF + comps + LBO via the Excel-template wrapper at `<repo>\engine\`).

- **Public trading comparables** — EV/Revenue, EV/EBITDA(aL), P/E for a peer set. State the date stamp; note where IFRS 16 / hyperinflation / non-GAAP adjustments apply (see `Sectors/<X>.md` accounting conventions).
- **Comparable precedent transactions** — EV/Revenue and EV/EBITDA on M&A precedents within the sector, ideally last 24–36 months.
- **DCF (entry / exit)** — engine-driven: WACC, terminal growth, projection horizon. Show sensitivity table (WACC × terminal growth).
- **VC / LBO method** *(if early-stage or LBO target)* — entry valuation required to deliver target IRR (e.g. 30% annual return) on a Yr-5 exit at a defined exit multiple range.

**Conclusion:** {entry valuation is reasonable / aggressive / favourable, within range of {LCY}Xm – {LCY}Ym}.

> Embed valuation chart from engine output. Reference the engine run ID(s) under `engine-runs:` in frontmatter.

### Appendix 2 — Cap table (target)

| Shareholder type | # of holders | Equity % |
|---|---:|---:|
| Founder / management | | |
| VC / angels | | |
| Retail | | |
| Strategics | | |
| **Total** | | 100% |

> Where the cap table has nuance (preferred vs ordinary, options pool, convertibles), spell out separately. Include named strategic shareholders if known and material.

### Appendix 3 — Total Addressable Market (TAM)

> Bottoms-up build, both revenue streams (or whatever segments are appropriate). Show the working — sources of each input, multiplication.

**{Revenue stream 1} — TAM = {LCY}X.Xbn**

- Step (i): {input}
- Step (ii): {input}
- Step (iii): {result}

**{Revenue stream 2} — TAM = {LCY}X.Xbn**

- Step (i)–(iii) similar

**Conclusion:** target market share at maturity ≈ {%} — {a bullish stretch / a conservative target / in line with home-market track record}.

### Appendix 4 — Revenue due diligence (if material)

> Detailed benchmarking of the revenue projections vs (a) historical growth and (b) any commercial-contract implied growth. Customer-by-customer / region-by-region breakdown if granularity supports it.

- Customer count by region, with historical CAGRs and forecast CAGRs side by side.
- Customer mix by size segment (small / medium / large).
- Revenue per customer benchmarking.
- For TTV / commission businesses: TTV per customer benchmark and commission rate trajectory.
- Sensitivity to customer-acquisition pace.

### Appendix N — {Other} *(as needed)*

- Commercial DD, Tech DD, ESG DD, IT DD — separate appendix per workstream that has material output worth annexing.

---

## Section ↔ source map (orchestration)

> When this template is invoked (e.g. user asks "build the investment proposal for {Project}"), the assembling Claude session pulls from these sources for each section. Documented here so the orchestration is mechanical and auditable.

| Section | Upstream source / skill |
|---|---|
| Cover-sheet approver list | `_claude/firm.md` `governance_approvers:` |
| Sign-off team list | `_claude/firm.md` `internal_teams:` |
| Sponsors / Prepared by | `Projects/<deal>/00 Brief.md` + `_claude/profile.md` |
| The Opportunity / Headline | `Projects/<deal>/00 Brief.md` |
| Context (firm strategy) | `_claude/profile.md` `current_role:` + `Sectors/<sector>.md` |
| Strategic Rationale | Operator analytical voice (manual) + `Projects/<deal>/05 Research/` |
| Investment Proposal terms | Deal-team work — populated manually from negotiation state |
| Comparison table (scenarios) | Deal-team work — populated manually |
| **Target overview** *(in body)* | `/company-profile <Target>` deep profile |
| **Summary financials** | Engine output (DCF / comps templates, when wired up) — `engine-runs:` |
| Revenue KPIs / DD | Manual diligence work + `Projects/<deal>/05 Research/` |
| Sales pipeline benchmarking | Manual diligence + commercial-contract implied growth |
| Cash burn / runway | Engine output + management info |
| Existing debt | Management info / DD |
| **Risks** | Operator analytical voice + `Projects/<deal>/05 Research/` |
| Legal | `Projects/<deal>/03 Emails/` + Legal DD output |
| Technology | Tech DD output workstream notes |
| Accounting / Tax / Treasury | Internal-team workstream notes — typically `Projects/<deal>/05 Research/` |
| Indicative timetable | Deal-team work — populated manually |
| **Valuation appendix** | Engine output — DCF / comps / LBO runs — `engine-runs:` |
| Cap table | Target diligence data — typically `Projects/<deal>/04 VDR Documents/` (gitignored) |
| TAM appendix | `Sectors/<sector>.md` + manual sector work + `Projects/<deal>/05 Research/` |
| Revenue DD appendix | Manual diligence work |
| **Sources for every claim** | `Projects/<deal>/01 Source Register.md` |

> **Numbers in this memo:** every number must trace to either (a) an engine run logged in `engine-runs:` frontmatter, (b) a Source Register entry, or (c) a clearly-flagged TBD. **No LLM arithmetic** — see `_claude/CLAUDE.md` §5.1.

---

*Template generated 2026-05-08, modelled on Project Matrix (DemoRetailer GFAB) submission. Sensitivity defaults to **confidential** because most deal-investment-proposals are. Adjust if and only if all underlying material is genuinely public.*
