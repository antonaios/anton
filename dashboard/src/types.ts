export type TaskStatus = "overdue" | "due-today" | "open";

export interface TaskItem {
  id: string;
  title: string;
  source: string;
  status: TaskStatus;
}

export interface IntelItem { id: string; text: string; }

export interface ActivityItem {
  path: string;
  ago: string;
  kind: "CREATED" | "UPDATED";
}

export interface MarketTickerItem {
  name: string;
  value: string;
  change: string;
  direction: "up" | "down" | "flat";
}

export interface RecallSource { rank: number; path: string; score: number; sensitivity?: string; mtime?: string; }

export interface RecallResponse {
  query: string;
  hits: RecallSource[];
  synthesis?: string;
}

export interface AuditRun {
  ts: string;
  run_id: string;
  status: "ok" | "skipped" | "error";
  duration_ms?: number;
  routine?: string;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  error?: string;
}

// ── Scheduler (#scheduler-panel) — mirrors the bridge DTOs in
//    routines/api/routes/scheduler.py. The list is read-only; pause/resume/
//    run-now are audited operator actions.
export interface SchedulerJob {
  id: string;
  name: string;
  func: string;
  trigger: string;
  next_run?: string | null;
  jobstore: string;
  coalesce: boolean;
  max_instances: number;
  misfire_grace_time?: number | null;
}

export interface SchedulerJobsResponse {
  running: boolean;            // false ⇒ scheduler offline / paused — surface, don't error
  jobs: SchedulerJob[];
}

// ``durable`` ⇒ the pause is persisted (cron-registry spec) and survives a
// bridge restart; false ⇒ live-only (ad-hoc / one-shot jobs).
export interface SchedulerPauseResponse { id: string; paused: boolean; durable: boolean; }
export interface SchedulerResumeResponse { id: string; paused: boolean; durable: boolean; }
export interface SchedulerRunNowResponse { id: string; status: string; run_id: string; }

export interface SchedulerRunRecord {
  ts: string;
  run_id: string;
  status: string;
  duration_ms?: number | null;
  error_class?: string | null;
  error?: string | null;
}

export interface SchedulerHistoryResponse { runs: SchedulerRunRecord[]; }

export interface ApiError { detail: string; }

export interface SectorComp {
  name: string;
  ticker: string;
  price: string;
  change: string;
  up: boolean;
  points: string;
}

// ── Markets adapter (bridge: /api/markets/*) ──────────────────────────────
export type QuoteDirection = "up" | "down" | "flat";

export interface Quote {
  symbol: string;
  name: string;
  price: string;
  change: string;
  direction: QuoteDirection;
  points: string;
  currency?: string | null;
  provider?: string | null;
}

export interface QuotesResponse {
  provider: string;       // "stub" | "openbb" | ...
  requested: string[];
  quotes: Quote[];
}

// Ticker-bar config (from _claude/tickers.md via /api/markets/ticker-bar).
export interface TickerBarEntry {
  symbol: string;
  name: string;
}

export interface TickerBarResponse {
  tickers: TickerBarEntry[];
  source: "config" | "fallback";
}

// Macro bar (indices, commodities, rates, indicators) via
// /api/markets/macro-bar.
export type MacroKind = "equity" | "index" | "commodity" | "rate" | "indicator";

export interface MacroRow {
  symbol: string;
  name: string;
  kind: MacroKind;
  value: string;       // "$104.91" / "4.46%" / "10,370"
  change: string;      // "+0.4%" / "+4bp" / "+0.21pp"
  direction: "up" | "down" | "flat";
  points: string;
  note?: string | null;
  provider?: string | null;
}

export interface MacroBarResponse {
  rows: MacroRow[];
  source: "config" | "fallback";
}

// Comps workflow (POST /api/workflows/comps)
export interface CompRow {
  symbol: string;
  name?: string | null;
  currency?: string | null;
  revenue?: number | null;
  ebitda?: number | null;
  ebitda_margin?: number | null;
  revenue_growth_5y_cagr?: number | null;
  pe?: number | null;
  ev_ebitda?: number | null;
  net_debt_ebitda?: number | null;
  dividend_yield?: number | null;
  fiscal_year?: number | null;
}

export interface CompsResult {
  target_symbol: string;
  target_name?: string | null;
  rows: CompRow[];
  note_path?: string | null;
  provider?: string | null;
  warnings: string[];
}

// Equity Research workflow (POST /api/workflows/equity-research)
export interface EquityResearchSnapshot {
  symbol: string;
  name?: string | null;
  currency?: string | null;
  last_price?: string | null;
  price_change?: string | null;
  direction?: QuoteDirection | null;
  pe?: number | null;
  ev_ebitda?: number | null;
  dividend_yield?: number | null;
  ebitda_margin?: number | null;
  revenue_growth_5y_cagr?: number | null;
}

export interface FundamentalsYear {
  fiscal_year: number;
  period_end?: string | null;
  revenue?: number | null;
  gross_profit?: number | null;
  ebitda?: number | null;
  ebit?: number | null;
  net_income?: number | null;
  capex?: number | null;
  operating_cash_flow?: number | null;
  free_cash_flow?: number | null;
  total_debt?: number | null;
  cash_and_equivalents?: number | null;
  shareholders_equity?: number | null;
}

export interface NewsItem {
  title: string;
  url: string;
  published?: string | null;
  source?: string | null;
  summary?: string | null;
}

// Drafts tab
export interface DraftItem {
  project: string;
  path: string;
  name: string;
  mtime: string;
  ago: string;
  size_bytes: number;
  ext: string;
}

// Daily tab
export interface DailyResponse {
  date: string;
  path: string;
  exists: boolean;
  content?: string | null;
  size_bytes?: number | null;
}

/** One row in the Notes "recent" rail — metadata only (no content). Served
 *  verbatim by GET /api/daily/recent (snake_case, like DailyResponse). */
export interface RecentDailyItem {
  date: string;          // ISO YYYY-MM-DD (the note's filename stem)
  path: string;          // vault-relative, e.g. "Daily/2026-06-30.md"
  size_bytes: number;
}

export interface RecentDailyResponse {
  items: RecentDailyItem[];   // most-recent-first, capped at the requested limit
  total: number;              // total daily notes in the vault
}

export interface EquityResearchResult {
  target_symbol: string;
  snapshot: EquityResearchSnapshot;
  fundamentals: {
    symbol: string;
    name?: string | null;
    currency?: string | null;
    years: FundamentalsYear[];
    provider?: string | null;
    error?: string | null;
  };
  comps: CompsResult;
  news: {
    symbol: string;
    items: NewsItem[];
    provider?: string | null;
    error?: string | null;
  };
  note_path?: string | null;
  provider?: string | null;
  warnings: string[];
}

export type DealSensitivity = "CONF" | "INT" | "PUBLIC";
export type DealNextStatus  = "due" | "ovd" | "ok";

export interface PipelineDeal {
  code: string;
  stage: string;
  sensitivity: DealSensitivity;
  nextLabel: string;
  nextStatus: DealNextStatus;
  active: boolean;
}

export type TimelineState = "done" | "next" | "future";

export interface DealTimelineEvent {
  label: string;
  date: string;
  state: TimelineState;
}

export type ActionTag = "due" | "open" | "flag";

export interface DealAction { tag: ActionTag; title: string; meta: string; }

// ── Live actions (bridge: /api/projects/:p/actions) ───────────────────────
// Per the 2026-05-23 inline-tag convention — see workspace-write-policy.md
export type ActionStatus = "overdue" | "open" | "stale" | "done";

export interface ActionItem {
  title: string;
  status: ActionStatus;
  due: string | null;          // ISO YYYY-MM-DD
  owner: string;
  urgent: boolean;
  flag: boolean;
  done: string | null;
  source_file: string;          // absolute path; round-trip back to toggle endpoint unchanged
  source_line: number;          // 1-indexed, hint only (text-hash is authoritative)
  task_hash: string;            // 8-char sha1 of normalised title — task identity
  issue: string | null;         // [issue:ISS-NN] — issue grouping (#issues-register v2)
}

export interface ActionsCounts {
  overdue: number;
  open: number;
  stale: number;
  done: number;
  total_open: number;           // overdue + open + stale
}

export interface ActionsResponse {
  project: string;
  overdue: ActionItem[];
  open:    ActionItem[];
  stale:   ActionItem[];
  done:    ActionItem[];
  counts:  ActionsCounts;
}

// ── Issues register (#issues-register v2: /api/projects/:p/issues) ────────
// Wire shape is snake_case verbatim (no camelize), sibling to ActionsResponse.

export interface IssueGatingItem {
  title: string;
  checked: boolean;
  due: string | null;
  owner: string | null;
  urgent: boolean;
  line: number;                 // 1-indexed in the register file
}

export interface IssueItem {
  id: string;                   // "ISS-03"
  title: string;
  status: "open" | "monitoring" | "blocked" | "closed";
  priority: string | null;      // "P1" | "P2" | "P3"
  owner: string | null;
  raised: string | null;
  affects: string | null;
  resolution: string | null;
  line: number;
  gating: IssueGatingItem[];
  gating_open: number;
  gating_total: number;
}

export interface IssuesCounts {
  open: number;
  monitoring: number;
  blocked: number;
  closed: number;
  non_closed: number;           // the /agenda (v3) contract: everything not closed
}

export interface IssuesResponse {
  project: string;
  exists: boolean;              // false = deal predates the v1 register template
  register_path: string | null;
  issues: IssueItem[];
  counts: IssuesCounts;
}

export interface ToggleRequest {
  source_file: string;
  task_hash: string;
  line_hint?: number | null;
  to: "open" | "done";
}

export interface ToggleCandidate { line: number; snippet: string; }

export interface ToggleResponse {
  success: boolean;
  line?: number | null;
  snippet?: string | null;
  candidates?: ToggleCandidate[] | null;
}

// ── Project chat (#42 · per-deal conversational memory) ─────────────────────
// Mirrors routines/project_chat/schema.py verbatim. Wire shape is snake_case
// (recall_hits / duration_ms / history_turns kept as-is, like ActionsResponse)
// — these endpoints are NOT camelized in lib/api. Endpoints (live after the
// operator restarts the bridge — §10):
//   POST /api/projects/{code}/chat          → ChatResponse
//   GET  /api/projects/{code}/chat/history  → ChatHistoryResponse
export type ChatRole = "user" | "assistant";

/** One recall hit surfaced as a citation for an assistant turn. */
export interface ChatSource {
  path: string;          // vault-relative POSIX, e.g. "Projects/FALCON/02 Meeting Notes/2026-05-08.md"
  score: number;         // recall match score
  excerpt: string;       // chunk text, ~200-300 chars
}

/** One turn of the conversation. `sources` is populated for assistant turns. */
export interface ChatTurn {
  timestamp: string;     // ISO-8601 UTC
  role: ChatRole;
  text: string;
  sources: ChatSource[];
}

/** POST body for /api/projects/{code}/chat. */
export interface ChatRequest {
  project: string;       // deal code, e.g. "FALCON"
  message: string;
  history_turns?: number;  // prior turns in the LLM window; server default 6
  // #42 v2 — relaxed-scope toggle. Default OFF keeps STRICT project scope
  // (recall filtered to Projects/<code>/). When true, recall widens to the WHOLE
  // vault, but out-of-deal content is capped at ≤ internal sensitivity server-
  // side (confidential/MNPI from other deals never surface); the current deal
  // stays full tier. Omitted == false.
  cross_projects?: boolean;
}

/** Return shape for a completed chat turn (the assistant turn, already persisted). */
export interface ChatResponse {
  turn: ChatTurn;
  sources: ChatSource[];
  recall_hits: number;
  duration_ms: number;
  // Echoes the scope this turn actually ran under (so the panel can mark a
  // cross-scope answer). Optional for back-compat with a pre-#42-v2 bridge.
  cross_projects?: boolean;
}

// NOTE: GET /chat/history returns the WRAPPED shape { project, turns } —
// `ChatHistoryResponse` in routines/api/routes/project_chat.py — NOT a bare
// ChatTurn[]. `load_history` is fail-soft (returns [] on a corrupt/unreadable
// _chat.md), so this GET never 409s; the corrupt-log 409 surfaces only on POST.
export interface ChatHistoryResponse {
  project: string;
  turns: ChatTurn[];
}

// ── Project chat streaming (#42 v2 · SSE) ───────────────────────────────────
// POST /api/projects/{code}/chat/stream → text/event-stream. The bridge frames
// named SSE events; `data:` payloads are snake_case verbatim (NOT camelized),
// matching the non-stream ChatResponse. Each variant below is one parsed event,
// discriminated by `type` (the SSE `event:` name) — lifecycle is:
//   start → delta* → (done | error)
/** Stream opened — flushed immediately so the client leaves its connecting state. */
export interface ChatStreamStart { type: "start"; project: string; }
/** One incremental answer chunk (token-ish) to append to the in-progress turn. */
export interface ChatStreamDelta { type: "delta"; text: string; }
/** Terminal success: both turns persisted server-side. Mirrors ChatResponse. */
export interface ChatStreamDone {
  type: "done";
  turn: ChatTurn;
  sources: ChatSource[];
  recall_hits: number;
  duration_ms: number;
  // Scope this turn ran under (#42 v2). Optional for back-compat with a bridge
  // that predates the cross-projects toggle.
  cross_projects?: boolean;
}
/** Terminal failure: NOTHING was persisted. `code` is "corrupt_log" |
 *  "ollama_error" | "error" — surfaced inline, mirroring the POST 409/500 UX. */
export interface ChatStreamError { type: "error"; code: string; message: string; }

export type ChatStreamEvent =
  | ChatStreamStart
  | ChatStreamDelta
  | ChatStreamDone
  | ChatStreamError;

export type ActivityKind = "Upd" | "New";

export interface DealActivityItem { kind: ActivityKind; path: string; ago: string; }

/**
 * Full content for the ActiveDealPanel — one record per deal code. Hand-
 * authored in seed.ts today; eventually backed by `Projects/<code>/`
 * via /api/projects/:code.
 */
export interface DealDetail {
  code: string;                    // "FALCON"
  name: string;                    // "Project Falcon"
  side: string;                    // "Buy-side" | "Sell-side" | "Advisory"
  sectorLabel: string;             // "T&L"
  stage: string;                   // "Diligence" | "Origination" | ...
  sensitivity: DealSensitivity;
  owner: string;                   // "OPR"
  ageWeeks: number;
  lastTouched: string;             // "38m ago"
  timeline: DealTimelineEvent[];
  actions: DealAction[];
  people: string[];
  peopleMoreCount: number;         // "+ 5 more in [[People]]…"
  activity: DealActivityItem[];
  quickActions: DealQuickAction[];
}

export type QuickActionVariant = "suggested" | "wired" | "default";

export interface DealQuickAction {
  code: string;
  label: string;
  variant: QuickActionVariant;
  workflowKey: WorkflowKey;
}

// ── Morning brief ─────────────────────────────────────────────────────────
export type BriefMarker = "ovd" | "due" | "open" | "news";

export interface BriefRow {
  marker: BriefMarker;
  text: string;
  sub: string;
}

export interface MorningBriefData {
  date: string;
  source: string;
  needsYou: BriefRow[];
  sectorThisWeek: BriefRow[];
  antonSuggests: string;
}

// ── Daily digest (EOD wrap-up, sibling to morning brief) ──────────────────
export type DigestMarker = "routine" | "vault" | "session" | "info";

export interface DigestRow {
  marker: DigestMarker;
  text: string;
  sub: string;
}

export interface DailyDigestData {
  date: string;
  source: string;
  activity: DigestRow[];        // routines that fired today
  vaultChanges: DigestRow[];    // files written/modified today
  antonCloses: string;          // 1-3 sentence reflective close
}

// ── Pending proposals (human-in-the-loop review queue) ────────────────────
export type ProposalKind =
  | "learning"
  | "memory-promotion"
  | "lessons-learned"
  | "sector-extraction"        // Plan v3 §6.9 Phase 3
  | "sector-synthesis"         // Plan v3 §6.9 Phase 4
  | "hinotes-unrouted";        // #8 — HiNotes routing (Session G, 2026-05-25)

/** #58 — every proposal carries a derived tier. Approvals (memory-promotion,
 *  learning, sector-extraction/synthesis, lessons-learned) write to the
 *  canonical vault layer and warrant audit-critical UI treatment;
 *  confirmations (hinotes-unrouted, email-unrouted) are lightweight routing. */
export type ProposalTier = "approval" | "confirmation";

export interface PendingProposal {
  id: string;            // 12-hex sha1 of vault-relative path; round-trip into route/reject/skip
  kind: ProposalKind;
  tier: ProposalTier;    // #58 — derived from kind server-side
  path: string;          // vault-relative POSIX
  title: string;
  date: string;          // ISO YYYY-MM-DD (or "" if absent)
}

export interface PendingProposalsResponse {
  total: number;
  /** Partial — bridge only emits the kinds it actually scans; missing kinds
   *  effectively count as zero. Use `byKind[kind] ?? 0` on the consumer side. */
  byKind: Partial<Record<ProposalKind, number>>;
  items: PendingProposal[];
}

/** #inbox-proposal-detail — read-only full write-up for one proposal (the Inbox
 *  card inlines this under the title). Served snake_case verbatim, like pending. */
export interface ProposalContentResponse {
  id: string;
  kind: string;
  path: string;
  title: string;
  date: string;
  body: string;          // markdown body (frontmatter stripped)
}

/** #inbox-resolved-feed — one recently-resolved proposal for the Inbox rail. */
export type ResolvedProposalVerdict = "routed" | "rejected" | "skipped" | "revision";
export interface ResolvedProposalItem {
  proposal_id: string;
  kind: string;
  verdict: ResolvedProposalVerdict;
  at: string;            // ISO-8601 resolution timestamp
  title: string;         // derived from the (now-moved) filename stem
}
export interface ResolvedProposalsResponse {
  total: number;
  items: ResolvedProposalItem[];   // newest-first
}

// ── Inbox action endpoints (Session G, 2026-05-25) ────────────────────────
// `POST /api/proposals/{id}/route|reject|skip` — see OUTSTANDING ## CONTRACTS
// · inbox/proposals routing. Bodies stay snake_case to match Pydantic on the
// wire; responses are also snake_case (no `byKind`-style camelisation here).

export interface RouteProposalBody {
  workspace_type: WorkspaceType;
  workspace_name: string;
}

export interface RouteProposalResponse {
  moved_to: string;     // absolute vault path
}

export interface RejectProposalBody {
  /** #58 — REQUIRED non-empty after `.strip()`; server returns 422 otherwise. */
  reason: string;
}

export interface RejectProposalResponse {
  ok: boolean;
}

export interface SkipProposalBody {
  /** 1-365 days (server-enforced). Default 7 on the server side. */
  defer_days?: number;
}

export interface SkipProposalResponse {
  reappears_at: string; // ISO-8601 UTC
}

// #58-harness2 — POST /api/proposals/{id}/request-revision (backend shipped
// in #58 routines `51958ad`). Writes `<file>.revision.json` sidecar; pending
// scanner excludes the proposal until the source routine re-fires + replaces
// it. 409 if a revision is already pending; 422 if `feedback` is empty /
// whitespace-only after `.strip()`.
export interface RequestRevisionBody {
  /** REQUIRED non-empty after `.strip()`; server returns 422 otherwise. */
  feedback: string;
}

export interface RequestRevisionResponse {
  ok: boolean;
  revision_sidecar_path: string;
}

// ── Intelligence feed ─────────────────────────────────────────────────────
export type IntelTone = "ok" | "warn" | "info";

export interface IntelFeedItem {
  id: string;
  source: string;
  sourceTone: IntelTone;
  ago: string;
  title: string;
  description: string;
  pill: string;
  link: string;
}

export type WorkflowKey =
  | "company-profile" | "market-snapshot" | "sector-read"
  | "comps-pull" | "comps-build" | "precedents-pull" | "deal-tracker-add"
  | "proposal" | "teaser" | "cim-draft" | "buyer-list"
  | "ndas" | "process-letter" | "ic-memo"
  | "build-agenda" | "pre-read-pack" | "pre-call-qa" | "post-call-cleanup"
  | "dcf-run" | "lbo-run" | "sensitivity" | "three-statement" | "ff" | "audit-model"
  | "recall-query" | "promote-memory" | "reindex" | "newsletter-run" | "meeting-notes-sync"
  // #front-door — on-demand operator tiles (formerly cron-only routines)
  | "actions-decay" | "bd-decay" | "lessons-suggest";

// ── Deal tracker (bridge: POST /api/workflows/deal-tracker) — #front-door ────
// Wire shape is snake_case verbatim (workflow routes are NOT camelized). `text`
// is the pasted article body (required); `url` is provenance only (not fetched).
export interface DealPreview {
  target_company: string;
  bidder_company: string;
  seller_company: string;
  announced_date?: string | null;
  enterprise_value_m?: number | null;
  currency: string;
  reported_revenue_multiple_y1?: number | null;
  reported_ebit_multiple_y1?: number | null;
  reported_ebitda_multiple_y1?: number | null;
  target_sector: string;
  deal_description: string;
  source_url: string;
}

export interface DealTrackerResult {
  status: "appended" | "skipped_duplicate" | "dry_run";
  run_id: string;
  deal: DealPreview;
  workbook_path: string;
  row?: number | null;
  existing_row?: number | null;
  warnings: string[];
}

// ── Crew dispatch (bridge: POST /api/crew/{verb}/run) — #front-door ──────────
// Crews are always-local subprocess lanes; the bridge gates sensitivity before
// spawning. The 202 reply carries SSE + poll URLs that are server-absolute
// (already /api-prefixed) — fetch them verbatim, NOT via the BASE-prefixed
// request() helper. The SSE channel is sparse: human_input_required asks + the
// terminal crew_completed (no per-role deltas; the audit poll has those).
export interface CrewRunResponse {
  run_id: string;
  verb: string;
  status: string;        // queued | running | ok | error
  sse_url: string;
  poll_url: string;
}

/** GET /api/crew/manifest — registered crews (mirrors registry.CrewManifestEntry,
 *  snake_case verbatim). Surfaced in the TAXONOMY tab (#35). */
export interface CrewManifestEntry {
  verb: string;
  module: string;
  description: string;
  sensitivity_override: Sensitivity | null;
  cost_cap_tokens: number;
  cost_cap_seconds: number;
  roles: string[];
  models_default: Record<string, string>;
}

/** GET /api/crew/runs/{run_id}?verb=… — the assembled run record. The
 *  AUTHORITATIVE final status (the SSE crew_completed event is sparse); the
 *  subset the crew bubble renders. snake_case verbatim. */
export interface CrewRunRecord {
  run_id: string;
  verb: string;
  status: string;            // ok | error | running | …
  error?: string | null;
  summary?: string | null;   // the crew's CrewOutput.summary (its conclusion) — surfaced in the bubble
  roles_log?: { role: string; status: string }[];
}

// ── Comps build (bridge: POST /api/workflows/comps-build) ──────────────────
// The operator-gated Stage 0-3 pipeline (#21-comps, COMPS-REDESIGN-2026-06-01).
// Wire shape is snake_case verbatim (the workflow routes are NOT camelized —
// cf. CompsResult), so these mirror routines StageResult exactly.
export type CompsBuildStage = "approval_pending" | "complete";

export interface CompsApprovalPayload {
  kind: "subsectors" | "peers_and_deals" | "assumptions";
  proposed: unknown;
  rationale?: Record<string, unknown> | null;
  tracker_writes_planned?: Array<Record<string, unknown>> | null;
}

export interface CompsBuildResult {
  ok: boolean;
  stage: CompsBuildStage;
  stage_just_completed?: number | null;
  deal_name: string;
  target?: string | null;
  run_id: string;
  approval_payload?: CompsApprovalPayload | null;
  approval_token_to_sign?: string | null;
  warnings: string[];
  // Per-token surfacing (the modal threads these to the next stage).
  subsectors_approval_token?: string | null;
  peers_approval_token?: string | null;
  deals_approval_token?: string | null;
  stage_2_blocks_approval_token?: string | null;
  assumptions_approval_token?: string | null;
  // Stage 3 complete fields.
  approved_subsectors?: string[] | null;
  blocks?: Array<Record<string, unknown>> | null;
  headline_ev_ebitda_median?: number | null;
  headline_ev_revenue_median?: number | null;
  peer_count?: number | null;
  deal_count?: number | null;
  as_of?: string | null;
  provider?: string | null;
  template_path?: string | null;
  prior_archived_path?: string | null;
  mirror_refresh_path?: string | null;
  tracker_writes?: Array<Record<string, unknown>>;
}

export interface CompsBuildBody {
  deal_name: string;
  target: string;
  parent_sector: string;
  stage: 0 | 1 | 2 | 3;
  today?: string;
  workspace_type?: "project" | "bd" | "general";
  workspace_name?: string;
  workspace_sensitivity?: Sensitivity;
  approved_subsectors?: string[];
  approved_peers_by_subsector?: Record<string, string[]>;
  approved_deals_by_subsector?: Record<string, string[]>;
  approved_assumptions?: Array<Record<string, unknown>>;
  // #21-comps-step-3 (2026-06-03) — subset approval. Optional: send the
  // bridge's FULL proposed_* set alongside a strict subset as approved_*
  // and the bridge will verify the HMAC over proposed_* + enforce
  // approved_* ⊆ proposed_* (operator can DROP, never ADD). Absent
  // proposed_* → exact-match against approved_* (pre-Step-3 contract).
  proposed_subsectors?: string[];
  proposed_peers_by_subsector?: Record<string, string[]>;
  proposed_deals_by_subsector?: Record<string, string[]>;
  submitted_coco_candidates_by_subsector?: Record<string, Array<Record<string, unknown>>>;
  submitted_cotrans_candidates_by_subsector?: Record<string, Array<Record<string, unknown>>>;
  subsectors_approval_token?: string;
  peers_approval_token?: string;
  deals_approval_token?: string;
  stage_2_blocks_approval_token?: string;
  assumptions_approval_token?: string;
}

// ── Sessions (bridge: /api/sessions/*) ─────────────────────────────────────
// Locked 2026-05-24 per OUTSTANDING.md ## CONTRACTS · sessions. Bridge sends
// snake_case + mixedCase (Pydantic field names verbatim, e.g. `duration_ms`
// alongside `runningText`); `lib/api.ts` recursively camelCases all keys
// before handing back, so the React layer consumes uniform camelCase below.
export type WorkspaceType = "project" | "bd" | "general";
export type SessionMode   = "chat" | "skill" | "composite" | "crew";
export type LaneKind      = "chat" | "skill" | "composite" | "crew";
export type Sensitivity   = "public" | "internal" | "confidential" | "MNPI";

/** Server-shape session, camelCase'd by `lib/api.ts` */
export interface ServerSession {
  id: string;
  workspaceType: WorkspaceType;
  workspaceName: string;
  title: string;
  mode: SessionMode;
  verb?: string | null;
  created: string;        // ISO
  lastActive: string;     // ISO
  archived: boolean;
  pinned: boolean;        // #session-ops — pinned sessions sort first
  messageCount: number;
  // Context-window usage — bridge stamps these per chat turn (latest prompt
  // tokens + the active model's window). Absent until the VAULT backend ships
  // (see session-briefs/SESSION-TKN-BUDGET-BACKEND.md); the chat header then
  // shows a real % instead of "—".
  contextTokens?: number | null;   // wire `context_tokens`
  contextWindow?: number | null;   // wire `context_window`
}

/** Server-shape message, camelCase'd by `lib/api.ts`. Superset of the
 *  ChatCanvas Message — extra wire fields (sessionId, parentMessageId,
 *  created) are kept as optional so the React layer can use this same
 *  type without re-shaping. */
export interface ServerMessage {
  id: string;
  sessionId?: string;
  parentMessageId?: string | null;
  role: "user" | "anton";
  who: string;
  time: string;                 // "HH:mm"
  created?: string;             // ISO
  body?: string | null;
  kpis?: Record<string, unknown>[] | null;
  commentary?: string | null;
  chips?: Record<string, unknown>[] | null;
  steps?: Record<string, unknown>[] | null;
  running?: boolean;
  runningText?: string | null;
  durationMs?: number | null;
  route?: string | null;
  lane?: LaneKind | null;
  parentRunId?: string | null;
  crewRunId?: string | null;
}

export interface CreateSessionRequest {
  workspace_type: WorkspaceType;
  workspace_name: string;
  mode?: SessionMode;
  verb?: string;
  title?: string;
}

export interface ListSessionsResponse { sessions: ServerSession[]; }
export interface MessagesListResponse { messages: ServerMessage[]; }
export interface ArchiveResponse      { ok: boolean; }

export interface PostMessageRequest {
  text: string;
  sensitivity_override?: Sensitivity;
}

export interface PostMessageResponse {
  user_message: ServerMessage;
  anton_message: ServerMessage;
  route: string;
  lane: string;
  sensitivity: Sensitivity;
  stream_mode?: "updates" | "messages" | "values" | "custom" | null;
}

// ── Project overview (bridge: GET /api/projects/{name}/overview) ───────────
// Locked 2026-05-24 per OUTSTANDING.md ## CONTRACTS · project overview (#11).
// Enums mirror the Pydantic Literals in routines/api/routes/projects.py
// verbatim — keep these in sync if the brief template grows new values.
export type ClientSide    = "buy" | "sell" | "advisory";
export type ProjectStage  = "pitch" | "kick-off" | "DD" | "bid-1" | "bid-2" | "signing" | "close";
export type ProjectStatus = "live" | "paused" | "won" | "lost" | "archived";
export type KeyDateState  = "done" | "next" | "future";

export interface ProjectKeyDate {
  label: string;
  date: string | null;      // ISO YYYY-MM-DD or null
  state: KeyDateState;
}

export interface ProjectOverview {
  name: string;
  clientSide?: ClientSide | null;     // wire `client_side`
  sector?: string | null;             // wikilink stripped server-side
  subsector?: string | null;
  industry?: string | null;
  stage?: ProjectStage | null;
  owner?: string | null;
  status?: ProjectStatus | null;
  sensitivity?: Sensitivity | null;
  target?: string | null;
  counterparty?: string | null;
  client?: string | null;
  tldr?: string | null;
  opened?: string | null;             // ISO YYYY-MM-DD
  closed?: string | null;             // ISO YYYY-MM-DD
  // Project-start anchor: `opened` when present, else the project folder's
  // creation time (server ctime fallback). Always populated for a real project,
  // so a lifetime "since project opened" spend query has a `since` to use.
  created?: string | null;            // wire `created` — ISO YYYY-MM-DD
  keyDates: ProjectKeyDate[];         // wire `key_dates` — always present (empty list when missing)
  lastTouched: string;                // wire `last_touched` — required ISO-8601 UTC
}

// ── Workspaces (bridge: GET /api/workspaces, POST /api/workspaces) ─────────
// Locked 2026-05-24 per `routines/api/routes/workspaces.py` Pydantic models.
// POST request body is snake_case (`root_index`); response is camelized.
export interface CreateWorkspaceBody {
  type: WorkspaceType;
  name: string;
  root_index?: number;
}

export interface WorkspacePaths {
  filesystem: string;
  vault?: string | null;
  sourceRoot: string;                 // wire `source_root`
}

export interface CreatedWorkspace {
  type: WorkspaceType;
  name: string;
  paths: WorkspacePaths;
}

export interface CreateWorkspaceResponse {
  workspace: CreatedWorkspace;
}

export interface WorkspaceListItem {
  type: WorkspaceType;
  name: string;
  lastTouched: string;                // wire `last_touched` — ISO-8601 UTC
  sourceRoot: string;                 // wire `source_root` — first of sourceRoots
  // #6c dual-scan tagging — currently meaningful for type="project" + type="bd";
  // general retains single-source semantics so flags default to false / [].
  inVault: boolean;                   // wire `in_vault`
  inCorporateFinance: boolean;        // wire `in_corporate_finance`
  sourceRoots: string[];              // wire `source_roots`
}

export interface ListWorkspacesResponse {
  workspaces: WorkspaceListItem[];
}

// ── LLM burn telemetry (bridge: GET /api/telemetry/llm-burn) ───────────────
// Locked 2026-05-25 (VAULT Session I) — see OUTSTANDING ## CONTRACTS · LLM
// telemetry. Aggregates routines/telemetry/llm_calls.jsonl into per-provider
// (and optionally per-session) summaries. Cost is USD; Ollama is $0 by
// design (local inference). Powers BurnRatePanel.
export interface ModelBurn {
  calls: number;
  tokensIn: number;        // wire `tokens_in`
  tokensOut: number;       // wire `tokens_out`
  costUsd: number;         // wire `cost_usd`
}

export interface ProviderBurn extends ModelBurn {
  models: Record<string, ModelBurn>;
}

export interface SessionBurn extends ModelBurn {
  workspaceType?: WorkspaceType | null;   // wire `workspace_type`
  workspaceName?: string | null;          // wire `workspace_name`
}

/** Per-workspace burn + a per-provider split — the project × LLM matrix.
 *  `providers` maps provider name → its burn within this workspace (matrix
 *  cells). Populated when llm-burn is called with group_by="all"/"workspace". */
export interface WorkspaceBurn extends ModelBurn {
  workspaceType?: string | null;          // wire `workspace_type`
  workspaceName?: string | null;          // wire `workspace_name`
  providers: Record<string, ModelBurn>;
}

export interface LLMBurnSummary {
  window: { since: string; until: string };   // ISO-8601 UTC
  totals: ModelBurn;
  byProvider: Record<string, ProviderBurn>;   // wire `by_provider`
  bySession?: Record<string, SessionBurn> | null;  // wire `by_session`
  byWorkspace?: Record<string, WorkspaceBurn> | null;  // wire `by_workspace`
}

// ── Plan-cap usage (bridge: GET /api/usage/plans) ──────────────────────────
// Mirrors `routines/api/routes/usage.py` PlanRow + PlansResponse verbatim
// (#14b — Tier 1 sweep, 2026-05-27). v1 returns exactly three rows in fixed
// order: anthropic Max (5h messages cap) / openai Plus (3h messages cap) /
// m27 Standard (24h GBP cap). `usedPct` is a fraction 0.0-1.0+ (may exceed
// 1.0 on overrun); the panel renders ×100 for display. `resetInSec` is a
// rolling-window-remaining integer; the panel formats as "Xh Ym".
export type PlanUnit = "messages" | "usd" | "gbp";

export interface PlanRow {
  provider: string;             // "anthropic" | "openai" | "m27"
  planTier: string;             // wire `plan_tier` — "Max" | "Plus" | "Standard" | "Agent-SDK credit"
  periodLabel: string;          // wire `period_label` — "5h block" | "3h block" | "daily £" | "monthly $"
  usedPct: number;              // wire `used_pct` — fraction 0.0-1.0+ (may exceed 1.0)
  used: number;                 // raw used amount in `unit`
  cap: number;                  // plan cap in `unit`
  unit: PlanUnit;
  resetInSec: number;           // wire `reset_in_sec` — seconds until the window resets
  // wire `reset_kind` (#llm-routing-postjune15 B5) — "rolling" for the sliding
  // plan-cap windows, "monthly" for a $-credit row (the Agent-SDK monthly
  // credit, sourced from a provider-scope budget cap). Optional/defaulted on a
  // bridge that predates B5.
  resetKind?: "rolling" | "monthly";
}

export interface PlansResponse {
  plans: PlanRow[];
}

// ── Budget gate (bridge: GET /api/budgets/incidents, POST .../ack) ─────────
// Mirrors `routines/api/routes/budgets.py` IncidentDTO + AckRequest verbatim.
// Scope is a flat (kind, a, b) shape — not a discriminated union — because
// the backend ScopeRef stores the two scope keys positionally per kind:
//   - global              → a=null, b=null
//   - provider/<a>/<b>    → a=provider, b=model
//   - workspace/<a>/<b>   → a=workspace_type, b=workspace_name
// The /incidents endpoint by default returns rows that BLOCK the gate
// (status IN ('open','acknowledged_paused')); pass ?include_acknowledged=1
// for full history.
// "workspace_provider" (a=type:name, b=provider) is the per-project-per-LLM
// scope the TKN BUDGET tab sets — pending a VAULT backend that adds the kind
// (see session-briefs/SESSION-TKN-BUDGET-BACKEND.md); POSTs 422 until then.
export type BudgetScopeKind = "global" | "provider" | "workspace" | "workspace_provider";

export interface BudgetScope {
  kind: BudgetScopeKind;
  a?: string | null;
  b?: string | null;
}

export type BudgetIncidentStatus = "open" | "acknowledged_raised" | "acknowledged_paused";
export type BudgetAckAction      = "raise_cap" | "leave_paused";

export interface BudgetIncident {
  id: string;
  scope: BudgetScope;
  openedAt: string;          // wire `opened_at` — ISO-8601 UTC
  periodStart: string;       // wire `period_start` — ISO-8601 UTC
  currentPct: number;        // wire `current_pct`
  hardPct: number;           // wire `hard_pct`
  capUsd: number;            // wire `cap_usd`
  currentSpendUsd: number;   // wire `current_spend_usd`
  status: BudgetIncidentStatus;
  ackAt?: string | null;     // wire `ack_at`
  ackAction?: BudgetAckAction | "force_clear" | null;  // wire `ack_action`
  ackNewCapUsd?: number | null;                        // wire `ack_new_cap_usd`
  ackComment?: string | null;                          // wire `ack_comment`
}

export interface ListBudgetIncidentsResponse {
  incidents: BudgetIncident[];
}

/** Body for POST /api/budgets/incidents/{id}/ack — snake_case on the wire. */
export interface AckBudgetIncidentBody {
  action: BudgetAckAction;
  comment: string;             // required; server 422 if blank
  new_cap_usd?: number;        // required when action='raise_cap'; must be > current cap
}

// ── Budget policies (bridge: GET/POST/DELETE /api/budgets) ─────────────────
// Mirrors routines/api/routes/budgets.py BudgetPolicyDTO/In. The TKN BUDGET
// tab uses token-only policies: cap_usd=0 (never USD-blocks) + cap_tokens=N.
// Token budgets are TRACK + WARN (v1) — the gate ignores cap_tokens; the
// current_token_pct surfaces usage so the panel can warn at warn/hard pct.
export interface BudgetPolicyRow {
  scope: BudgetScope;          // {kind, a, b}
  capUsd: number;              // wire `cap_usd`
  period: "monthly_utc";
  warnPct: number;             // wire `warn_pct`
  hardPct: number;             // wire `hard_pct`
  created: string;
  lastModified: string;        // wire `last_modified`
  currentSpendUsd: number;     // wire `current_spend_usd`
  currentPct: number;          // wire `current_pct`
  incidentId?: string | null;  // wire `incident_id`
  capTokens?: number | null;   // wire `cap_tokens`
  currentTokens: number;       // wire `current_tokens`
  currentTokenPct?: number | null;  // wire `current_token_pct` — null when no cap_tokens
}

export interface ListBudgetsResponse {
  policies: BudgetPolicyRow[];
  window: { since: string; until: string };
}

/** POST /api/budgets body — snake_case on the wire (Pydantic BudgetPolicyIn). */
export interface CreateBudgetBody {
  scope: { kind: BudgetScopeKind; a?: string | null; b?: string | null };
  cap_usd: number;
  cap_tokens?: number | null;
  warn_pct?: number;
  hard_pct?: number;
}

// ── Tier 2 per-skill provider matrix ───────────────────────────────────────
// Bridge: GET /api/skills/providers, PATCH /api/skills/{key}/provider
// (#llm-routing-tier-2). Mirrors routines/api/routes/skills_providers.py
// SkillProviderRow + SkillsProvidersResponse; GET responses are camelized by
// lib/api.ts (so `effective_source` → `effectiveSource`, and the nested raw
// `override` sidecar dict has its keys camelized too). Resolution precedence
// the matrix reflects: sidecar > frontmatter > env > task-class > default;
// confidential/MNPI skills are forced local (effective_provider="ollama-only",
// effective_source="confidential-policy") and can't be overridden onto cloud.
//
// #llm-routing-postjune15 P2 added the `task-class` layer (a per-task-class
// provider bias below env, e.g. cross-check→openai) + a per-skill cloud MODEL
// pin (preferred_model / effective_model — see CloudModelAlias).
export type ProviderEffectiveSource =
  | "sidecar" | "frontmatter" | "env" | "task-class" | "default" | "confidential-policy";

/** Operator-selectable cloud MODEL alias (#llm-routing-postjune15 P2 Task 3) —
 *  mirrors shared.routing.CLOUD_MODEL_ALIASES. Consumed only on the Claude
 *  (anthropic) lane; a `-1m` variant also sizes the context window to 1M.
 *  `opus-1m` adds the 1M context window; P4 pinned the id (CLI `claude-opus-4-8[1m]` / API native `claude-opus-4-8`). */
export type CloudModelAlias = "opus" | "sonnet" | "haiku" | "opus-1m";

export interface SkillLLMParams {
  temperature?: number | null;
  maxTokens?: number | null;              // wire `max_tokens`
}

/** The raw operator sidecar entry, camelized. Present when the operator has set
 *  an override (vs frontmatter/default pickup). */
export interface SkillOverrideEntry {
  preferredProvider?: string;             // wire `preferred_provider`
  preferredModel?: string;                // wire `preferred_model` — cloud model alias
  llmParams?: SkillLLMParams;             // wire `llm_params`
}

export interface SkillProviderRow {
  key: string;
  sensitivity: Sensitivity;
  workspaceScope: string;                 // wire `workspace_scope`
  // Frontmatter-declared baseline (SKILL.md).
  preferredProvider?: string | null;      // wire `preferred_provider`
  fallbackProvider?: string | null;       // wire `fallback_provider`
  allowedProviders: string[];             // wire `allowed_providers` — {anthropic,openai,ollama}
  preferredModel?: string | null;         // wire `preferred_model` — frontmatter cloud model alias
  llmParams: SkillLLMParams;              // wire `llm_params`
  // Effective values after the sidecar overlay + env/default fall-through.
  effectiveProvider: string;              // wire `effective_provider`
  effectiveSource: ProviderEffectiveSource | string;   // wire `effective_source`
  // Resolved cloud model alias — non-null ONLY on the Claude lane
  // (effective_provider==="anthropic"); null = lane default / not a Claude pick.
  effectiveModel?: string | null;         // wire `effective_model`
  effectiveLlmParams: SkillLLMParams;     // wire `effective_llm_params`
  // Non-null when resolution failed loud (TIER2 PROVIDER INVALID / NOT ALLOWED /
  // SKILL NOT FOUND). Added with #llm-routing-tier-2-matrix-error — absent on a
  // bridge that hasn't restarted onto the new field yet, so always treat as
  // optional. null on a clean resolution (confidential rows resolve cleanly).
  effectiveError?: string | null;         // wire `effective_error`
  override?: SkillOverrideEntry | null;
  // Telemetry roll-up (null/0 until the skill fires under Tier 2).
  lastFire?: string | null;               // wire `last_fire` — ISO
  lastProvider?: string | null;           // wire `last_provider`
  costUsd: number;                        // wire `cost_usd`
  calls: number;
}

export interface SkillsProvidersResponse {
  skills: SkillProviderRow[];
  envProvider?: string | null;            // wire `env_provider` — AGENTIC_CLOUD_PROVIDER, if set
  defaultProvider: string;                // wire `default_provider`
  sidecarPath: string;                    // wire `sidecar_path` — absolute _claude/provider_overrides.yaml
  // #llm-routing-postjune15 G5 (Mission B) — per-CLOUD-provider sensitivity ceiling
  // (providers.<name>.max_sensitivity in _claude/profile.md). null = UNCONFIGURED
  // (no per-provider cap; the §4 matrix + override window remain the gates). §E
  // parity: anthropic + openai both unconfigured today (= `internal` in bridge tier)
  // until Enterprise/ZDR raises one.
  providerCeilings?: Record<string, string | null>;  // wire `provider_ceilings` — absent on a pre-G5 bridge
  asOf: string;                           // wire `as_of`
}

// ── Crew cloud-lane promotion (#crew-cloud-promotion) ────────────────────────
// GET /api/crew/providers + PATCH /api/crew/{verb}/provider (loopback-only).
// Camelized. A promoted role routes its LLM calls back through the gated
// /api/crew/_llm; the crew subprocess never holds cloud keys.

/** One crew's promotion row (mirrors crew_providers.py CrewProviderRow, camelized). */
export interface CrewProviderRow {
  verb: string;
  description: string;
  roles: string[];
  sensitivityLock?: Sensitivity | null;   // wire `sensitivity_lock` — MNPI/confidential lock, else null
  promotable: boolean;                    // live lift: unlocked always; confidential→enterprise; MNPI→enterprise+attestation
  enterprise?: boolean;                   // wire `enterprise` — the plan tier permits the enterprise lift (absent on a pre-B/C bridge)
  mnpiPromotable?: boolean;               // wire `mnpi_promotable` — MNPI-locked AND liftable now (enterprise + an active attestation)
  modelsDefault?: Record<string, string>; // wire `models_default` — local per-role Ollama models (camelize may omit)
  // Raw operator sidecar entry (camelized incl. nested), or null — for "has override".
  override?: { preferredProvider?: string; preferredModel?: string; roles?: Record<string, unknown> } | null;
  promotedRoles?: Record<string, string>; // wire `promoted_roles` — role → cloud lane (camelize may omit when empty)
}

export interface CrewProvidersResponse {
  crews: CrewProviderRow[];
  sidecarPath: string;                    // wire `sidecar_path` — absolute _claude/crew_overrides.yaml
  planTier: string;                       // wire `plan_tier` — bridge | enterprise
  asOf: string;                           // wire `as_of`
}

/** PATCH /api/crew/{verb}/provider body — snake_case verbatim. */
export interface PatchCrewProviderBody {
  preferred_provider?: string;            // anthropic | openai | local
  preferred_model?: string;               // opus | sonnet | haiku | opus-1m (anthropic only)
  role?: string;                          // crew-level if omitted
  clear?: boolean;
}

// ── MNPI cloud-attestations (#crew-cloud-promotion Phase C / #llm-routing P5) ──
// GET/POST /api/mnpi/attestations (loopback-only). An attestation records that a
// provider carries DPA + ZDR + no-training; under enterprise tier it lets
// EXPLICIT MNPI route to that provider's cloud lane. Granting one is the single
// most sensitive operator action — it relaxes the #no-mnpi-to-cloud floor for one
// provider — so the grant is nonce-confirmed (challenge → grant) server-side.

/** One per-provider MNPI cloud-attestation (mirrors mnpi_attestations.py AttestationDTO, camelized). */
export interface AttestationDTO {
  id: string;
  provider: string;                       // canonical: "anthropic" | "openai"
  dpa: boolean;
  zdr: boolean;
  noTraining: boolean;                    // wire `no_training`
  grantedBy: string;                      // wire `granted_by`
  grantedAt: string;                      // wire `granted_at` — ISO
  expiresAt: string;                      // wire `expires_at` — ISO
  revokedAt?: string | null;              // wire `revoked_at`
  revokedReason?: string | null;          // wire `revoked_reason`
}

export interface ListAttestationsResponse {
  attestations: AttestationDTO[];         // active only (not revoked, not expired)
  asOf: string;                           // wire `as_of`
}

/** POST /api/mnpi/attestations/challenge response — a single-use confirmation nonce. */
export interface ChallengeResponse {
  confirmationNonce: string;              // wire `confirmation_nonce`
  expiresInSeconds: number;               // wire `expires_in_seconds`
}

/** POST /api/mnpi/attestations body — snake_case verbatim. All three protections
 *  MUST be true; confirmation_nonce comes from the challenge endpoint. */
export interface GrantAttestationBody {
  provider: string;                       // anthropic | openai (claude/codex accepted, normalised)
  dpa: boolean;
  zdr: boolean;
  no_training: boolean;
  granted_by: string;
  duration_seconds?: number;
  confirmation_nonce: string;
}

// ── LLM-routing posture: lane-matrix (G4) + lane-status (G1) ─────────────────
// #llm-routing-postjune15 Mission B. Both loopback-only GET reads under
// /api/routing, camelized by lib/api.ts. SURFACE-ONLY readouts of the routing the
// dispatcher already does — consuming them changes no behavior.

/** One lane's resolved (provider, model) + whether it runs on the local box.
 *  Mirrors routing_matrix.py LaneInfo. */
export interface LaneInfo {
  lane: string;        // claude-cli | ollama-haiku | codex-cli | minimax | ...
  provider: string;    // claude | ollama | codex | minimax
  model: string;       // opus | qwen3:14b | gpt-5 | ...
  local: boolean;      // true iff the lane runs on the local Ollama box
}

/** One row of the tiering grid: a task type + the lane each sensitivity resolves
 *  to. `cells` is keyed by sensitivity (public|internal|confidential|MNPI). */
export interface LaneMatrixRow {
  taskType: string;                    // wire `task_type`
  cells: Record<string, LaneInfo | undefined>;   // keyed by sensitivity; the UI tolerates a missing cell
}

/** GET /api/routing/lane-matrix (G4) — the (task_type × sensitivity) → lane grid,
 *  swept live from shared.routing.pick_lane (single-source, drift-proof). */
export interface LaneMatrixResponse {
  tier: string;                        // bridge | enterprise — the tier the grid was computed for
  taskTypes: string[];                 // wire `task_types`
  sensitivities: string[];
  matrix: LaneMatrixRow[];
  lanes: Record<string, LaneInfo>;     // legend: every lane → provider/model/local
}

/** GET/POST /api/routing/plan-tier — the live plan tier + provenance (#plan-tier-toggle). */
export interface PlanTierState {
  tier: string;                        // bridge | enterprise (the LIVE tier)
  source: string;                      // 'operator' (persisted UI flip) | 'env-default'
  setBy: string | null;                // wire `set_by`
  setAt: string | null;                // wire `set_at`
}

/** One rung of a cloud lane's fallback ladder. Mirrors lane_status.py Rung. */
export interface LaneRung {
  rung: string;        // oauth-subprocess | anthropic-api | ollama-degrade | openai-api
  transport: string;   // human label
  state: string;       // available | unavailable | armed | absent | floor | not-wired
  detail: string;
}

export interface CloudLane {
  lane: string;        // claude | codex
  purpose: string;     // orchestration | analysis
  rungs: LaneRung[];
}

export interface SdkCredit {
  state: string;       // parked | configured
  env: string;
  detail: string;
}

/** GET /api/routing/lane-status (G1) — the per-lane cloud-dispatch fallback
 *  ladder + each rung's CONFIGURED state (not a runtime probe). */
export interface LaneStatusResponse {
  claude: CloudLane;
  codex: CloudLane;
  sdkCredit: SdkCredit;                 // wire `sdk_credit`
  note: string;
}

// ── Skill taxonomy catalog (#35 · TAXONOMY tab) ─────────────────────────────
// Bridge: GET /api/skills/taxonomy (routines/api/routes/skills_providers.py
// SkillTaxonomyRow + SkillTaxonomyResponse). Camelized by lib/api.ts (so
// `tile_label` → `tileLabel`, `cost_ceiling_tokens` → `costCeilingTokens`).
// Sourced verbatim from validated SKILL.md frontmatter — the catalog can't
// drift. The endpoint is NEW (added on routines feat/taxonomy-endpoint-35), so
// a bridge that hasn't restarted onto it returns 404; the tab falls back to the
// (always-live) providers matrix for the overlapping columns in that case.
export interface SkillTaxonomyRow {
  name: string;                  // the verb / registry key (frontmatter `name`)
  description: string;           // full frontmatter description (carries Triggers + Output)
  tileLabel: string;             // wire `tile_label`
  sensitivity: Sensitivity;
  workspaceScope: string;        // wire `workspace_scope` — project | bd | general | any
  lane: string;                  // v1 always "skill"
  version: string;
  costCeilingTokens: number;     // wire `cost_ceiling_tokens`
  costCeilingSeconds: number;    // wire `cost_ceiling_seconds`
  allowedTools: string[];        // wire `allowed_tools`
  // "Output destination" — declared write surface.
  vaultWrite: string[];          // wire `vault_write` — capabilities.vault_write globs
  capturesTarget?: string | null;   // wire `captures_target` — #76 captures_to_vault target
  capturesSection?: string | null;  // wire `captures_section`
  // Telemetry roll-up (null/0 until the skill fires).
  lastFire?: string | null;      // wire `last_fire` — ISO
  lastProvider?: string | null;  // wire `last_provider`
  costUsd: number;               // wire `cost_usd`
  calls: number;
}

export interface SkillTaxonomyResponse {
  skills: SkillTaxonomyRow[];
  composites: Record<string, unknown>[];  // empty in v1 (dir not on disk)
  crews: Record<string, unknown>[];        // empty in v1
  counts: Record<string, number>;          // { skills, composites, crews, sensitivity_<tier> }
  asOf: string;                            // wire `as_of`
}

/** PATCH /api/skills/{key}/provider body — snake_case on the wire. A partial
 *  patch MERGES over the existing sidecar entry (a temp-only patch preserves the
 *  existing provider/model). `clear:true` removes the entry (revert to
 *  frontmatter). `preferred_provider` vocabulary is {anthropic, openai,
 *  ollama-only, prefer_local} — "ollama-only"/"prefer_local" both map to the
 *  "ollama" allow-list key (ollama-only = fail-closed local; prefer_local =
 *  token-saving downgrade of a public/internal cloud pick). `preferred_model`
 *  is a cloud model alias {opus, sonnet, haiku, opus-1m}. Omit a field to leave
 *  it unchanged — NB the partial PATCH can SET preferred_model but cannot
 *  individually clear it (null === leave unchanged); use clear:true to drop the
 *  whole entry. */
export interface PatchSkillProviderBody {
  preferred_provider?: string;
  preferred_model?: string;
  llm_params?: { temperature?: number | null; max_tokens?: number | null };
  clear?: boolean;
}

// ── Operator sensitivity overrides (per-window) ────────────────────────────
// Bridge: GET/POST /api/sensitivity/overrides, POST .../{id}/close
// (#llm-routing-override, LLM-ROUTING-2026-06-02.md §5). The GET list returns
// only ACTIVE windows (expired/closed drop out server-side). MNPI is NOT a
// valid ceiling (refused at the API + storage layers). Powers the right-rail
// countdown panel.
export type OverrideCeiling = "public" | "internal" | "confidential";

export interface SensitivityOverride {
  id: string;
  skill: string;
  workspace: string;             // "project:DemoDeal" | "general:default" etc.
  provider: string;              // "anthropic" | "openai" | "ollama" | ...
  ceiling: string;               // public | internal | confidential
  openedAt: string;              // wire `opened_at` — ISO
  // wire `expires_at` — ISO countdown target, OR null for an until-closed window
  // (#llm-routing-postjune15 P2; no auto-expiry — drops on explicit close or the
  // server's 24h defense-in-depth hard cap measured from openedAt).
  expiresAt: string | null;
  justification: string;
  closedAt?: string | null;      // wire `closed_at`
  closedReason?: string | null;  // wire `closed_reason`
}

export interface ListOverridesResponse {
  overrides: SensitivityOverride[];
  // Server clock at list time — anchor the client countdown to this (correct
  // for client/server clock skew) rather than to the local clock alone.
  asOf: string;                  // wire `as_of`
}


// ── LBO run (bridge: POST /api/workflows/lbo + #63 suspend/resume) ─────────
// #lbo-dashboard-wiring 2026-06-09. Wire shape is snake_case verbatim (the
// workflow + skills routes are NOT camelized — cf. CompsBuildResult).

// A suspended-skill awaiting payload (the wrapper''s 202 reply) — also the row
// shape of GET /api/skills/suspended (which adds workspace/created fields).
export interface SkillAwaiting {
  status: "suspended";
  run_id: string;
  skill: string;
  prompt: string;
  options: LBOBoxField[] | null;   // for lbo: the boxes manifest, rendered verbatim
  resume_token: string;
  expires_at: string;
  resume_url: string;
}

export interface SuspendedSkill extends SkillAwaiting {
  workspace_type: string;
  workspace_name: string;
  sensitivity: string;
  created: string;
}

// One field of the deal-assumption boxes manifest (server-defined form).
export interface LBOBoxField {
  key: string;
  label: string;
  type: "number" | "int" | "date" | "text" | "select";
  unit?: string;                    // "m" | "x" | "dec" | "yrs"
  required?: boolean;
  default?: string | number | null;
  options?: Array<string | number>; // for selects
  help?: string;
  // #lbo-agent-leg Phase 2 (D4 provenance): set on fields the in-bridge
  // intake agent prefilled — `source` is the document location the value was
  // transcribed from; `provided_via` identifies the producer. Absent on
  // operator-/convention-defaulted fields. Rendered as a provenance line.
  source?: string;
  provided_via?: string;
  // Explicit suspension-stage marker stamped by the lbo-intake-agent route on
  // every option (codex slice-3 SEV-2): the modal dispatches clarify vs boxes
  // on THIS, never on prompt wording. Absent on plain /lbo manifests.
  stage?: "clarify" | "boxes";
}

export interface LBOIntakeRequest {
  mode: "intake";
  deal_name: string;
  workspace_type: "project" | "bd" | "general";
  workspace_name: string;
  workspace_sensitivity: "public" | "internal" | "confidential" | "MNPI";
  deal_context?: string;
  prefill?: Record<string, string | number>;
}

// #lbo-agent-leg Phase 2 — the governed in-bridge intake agent's fire payload.
export interface LBOAgentIntakeRequest {
  deal_name: string;
  workspace_type: "project" | "bd" | "general";
  workspace_name: string;
  workspace_sensitivity: "public" | "internal" | "confidential" | "MNPI";
  doc_paths: string[];
  deal_context?: string;
}

export interface LBOReturnsWire {
  irr_central_pct: number | null;
  moic_central_x: number | null;
  equity_cheque_m: number;
  hold_years: number;
}

export interface LBOHeadlineWire {
  ftev_m: number;
  entry_multiple: number;
  exit_multiple: number;
  tla_quantum_m: number;
  tlb_quantum_m: number;
  net_debt_at_close_m: number;
  sponsor_equity_m: number;
  management_equity_m: number;
  total_equity_m: number;
  stub_period: number;
}

export interface LBOValidationWire {
  engine_status: string;
  engine_rules_passed: boolean;
  sources_and_uses_ties: boolean;
}

export interface LBORunResult {
  ok: boolean;
  deal_name: string;
  run_id: string;
  output_xlsx_path: string;
  duration_ms: number;
  convergence_iters: number;
  returns: LBOReturnsWire;
  headline: LBOHeadlineWire;
  sensitivity: {
    irr_grid: Array<Array<number | null>>;
    entry_axis: number[];
    exit_axis: number[];
    summary_grid: Array<Array<string | null>>;
    moic_grid: null;
  };
  sources_and_uses: unknown[][];
  validation: LBOValidationWire;
  warnings: string[];
  citations: Array<Record<string, unknown>>;
}

// Resume can complete (LBORunResult) or re-suspend (SkillAwaiting).
export type LBOResumeResponse = LBORunResult | SkillAwaiting;

// ── Operator tab (#operator-tab) — /api/operator/config wire shapes ────────
// snake_case verbatim from the bridge (no camelize pass on this surface).

export interface OperatorFileInfo {
  path: string;
  exists: boolean;
  mtime: string | null;       // str(st_mtime_ns) — string on purpose (JS safe-int)
  mtime_iso: string | null;
}

export interface OperatorTickerRow { symbol: string; name?: string }
export interface OperatorMacroRow extends OperatorTickerRow {
  kind: "equity" | "index" | "commodity" | "rate" | "indicator";
}

export interface OperatorCoverageRow {
  name: string;
  sector?: string | null;
  sources: string[];
  query?: string | null;
  enabled?: boolean;
}

export interface OperatorSectorTree {
  sector: string;
  slug: string;
  tree: "full" | "partial" | "missing";
  note_exists: boolean;
}

export interface OperatorCredentialSummary {
  provider: string;
  kind: string;
  created: string;
  last_used: string | null;
  expires_at: string | null;
}

/** #operator-tab v2 — which copy of a known API key is effective.
 *  "store-over-env" = the encrypted store's key is in use AND an
 *  independent env (setx) copy also exists underneath. */
export interface OperatorKeyStatus {
  env_var: string;
  store: boolean;
  env: boolean;
  effective: "store" | "store-over-env" | "env" | "none";
}

export interface CredentialSummaryResponse {
  provider: string;
  kind: string;
  created: string;
  last_used: string | null;
  expires_at: string | null;
  metadata: Record<string, unknown>;
}

export interface OperatorConfigResponse {
  sections: {
    banners: {
      ticker_bar: OperatorTickerRow[];
      macro_bar: OperatorMacroRow[];
      issues: string[];
    };
    watchlist: { earnings_watchlist: OperatorTickerRow[]; issues: string[] };
    coverage: {
      coverage: OperatorCoverageRow[];
      synthesised: boolean;
      issues: string[];
    };
    sectors: {
      active_sectors: string[];
      trees: OperatorSectorTree[];
      orphan_trees: string[];
      scaffold_hint: string;
    };
    profile: {
      operator?: string;
      operator_slug?: string;
      qualifications?: string[];
      role_title?: string;
      role_firm?: string;
      issues?: string[];
    };
    credentials: {
      credentials: OperatorCredentialSummary[];
      credentials_error?: string;
      // #operator-tab v2 — per-known-provider effective key source.
      keys: Record<string, OperatorKeyStatus>;
      ollama: { reachable: boolean; version?: string; models?: string[]; error?: string };
      provider_overrides: { path: string; exists: boolean };
      cli_auth: string;
    };
  };
  files: Record<"tickers" | "earnings_watchlist" | "news_coverage" | "profile", OperatorFileInfo>;
  writable_sections: string[];
}

export interface PutOperatorSectionBody {
  expected_mtime: string | null;
  data: Record<string, unknown>;
}

export interface PutOperatorSectionResponse {
  ok: boolean;
  section: string;
  file: OperatorFileInfo;
}
