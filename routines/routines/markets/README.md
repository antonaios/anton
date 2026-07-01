# routines.markets

Markets-data adapter for the dashboard. Quotes today; fundamentals + peers
+ news in Phase 2.

## Architecture

```
routes/markets.py  ──► get_provider() ──► OpenBBProvider   (live)
                                      ╲
                                       ╲► StubProvider     (offline / unlicensed)
```

`get_provider()` picks at runtime:
- OpenBB importable → `OpenBBProvider`
- else → `StubProvider` (deterministic seed; same data as `dashboard/src/data/seed.ts`)

Set `MARKETS_FORCE_STUB=1` to force the stub even when OpenBB is installed.

## Installation

```bash
# From <repo>/routines/
pip install -e ".[markets]"
```

Without `[markets]`, the bridge still works — endpoints return stub data so
the dashboard ticker stays populated.

## AGPLv3 surface

OpenBB is AGPLv3. Three-ring containment:

1. **Import scoped to this package.** `openbb_provider.py` is the **only**
   file that imports OpenBB. Everything else in `routines/` reaches data
   through the `MarketsProvider` protocol in `adapter.py`.
2. **Optional install.** `pip install -e ".[markets]"` is opt-in. The base
   `routines` install has no OpenBB.
3. **Out-of-process fallback** (future): if you ever need to expose the
   bridge beyond loopback, run OpenBB's own server (`openbb-api`) on a
   separate port and proxy from `routes/markets.py`. Scopes AGPLv3 to that
   process; this package stays clean.

Internal use at Demo Advisers is fine under AGPLv3 (no distribution).
Don't expose this beyond loopback without re-evaluating.

## Sensitivity wrap (non-negotiable)

OpenBB calls out to external providers (YFinance / FMP / FRED / SEC). The
bridge route in `routes/markets.py` validates that all symbols look like
**public tickers** (e.g. `JDW.L`, `IHG.L`, `^FTSE`) — never deal
codenames, target names from confidential mandates, or buyer names from
active sell-sides.

If you find yourself wanting to look up a private company by codename, you
can't. That's by design.

## Endpoints (current)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/markets/quotes?symbols=JDW.L,IHG.L,WTB.L` | Live quotes (60s cache) |
| GET | `/api/markets/health` | Bridge + provider name |

## Endpoints (Phase 2-4 planned)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/markets/fundamentals?ticker=JDW.L&yrs=5` | 24h cache |
| GET | `/api/markets/peers?ticker=JDW.L` | Auto-comp set, 7d cache |
| GET | `/api/markets/news?ticker=JDW.L&days=7` | 30m cache |
| GET | `/api/markets/equity-research?ticker=JDW.L` | 6h cache, feeds Companies/<X>.md |

## Cache

SQLite at `routines/.markets-cache/cache.db`. TTL per call:
- Quotes: 60s
- Fundamentals: 24h
- Peers: 7d
- News: 30m

Wipe with `rm -rf routines/.markets-cache/` to force a refresh.

## Provider chain (planned)

Free tier first; premium kept out of scope:

1. **YFinance** (default, no API key) — quotes / fundamentals / news
2. **FMP free tier** (250/day) — better fundamentals
3. **FRED** (free, requires API key) — macro
4. **SEC EDGAR** (free) — filings
5. **Skip:** Bloomberg / Refinitiv / S&P CapIQ — licensed

## Adding a function

1. Add to `MarketsProvider` protocol in `adapter.py`
2. Implement in `openbb_provider.py` with `@cached(ttl_seconds=...)`
3. Implement in `stub_provider.py` with seed data
4. Add route in `routes/markets.py` with public-id validation
5. Add TypeScript type + client method in `dashboard/src/lib/api.ts`
