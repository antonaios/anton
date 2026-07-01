# Credit-stats glossary — for the leverage / cap-structure narration

Definitions Anton uses when narrating the LBO's debt structure. Loaded on
demand; not deal-specific.

- **Senior leverage** = Senior net debt (TLA + TLB + drawn RCF − cash) / EBITDA.
  In the v4 template, TLA + TLB are gross; net debt at close nets `min_cash`.
- **Total leverage** = (all debt incl. sub / PIK / pref where it ranks as debt)
  / EBITDA. Distinguish from senior — sub and preferred change the ranking.
- **Interest coverage (EBITDA / cash interest)** — how many times operating
  earnings cover cash interest. Below ~2.0x is tight for a sponsor structure.
- **Fixed-charge cover** = EBITDA / (cash interest + scheduled amortisation +
  cash tax). Stricter than interest cover; the covenant lenders usually test.
- **DSCR (debt-service coverage)** = cash available for debt service /
  (interest + scheduled principal). A cash-flow, not earnings, test.
- **FCF conversion** = (EBITDA − capex − ΔNWC) / EBITDA. Drives the cash sweep;
  low conversion (working-capital-heavy or capex-heavy) slows de-leveraging.

**Sector norms (orientation, always cite the specific deal's comps):** software
6–7x total is common on high FCF conversion; cyclical industrials run lower
(3–4x). The `debt_ebitda` input is floored by `min_equity` in the engine — a
requested leverage above the floor is silently clamped, so confirm the intended
structure actually applied (Phase 3).
