import type {
  IntelItem, MarketTickerItem, TaskItem, ActivityItem,
  SectorComp, PipelineDeal, DealTimelineEvent, DealAction,
  DealActivityItem, DealQuickAction, DealDetail,
  MorningBriefData, DailyDigestData, IntelFeedItem,
} from "../types";

export const MARKETS: MarketTickerItem[] = [
  { name: "Gold",     value: "£2,341.28", change: "+0.48%", direction: "up"   },
  { name: "Brent",    value: "$82.15",    change: "-0.71%", direction: "down" },
  { name: "S&P 500",  value: "5,384",     change: "+0.35%", direction: "up"   },
  { name: "FTSE 100", value: "8,172",     change: "-0.12%", direction: "down" },
  { name: "NASDAQ",   value: "16,795",    change: "+0.62%", direction: "up"   },
  { name: "SONIA 3M", value: "4.81%",     change: "",       direction: "flat" },
  { name: "UK 10Y",   value: "4.27%",     change: "",       direction: "flat" },
];

export const SECTOR_COMPS: SectorComp[] = [
  { name: "JD Wetherspoon",      ticker: "JDW.L",  price: "604p",   change: "+0.4%", up: true,  points: "0,15 6,14 12,12 18,11 24,9 30,8 36,7 42,5 48,4 54,3 60,2"  },
  { name: "IHG",                 ticker: "IHG.L",  price: "7,420p", change: "+1.2%", up: true,  points: "0,16 6,14 12,12 18,11 24,9 30,8 36,6 42,7 48,5 54,4 60,2"  },
  { name: "Whitbread",           ticker: "WTB.L",  price: "2,940p", change: "−0.4%", up: false, points: "0,4 6,6 12,5 18,8 24,7 30,10 36,9 42,12 48,11 54,14 60,13" },
  { name: "Mitchells & Butlers", ticker: "MAB.L",  price: "285p",   change: "+0.8%", up: true,  points: "0,12 6,11 12,9 18,10 24,7 30,8 36,5 42,6 48,4 54,3 60,5"  },
  { name: "Hollywood Bowl",      ticker: "BOWL.L", price: "312p",   change: "−1.1%", up: false, points: "0,5 6,7 12,8 18,10 24,9 30,12 36,11 42,13 48,14 54,15 60,16"},
  { name: "SSP Group",           ticker: "SSPG.L", price: "157p",   change: "+1.3%", up: true,  points: "0,15 6,13 12,12 18,11 24,12 30,9 36,10 42,8 48,7 54,6 60,7" },
];

export const PIPELINE_DEALS: PipelineDeal[] = [
  { code: "FALCON",    stage: "Buy-side · Diligence wk 5",   sensitivity: "CONF", nextLabel: "IC Tue T-3",  nextStatus: "due", active: true  },
  { code: "HEARTWOOD", stage: "Sell-side · NDA outstanding", sensitivity: "CONF", nextLabel: "NDA 27d ovd", nextStatus: "ovd", active: false },
  { code: "SAGE",      stage: "Advisory · Origination",      sensitivity: "INT",  nextLabel: "Pitch +9d",   nextStatus: "ok",  active: false },
];

export const DEAL_TIMELINE: DealTimelineEvent[] = [
  { label: "Mandate signed",   date: "04 Apr",      state: "done"   },
  { label: "Teaser sent",      date: "14 Apr",      state: "done"   },
  { label: "Site visit · MCR", date: "30 Apr",      state: "done"   },
  { label: "Site visit · BRS", date: "07 May",      state: "done"   },
  { label: "IC committee",     date: "12 May T-3",  state: "next"   },
  { label: "Bids due",         date: "26 May T-17", state: "future" },
  { label: "Exclusivity",      date: "09 Jun T-31", state: "future" },
];

export const DEAL_ACTIONS: DealAction[] = [
  { tag: "due",  title: "Draft IC memo for committee Tue", meta: "HiNotes 2026-05-07 · est 3h" },
  { tag: "open", title: "Review FY25 audit comments",      meta: "+2d · Email · Stephen"       },
  { tag: "flag", title: "Update model with Q1 estimates",  meta: "+3d · Manual"                },
];

export const DEAL_PEOPLE = [
  "Daniel Marston · CEO Heartwood",
  "Stephen Anderson · CFO target",
  "Anna Costa · Partner Latham",
];

export const DEAL_ACTIVITY: DealActivityItem[] = [
  { kind: "Upd", path: "02 Meeting Notes/post-call-08.md", ago: "38m" },
  { kind: "New", path: "04 VDR/Q&A response v2.docx",      ago: "1h"  },
  { kind: "Upd", path: "05 Model/falcon-base.xlsx",         ago: "2h"  },
  { kind: "New", path: "02 Meeting Notes/2026-05-08.md",    ago: "6h"  },
];

export const DEAL_QUICK_ACTIONS: DealQuickAction[] = [
  { code: "ICM", label: "Draft IC memo",   variant: "suggested", workflowKey: "ic-memo"        },
  { code: "RQ",  label: "Recall · thesis", variant: "wired",     workflowKey: "recall-query"   },
  { code: "CB",  label: "Comps (Build)",   variant: "wired",     workflowKey: "comps-build"    },
  { code: "CM",  label: "Comps pull",      variant: "default",   workflowKey: "comps-pull"     },
  { code: "3S",  label: "3-statement",     variant: "default",   workflowKey: "three-statement"},
  { code: "SN",  label: "Sensitivity",     variant: "default",   workflowKey: "sensitivity"    },
  { code: "AG",  label: "Build IC agenda", variant: "default",   workflowKey: "build-agenda"   },
];

// ── Per-deal detail records (drive ActiveDealPanel content per deal) ─────
// Hand-authored today; will be backed by /api/projects/:code reading
// Projects/<code>/02 Meeting Notes/, /03 Actions/, etc. once parsed.

export const DEAL_DETAILS: Record<string, DealDetail> = {
  FALCON: {
    code: "FALCON",
    name: "Project Falcon",
    side: "Buy-side",
    sectorLabel: "T&L",
    stage: "Diligence",
    sensitivity: "CONF",
    owner: "OPR",
    ageWeeks: 5,
    lastTouched: "38m ago",
    timeline: DEAL_TIMELINE,
    actions: DEAL_ACTIONS,
    people: DEAL_PEOPLE,
    peopleMoreCount: 5,
    activity: DEAL_ACTIVITY,
    quickActions: DEAL_QUICK_ACTIONS,
  },

  HEARTWOOD: {
    code: "HEARTWOOD",
    name: "Heartwood Collection",
    side: "Sell-side",
    sectorLabel: "T&L",
    stage: "NDA outstanding",
    sensitivity: "CONF",
    owner: "OPR",
    ageWeeks: 1,
    lastTouched: "27d ago",
    timeline: [
      { label: "Mandate signed",          date: "14 Apr",      state: "done"   },
      { label: "Teaser drafted",          date: "16 Apr T-27", state: "next"   },
      { label: "NDA + IM out to buyers",  date: "—",           state: "future" },
      { label: "Management presentation", date: "—",           state: "future" },
    ],
    actions: [
      { tag: "flag", title: "Send NDA to Heartwood Collection",   meta: "overdue · 27 days · HiNotes 2026-04-12" },
      { tag: "due",  title: "Finalise teaser one-pager",          meta: "+1d · Comms · Operator"                 },
      { tag: "open", title: "Build long-list buyers (UK pubs)",   meta: "+5d · Manual"                           },
    ],
    people: [
      "Daniel Marston · CEO Heartwood",
      "Operator · MD Heartwood",
      "Anna Costa · Partner Latham",
    ],
    peopleMoreCount: 3,
    activity: [
      { kind: "New", path: "02 Meeting Notes/2026-04-12-DM.md",  ago: "27d" },
      { kind: "Upd", path: "01 Brief/teaser-draft-v3.docx",       ago: "2d"  },
    ],
    quickActions: [
      { code: "TZ",  label: "Teaser draft",     variant: "suggested", workflowKey: "teaser"       },
      { code: "BL",  label: "Buyer universe",   variant: "default",   workflowKey: "buyer-list"   },
      { code: "ND",  label: "NDA pack",         variant: "default",   workflowKey: "ndas"         },
      { code: "PL",  label: "Process letter",   variant: "default",   workflowKey: "process-letter" },
      { code: "RQ",  label: "Recall · history", variant: "wired",     workflowKey: "recall-query" },
      { code: "CM",  label: "Comps for IM",     variant: "default",   workflowKey: "comps-pull"   },
    ],
  },

  SAGE: {
    code: "SAGE",
    name: "Project Sage",
    side: "Advisory",
    sectorLabel: "Leisure",
    stage: "Origination",
    sensitivity: "INT",
    owner: "OPR",
    ageWeeks: 0,
    lastTouched: "9d ago",
    timeline: [
      { label: "Origination call",   date: "02 May", state: "done"   },
      { label: "Pitch deck",         date: "20 May", state: "next"   },
      { label: "Engagement letter",  date: "—",      state: "future" },
    ],
    actions: [
      { tag: "due",  title: "Update buyer universe for Sage", meta: "+9d · Manual"          },
      { tag: "open", title: "Sector benchmarking · listed",   meta: "+11d · Research"       },
    ],
    people: [
      "External — sponsor side",
      "External — corporate side",
    ],
    peopleMoreCount: 0,
    activity: [
      { kind: "New", path: "01 Brief/sage-pitch-v1.docx",      ago: "9d" },
      { kind: "New", path: "03 Research/sage-sector-read.md",  ago: "9d" },
    ],
    quickActions: [
      { code: "PRP", label: "Proposal",          variant: "suggested", workflowKey: "proposal"     },
      { code: "MS",  label: "Market snapshot",   variant: "default",   workflowKey: "market-snapshot" },
      { code: "SR",  label: "Sector read",       variant: "default",   workflowKey: "sector-read"  },
      { code: "CP",  label: "Company profile",   variant: "default",   workflowKey: "company-profile" },
      { code: "RQ",  label: "Recall · sponsors", variant: "wired",     workflowKey: "recall-query" },
      { code: "CM",  label: "Comps pull",        variant: "default",   workflowKey: "comps-pull"   },
    ],
  },
};

/**
 * Look up the detail record for a given deal code. Returns the FALCON
 * record as a safe default so the UI always renders something coherent.
 */
export function dealDetailFor(code: string): DealDetail {
  return DEAL_DETAILS[code] ?? DEAL_DETAILS.FALCON;
}

export const PRIORITY_TASKS: TaskItem[] = [
  { id: "t-1", title: "Send NDA to Heartwood Collection",  source: "HiNotes 2026-04-12", status: "overdue"   },
  { id: "t-2", title: "Draft IC memo for Project Falcon",  source: "HiNotes 2026-05-07", status: "due-today" },
  { id: "t-3", title: "Review FY25 audit comments",        source: "Email — Stephen",    status: "open"      },
  { id: "t-4", title: "Update buyer universe for Sage",    source: "Manual",             status: "open"      },
];

export const LATEST_INTEL: IntelItem[] = [
  { id: "i-1", text: "DemoTelco agrees £11bn buyout of JV partner stake in mobile unit" },
  { id: "i-2", text: "Greene King reports H1 LFL -5.2% margins 18.9%" },
  { id: "i-3", text: "PE-backed Heartwood Collection eyes UK pub estate expansion" },
];

export const VAULT_ACTIVITY_FALLBACK: ActivityItem[] = [
  { path: "Companies/Heartwood Collection.md",    ago: "38m ago", kind: "UPDATED" },
  { path: "Projects/Falcon/02 Meeting Notes.md",  ago: "1h ago",  kind: "UPDATED" },
  { path: "Inbox/HiNotes/processed-hash3xru...",  ago: "1h ago",  kind: "CREATED" },
  { path: "Sectors/Leisure.md",                   ago: "2h ago",  kind: "UPDATED" },
  { path: "Resources/Newsletters/2026-05-08-...", ago: "6h ago",  kind: "CREATED" },
];

export const STATUS_CARDS = [
  { title: "5-hour cap",  primary: "3.1M / 5.0M",   sub: "7 sessions",   pct: 62 },
  { title: "Weekly cap",  primary: "41.0M / 60.0M", sub: "43 sessions",  pct: 68 },
  { title: "Routines",    primary: "9 / 15",        sub: "£14.30 today", pct: 60 },
];

// ── Morning brief: auto-generated overnight by local Ollama ───────────────
export const MORNING_BRIEF: MorningBriefData = {
  date: "Mon · 11 May 2026 · 09:24 BST",
  source: "Generated overnight · Local Ollama qwen3:14b",
  needsYou: [
    { marker: "ovd",  text: "Send NDA to Heartwood Collection",   sub: "overdue · 27 days · HiNotes 2026-04-12" },
    { marker: "due",  text: "Draft IC memo · Project Falcon",      sub: "due today · committee Tue"               },
    { marker: "open", text: "Review FY25 audit comments",          sub: "+2d · Email · Stephen"                   },
    { marker: "open", text: "Update buyer universe — Sage",        sub: "+5d · Manual"                            },
  ],
  sectorThisWeek: [
    { marker: "news", text: "Greene King · H1 trading update",     sub: "Tue · listed comp for Falcon thesis" },
    { marker: "news", text: "Hollywood Bowl · Q3 trading",          sub: "Wed · LFL guidance flagged"          },
    { marker: "news", text: "Whitbread · Premier Inn RevPAR",       sub: "Fri · prior +3.2% YoY"                },
  ],
  antonSuggests:
    "Greene King reports Tuesday. Your Falcon thesis assumes consumer LFL recovery in Q2 — last week's BRC retail sales were soft. The trading update is worth scanning before the IC. Try /equity GNK or schedule for Tue 06:30.",
};

// ── Daily digest: auto-generated at 22:00 by local Ollama ─────────────────
export const DAILY_DIGEST: DailyDigestData = {
  date: "Mon · 11 May 2026 · 22:00 BST",
  source: "Generated · Local Ollama qwen3:14b",
  activity: [
    { marker: "routine", text: "sectornews",     sub: "3 ok"           },
    { marker: "routine", text: "hinotes",        sub: "1 ok"           },
    { marker: "routine", text: "morning-brief",  sub: "1 ok"           },
    { marker: "routine", text: "deal-tracker",   sub: "2 ok · 1 skipped" },
  ],
  vaultChanges: [
    { marker: "vault", text: "Projects/Falcon/02 Meeting Notes/2026-05-11.md", sub: "Projects · 14:22 BST" },
    { marker: "vault", text: "Companies/Greene King.md",                       sub: "Companies · 16:45 BST" },
    { marker: "vault", text: "Resources/Newsletters/2026-05-11-Leisure.md",     sub: "Resources · 06:32 BST" },
  ],
  antonCloses:
    "Quiet but productive — Falcon meeting note landed and Greene King comp page refreshed ahead of the Tuesday trading update. No routine errors. Tomorrow's IC prep will lean on the comp refresh; consider running /equity GNK first thing.",
};

// ── Intelligence feed: routine outputs, vault changes, news ───────────────
export const INTEL_FEED: IntelFeedItem[] = [
  {
    id: "if-1",
    source: "Routines · Recall",
    sourceTone: "ok",
    ago: "38m ago",
    title: "Recall index refresh complete · 1,247 notes embedded",
    description:
      "Background reindex finished. Added 12 notes, updated 38, skipped 1,197 unchanged. Vector index at .recall-index/index.db (38.4 MB).",
    pill: "Run · ok",
    link: "Open audit ›",
  },
  {
    id: "if-2",
    source: "HiNotes",
    sourceTone: "warn",
    ago: "2h ago",
    title: "New transcript · Heartwood Collection — call with Daniel Marston",
    description:
      "Structured note written to Projects/Heartwood-Bid/02 Meeting Notes/. 4 action items stubbed, 2 People stubs created. NDA follow-up flagged on Priority Tasks.",
    pill: "Note · created",
    link: "Open note ›",
  },
  {
    id: "if-3",
    source: "Sector news",
    sourceTone: "ok",
    ago: "6h ago",
    title: "Travel & Leisure newsletter 2026-05-09 · 14 items · 3 deals",
    description:
      "Top stories: Greene King H1 trading update, IHG Q1 RevPAR +3.2%, Hollywood Bowl FY25 prelim. 12 sources cited.",
    pill: "Newsletter",
    link: "Open ›",
  },
];

export const NAV_ITEMS = ["Knowledge Vault", "Plan", "Approvals", "Admin Override"];

export const TABS = [
  { key: "agent",     label: "Agent"  },
  { key: "daily",     label: "Daily"  },
  { key: "inbox",     label: "Inbox"  },
  { key: "runs",      label: "Runs"   },
  { key: "drafts",    label: "Drafts" },
  { key: "vault",     label: "Vault"  },
  { key: "budget",    label: "TKN Budget" },
  { key: "providers", label: "Providers" },
  { key: "taxonomy",  label: "Taxonomy" },
  { key: "operator",  label: "Operator" },
] as const;
