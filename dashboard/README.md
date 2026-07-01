# ANTON dashboard

React + Tailwind frontend for the Agentic OS routines bridge. Implements
`Topics/Architecture/dashboard-brief.md` from the vault.

## Stack

- Vite 4.5.x (Node 18.12 compatible — upgrade Vite when Node moves to 20 LTS)
- React 18 + TypeScript
- Tailwind 3.4
- Lucide icons, Inter via @fontsource

## Commands

```bash
npm install
npm run dev          # http://127.0.0.1:5173, proxies /api -> 127.0.0.1:8765
npm run build
npm run typecheck
```

The dev server expects the FastAPI bridge at `127.0.0.1:8765`. Start it
from the routines repo:

```bash
cd "<repo>/routines"
python -m routines.api.app
```

## Layout

The page composition tracks the brief 1:1:

- TopHeader · MarketTicker · ProjectControls · MainTabs
- LEFT workspace (panel containing PromptPanel, the workflow grid, optional
  RunResultPanel, and three StatusCards along the floor)
- RIGHT sidebar (PriorityTasks, LatestIntelligence, VaultActivity, Forecast)

Workflow sections live in a 2-column workspace grid:
- Left: Research, Meetings, Vault & Ops
- Right: Transaction materials, Valuation

Each section uses a 3-column grid for buttons. Orphan rows (IC memo,
Post-call cleanup, Newsletter run + Meeting notes sync) inherit the same
button size and align to the left edge of the grid — handled by the shared
`WorkflowButton` size class in `components/WorkflowButton.tsx`.

## Wired vs static

Live endpoints (FastAPI bridge): Recall query, Reindex, Promote memory,
Newsletter run, plus the project list (in the dropdown) and vault pulse
(in the right sidebar).

The other 20 workflow buttons are static — they set the active workflow
and surface a "not yet wired" hint when clicked.
