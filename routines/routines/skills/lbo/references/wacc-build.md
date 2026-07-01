# WACC build — for the LBO discount-rate sanity check

Loaded only when Anton needs to sanity-check an LBO's implied sponsor return
against a cost-of-capital benchmark. The LBO engine does **not** compute WACC;
this is a cross-check the operator may ask for.

**Build order (post-tax):**

1. **Cost of equity (Ke)** via CAPM: `Ke = Rf + β_levered × ERP`.
   - `Rf` — on-the-run government yield matching the hold horizon (5y gilt for
     a 5y hold), not a generic 10y.
   - `ERP` — equity risk premium from the house assumption set; cite it.
   - `β_levered` — re-lever an unlevered peer beta to the deal's target
     structure: `β_L = β_U × (1 + (1 − tax) × D/E)`.
2. **Cost of debt (Kd)** — the blended margin on the indicative term sheet
   (TLA/TLB/RCF), post-tax: `Kd × (1 − tax)`.
3. **Weights** — use the *target* capital structure (the LBO's entry D/E), not
   the peer's.

`WACC = E/V × Ke + D/V × Kd × (1 − tax)`.

**Use in an LBO context:** a sponsor IRR materially below the implied WACC is a
red flag worth surfacing — the deal may not clear the cost of capital even
before the equity-risk premium a sponsor demands. Every input (Rf, ERP, beta
source) needs a source-register entry —
[no-invented-sources](../../../../../OS%20AI%20Vault/CLAUDE.md#no-invented-sources).
