// Typed client for the FastAPI bridge at /api/* (proxied to 127.0.0.1:8765 in dev).
// All endpoints documented in routines/routines/api/.

import type {
  ActionsResponse, ActivityItem, AuditRun, RecallResponse, QuotesResponse,
  CompsResult, CompsBuildResult, CompsBuildBody, EquityResearchResult,
  DraftItem, DailyResponse, RecentDailyResponse, MorningBriefData, DailyDigestData,
  PendingProposalsResponse, ProposalContentResponse, ResolvedProposalsResponse,
  TickerBarResponse, MacroBarResponse,
  SchedulerJobsResponse, SchedulerPauseResponse, SchedulerResumeResponse,
  SchedulerRunNowResponse, SchedulerHistoryResponse,
  ToggleRequest, ToggleResponse,
  IssuesResponse,
  ChatRequest, ChatResponse, ChatHistoryResponse,
  ChatStreamStart, ChatStreamDelta, ChatStreamDone, ChatStreamError,
  ServerSession, SessionMode, WorkspaceType, Sensitivity,
  ProjectOverview, CreateWorkspaceBody, CreateWorkspaceResponse,
  ListWorkspacesResponse,
  RouteProposalBody, RouteProposalResponse,
  RejectProposalBody, RejectProposalResponse,
  SkipProposalBody, SkipProposalResponse,
  RequestRevisionBody, RequestRevisionResponse,
  LLMBurnSummary, PlansResponse,
  ListBudgetIncidentsResponse, BudgetIncident, AckBudgetIncidentBody,
  ListBudgetsResponse, CreateBudgetBody, BudgetPolicyRow,
  SkillsProvidersResponse, SkillProviderRow, PatchSkillProviderBody,
  CrewProvidersResponse, CrewProviderRow, PatchCrewProviderBody,
  AttestationDTO, ListAttestationsResponse, ChallengeResponse, GrantAttestationBody,
  LaneMatrixResponse, LaneStatusResponse, PlanTierState,
  SkillTaxonomyResponse,
  OperatorConfigResponse, PutOperatorSectionBody, PutOperatorSectionResponse,
  CredentialSummaryResponse,
  ListOverridesResponse, SensitivityOverride,
  LBOIntakeRequest, LBOAgentIntakeRequest, SkillAwaiting, SuspendedSkill, LBOResumeResponse,
  DealTrackerResult, CrewRunResponse, CrewManifestEntry, CrewRunRecord,
} from "../types";
import type { Message } from "../components/ChatCanvas";
import type { Session as ListSession } from "../components/SessionList";
import { newRunId } from "./runId";

const BASE = "/api";

/** #59-harness — header name the backend's RunIdMiddleware reads + echoes.
 *  Case-insensitive on the wire (Starlette normalises) but canonical casing
 *  matters for grep + DevTools recognition. */
const ANTON_RUN_ID_HEADER = "X-ANTON-Run-Id";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

/** #42 v2 — thrown by `projectChatStream` when the streaming endpoint isn't
 *  available on this bridge (route absent → 404/405, or the response isn't a
 *  `text/event-stream`). Signals the caller to fall back to the non-stream
 *  `projectChat` POST — NOT a genuine chat error (a 404 for an unknown project
 *  also lands here, and re-running via the non-stream path resurfaces it
 *  correctly). `status` is the HTTP status the stream attempt returned. */
class StreamUnavailableError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`chat streaming unavailable (status ${status})`);
    this.status = status;
    this.name = "StreamUnavailableError";
  }
}

/** #59-harness — distinct error class for the session-lock 409. Pairs with
 *  the backend `SessionLockBusy` middleware: detail.error === "session_lock_busy".
 *  Callers can branch on `sameRunIdRetry` — `true` means "this exact run is
 *  already in flight, poll don't double-fire"; `false` means "a different
 *  request holds the lock, back off + retry with a fresh id". Discriminated
 *  from business-conflict 409s (route/reject/revision already-exists) by the
 *  detail shape — only the lock middleware emits `pending_run_id`.
 *
 *  v1 surfaces this distinctly so future UX work (toast, retry policy) can
 *  branch; today the inbox-action callers still render it as a generic 409
 *  via formatError fallback. */
class SessionLockBusyError extends Error {
  readonly status = 409;
  readonly pendingRunId: string | null;
  readonly sameRunIdRetry: boolean;
  readonly humanMessage: string | null;
  readonly acquiredAgeSec: number | null;
  constructor(detail: Record<string, unknown>) {
    const human = typeof detail.human_message === "string" ? detail.human_message : null;
    super(human ?? "Session lock busy");
    this.name = "SessionLockBusyError";
    this.pendingRunId   = typeof detail.pending_run_id === "string" ? detail.pending_run_id : null;
    this.sameRunIdRetry = detail.same_run_id_retry === true;
    this.humanMessage   = human;
    this.acquiredAgeSec = typeof detail.acquired_age_sec === "number" ? detail.acquired_age_sec : null;
  }
}

function isSessionLockDetail(d: unknown): d is Record<string, unknown> {
  return (
    d !== null
    && typeof d === "object"
    && (d as Record<string, unknown>).error === "session_lock_busy"
  );
}

async function request<T>(
  path: string,
  init?: RequestInit & { runId?: string }
): Promise<T> {
  // Extract runId so it doesn't leak into the underlying fetch init.
  const { runId, headers: initHeaders, ...rest } = init ?? {};
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...(initHeaders as Record<string, string> | undefined ?? {}),
  };
  if (runId) headers[ANTON_RUN_ID_HEADER] = runId;

  const res = await fetch(`${BASE}${path}`, { ...rest, headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let parsed: unknown = null;
    try {
      parsed = await res.json();
      const body = parsed as { detail?: unknown };
      // FastAPI HTTPException nests under `detail`; the session-lock middleware
      // uses a structured-object detail.
      if (res.status === 409 && isSessionLockDetail(body?.detail)) {
        throw new SessionLockBusyError(body.detail as Record<string, unknown>);
      }
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch (e) {
      if (e instanceof SessionLockBusyError) throw e;
      /* response had no JSON body */
    }
    throw new ApiError(res.status, detail);
  }
  // 204 No Content (e.g. DELETE /budgets) has no body to parse.
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ── snake_case ↔ camelCase translators ─────────────────────────────────────
// The sessions bridge returns Pydantic field names verbatim (so a Message
// carries `duration_ms` alongside `runningText`). React layer uses camelCase
// throughout, so we recursively rewrite keys at the boundary. Values are
// untouched — strings, numbers, booleans, and arrays pass through.

function snakeToCamel(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, ch: string) => ch.toUpperCase());
}

function camelToSnake(key: string): string {
  return key.replace(/[A-Z]/g, (ch) => `_${ch.toLowerCase()}`);
}

function rekey<T>(input: unknown, transform: (k: string) => string): T {
  if (Array.isArray(input)) {
    return input.map((v) => rekey(v, transform)) as unknown as T;
  }
  if (input && typeof input === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(input as Record<string, unknown>)) {
      out[transform(k)] = rekey(v, transform);
    }
    return out as T;
  }
  return input as T;
}

const camelize = <T,>(o: unknown): T => rekey<T>(o, snakeToCamel);
const snakeify = <T,>(o: unknown): T => rekey<T>(o, camelToSnake);

// ── SSE frame parser (#42 v2) ───────────────────────────────────────────────
// One frame = lines until a blank line. We read the `event:` name and the
// (possibly multi-line) `data:` payload; `:`-prefixed comment/heartbeat lines
// are ignored. CRs are stripped upstream so only `\n` framing matters here.
function parseSseFrame(frame: string): { event: string; data: string } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;     // blank or comment/heartbeat
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length && event === "message") return null;
  return { event, data: dataLines.join("\n") };
}

// ── List-session mapper ────────────────────────────────────────────────────
// SessionList renders a presentational `Session` (workspaceTag, ago, running)
// — distinct from the raw server shape. Keep the component dumb; do the
// derivation here.

const WS_TAG: Record<WorkspaceType, string> = {
  project: "PRJ",
  bd: "BD",
  general: "GEN",
};

function relativeAgo(iso: string, now = new Date()): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const sec = Math.max(0, Math.round((now.getTime() - then) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.round(hr / 24);
  if (day === 1) return "yesterday";
  if (day < 7) return `${day}d`;
  const wk = Math.round(day / 7);
  return `${wk}w`;
}

export function mapServerToListSession(s: ServerSession): ListSession {
  return {
    id: s.id,
    workspaceTag: `${WS_TAG[s.workspaceType]} · ${s.workspaceName.toUpperCase()}`,
    title: s.title,
    ago: relativeAgo(s.lastActive),
    // "kind" = the session verb when present (comps-build / company-profile / …),
    // else the bare mode (chat). Surfaced in the left-rail row subline.
    kind: s.verb?.trim() || s.mode,
    messageCount: s.messageCount,
    archived: s.archived,
    pinned: s.pinned,
  };
}

// Bridge tags placeholder Anton messages via the `route` field (e.g.
// "UNWIRED · CLAUDE → opus") when the target lane isn't wired in yet — the
// body is a free-form notice, not a fixed prefix. Surface that as a
// presentational flag so ChatCanvas can dim the bubble.
function markUnwired(m: Message): Message {
  if (m.role === "anton" && typeof m.route === "string" && m.route.startsWith("UNWIRED")) {
    return { ...m, unwired: true };
  }
  return m;
}

export interface RecallRequest {
  query: string;
  limit?: number;
  synthesise?: boolean;
  project?: string;
  max_sensitivity?: "public" | "internal" | "confidential" | "MNPI";
}

// ── Chat document attachments (#chat-attach) ────────────────────────────────
/** The result of POST /api/sessions/{id}/attachments. The backend persists the
 *  raw file, extracts its text (truncating very large docs), and classifies its
 *  sensitivity. `text` is the extracted body the composer forwards on the next
 *  send; `truncated` flags that `chars` was capped. snake_case verbatim on the
 *  wire (NOT camelized — these are flat scalar keys with no compound names). */
export interface AttachmentResult {
  filename: string;
  saved_relpath: string;
  chars: number;
  truncated: boolean;
  text: string;
  sensitivity: Sensitivity;
}

/** Upload one document to a session (multipart/form-data, field `file`).
 *
 *  This is DELIBERATELY a raw `fetch` rather than the shared `request()`:
 *  `request()` forces `Content-Type: application/json`, which would clobber the
 *  multipart boundary the browser must set itself for a FormData body. We mirror
 *  `request()`'s wire conventions otherwise — same `${BASE}` prefix, JSON Accept,
 *  the `X-ANTON-Run-Id` header, and the same error-unwrapping (FastAPI nests its
 *  message under `detail`). No explicit Content-Type: the browser stamps
 *  `multipart/form-data; boundary=…` from the FormData body.
 *
 *  (This branch's `request()` does not attach credentials or a `Sec-Fetch-Site`
 *  header — the dev proxy + prod same-origin bridge serve `/api/*` same-origin,
 *  so cookies ride by default and no CSRF header is minted here. We mirror that
 *  exactly: no `credentials`/`Sec-Fetch-Site` override, only the run-id header.) */
async function uploadAttachment(sessionId: string, file: File): Promise<AttachmentResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(sessionId)}/attachments`, {
    method: "POST",
    // No Content-Type — the browser sets multipart/form-data + boundary.
    headers: { Accept: "application/json", [ANTON_RUN_ID_HEADER]: newRunId() },
    body: form,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch { /* response had no JSON body */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<AttachmentResult>;
}

export const api = {
  recall: (body: RecallRequest) =>
    request<RecallResponse>("/recall", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  recallIndex: (rebuild = false) =>
    request<{ status: string; pid?: number }>("/recall/index", {
      method: "POST",
      body: JSON.stringify({ rebuild }),
    }),

  sectorNewsRun: (sector?: string) =>
    request<{ status: string; pid?: number }>("/sector-news/run", {
      method: "POST",
      body: JSON.stringify({ sector: sector ?? null }),
    }),

  memoryPromoteRunAll: () =>
    request<{ status: string; pid?: number }>("/memory-promote/run-all", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  // #front-door — on-demand decay sweeps + lessons-suggest (formerly cron-only).
  // Workflow routes are snake_case verbatim (NOT camelized — like compsPull).
  actionsDecay: () =>
    request<{ status: string; run_id: string; counts: { overdue: number; stale: number; projects_scanned: number; projects_failed: number } }>(
      "/workflows/actions-decay",
      { method: "POST", body: JSON.stringify({ format: "json" }) },
    ),

  bdDecay: () =>
    request<{ status: string; run_id: string; counts: { scanned: number; stale: number; fresh: number; untracked: number } }>(
      "/workflows/bd-decay",
      { method: "POST", body: JSON.stringify({ format: "json" }) },
    ),

  lessonsSuggest: (project: string) =>
    request<{ status: string; run_id: string; bullets: string; suggestions: { slug: string; title: string; score: number; reason: string; wikilink: string }[]; counts: { total_entries: number; scored: number; returned: number } }>(
      "/workflows/lessons-suggest",
      { method: "POST", body: JSON.stringify({ project, limit: 10, format: "bullets" }) },
    ),

  /** POST /api/workflows/deal-tracker (#front-door). Extracts a precedent-deal
   *  row from pasted article `text` (url is provenance only, never fetched).
   *  dry_run previews the DealPreview without writing; the route 422s when no
   *  target company can be extracted, and reports skipped_duplicate when the
   *  deal is already tracked. snake_case verbatim (NOT camelized). */
  dealTracker: (body: { url: string; text: string; dry_run: boolean }) =>
    request<DealTrackerResult>("/workflows/deal-tracker", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** POST /api/crew/{verb}/run (#front-door) — dispatch a crew. 202 →
   *  {run_id, sse_url, poll_url}. Crews always run on the local subprocess lane;
   *  the bridge gates sensitivity (triage is MNPI-locked) before spawning.
   *  snake_case verbatim. */
  crewRun: (verb: string, body: {
    workspace: { type: string; name: string; sensitivity_tier: string };
    args?: Record<string, unknown>;
    session_id?: string;
  }) =>
    request<CrewRunResponse>(`/crew/${encodeURIComponent(verb)}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Consume a crew run's SSE channel (the server-absolute `sse_url` from
   *  crewRun — fetched verbatim, NOT via request()). The channel is sparse:
   *  `human_input_required` asks + the terminal `crew_completed` (plus
   *  `: keepalive` comments). `onEvent` fires per event; resolves on
   *  crew_completed (or when the stream ends). Mirrors projectChatStream. */
  crewEvents: async (
    sseUrl: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const res = await fetch(sseUrl, { headers: { Accept: "text/event-stream" }, signal });
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, `crew events stream unavailable (${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const parsed = parseSseFrame(frame);   // shared parser (multi-line data, keepalive skip)
        if (!parsed) continue;
        let payload: Record<string, unknown> = {};
        if (parsed.data) { try { payload = JSON.parse(parsed.data) as Record<string, unknown>; } catch { /* skip malformed frame */ } }
        onEvent(parsed.event, payload);
        if (parsed.event === "crew_completed") return;
      }
    }
    // EOF without a terminal crew_completed frame — the stream dropped before the
    // crew finished. Throw rather than resolve, so the caller never renders a
    // false "complete" (review HIGH).
    throw new Error("crew event stream closed before completion");
  },

  /** GET a crew run's assembled audit record (the server-absolute `poll_url`
   *  from crewRun — fetched verbatim, NOT via request()). The AUTHORITATIVE
   *  final status; the SSE crew_completed event is sparse. */
  crewRunRecord: (pollUrl: string) =>
    fetch(pollUrl, { headers: { Accept: "application/json" } }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, `crew run record unavailable (${r.status})`);
      return r.json() as Promise<CrewRunRecord>;
    }),

  /** POST /api/crew/runs/{run_id}/human-input — answer a crew's mid-run
   *  human-input ask (the `human_input_required` SSE event). BASE-prefixed
   *  `request()` (this path is `/api`-relative, unlike the verbatim
   *  `sse_url`/`poll_url`). The reply is a SIDE-CHANNEL POST — it does NOT go
   *  through the open SSE stream, which stays awaiting crew_completed. A 404
   *  (ApiError) means the ask is no longer pending (already answered / unknown
   *  id / timed out) — surface it, don't retry blindly. */
  crewHumanInput: (runId: string, body: { msg_id: string; response: string }) =>
    request<{ ok: boolean; run_id: string; msg_id: string }>(
      `/crew/runs/${encodeURIComponent(runId)}/human-input`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  /** GET /api/crew/manifest — the registered crews catalog (#35 taxonomy tab).
   *  snake_case verbatim (NOT camelized). */
  crewManifest: () =>
    request<{ crews: CrewManifestEntry[] }>("/crew/manifest"),

  // #6c-harness (2026-05-28) — api.projects() helper removed.
  // api.listWorkspaces("project") is the canonical project list source; it
  // returns the richer Workspace shape (inVault / inCorporateFinance /
  // sourceRoots) the dual-scan exposes. Callers that only need names
  // map via `.workspaces.map(w => w.name)`.

  auditRuns: (routine: string, limit = 25) =>
    request<{ runs: AuditRun[] }>(
      `/audit-runs?routine=${encodeURIComponent(routine)}&limit=${limit}`
    ),

  vaultPulse: (hours = 24, limit = 5) =>
    request<{ items: ActivityItem[] }>(
      `/vault-pulse?hours=${hours}&limit=${limit}`
    ),

  // ── Scheduler (#scheduler-panel) — the bridge-embedded APScheduler.
  //    jobs() is read-only (running=false ⇒ scheduler offline, not a 500).
  //    pause/resume/run-now are audited operator actions (the bridge persists
  //    durable pauses for cron-registry specs + logs each); history tails
  //    runs/scheduler.<id>.jsonl, latest first.
  schedulerJobs: () =>
    request<SchedulerJobsResponse>("/scheduler/jobs"),

  pauseSchedulerJob: (id: string) =>
    request<SchedulerPauseResponse>(
      `/scheduler/jobs/${encodeURIComponent(id)}/pause`,
      { method: "POST" },
    ),

  resumeSchedulerJob: (id: string) =>
    request<SchedulerResumeResponse>(
      `/scheduler/jobs/${encodeURIComponent(id)}/resume`,
      { method: "POST" },
    ),

  runSchedulerJobNow: (id: string) =>
    request<SchedulerRunNowResponse>(
      `/scheduler/jobs/${encodeURIComponent(id)}/run-now`,
      { method: "POST" },
    ),

  schedulerJobHistory: (id: string, limit = 10) =>
    request<SchedulerHistoryResponse>(
      `/scheduler/jobs/${encodeURIComponent(id)}/history?limit=${limit}`,
    ),

  marketsQuotes: (symbols: string[]) =>
    request<QuotesResponse>(
      `/markets/quotes?symbols=${encodeURIComponent(symbols.join(","))}`
    ),

  /** #operator-tab — GET /api/operator/config. snake_case verbatim (no
   *  camelize: the section payloads are round-tripped back into PUT). */
  operatorConfig: (signal?: AbortSignal) =>
    request<OperatorConfigResponse>("/operator/config", { signal }),

  /** #operator-tab — PUT /api/operator/config/{section}. expected_mtime is
   *  the string token from the last GET (null = file didn't exist); a 409
   *  means a mid-flight Obsidian edit — re-fetch, re-apply. */
  putOperatorConfig: (section: string, body: PutOperatorSectionBody) =>
    request<PutOperatorSectionResponse>(
      `/operator/config/${encodeURIComponent(section)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  /** #operator-tab v2 — PUT /api/credentials/{provider}: atomic upsert /
   *  ROTATE into the encrypted store (#25). The key travels once over
   *  loopback and is NEVER echoed back (response is a summary). */
  putCredential: (provider: string, apiKey: string) =>
    request<CredentialSummaryResponse>(
      `/credentials/${encodeURIComponent(provider)}`,
      {
        method: "PUT",
        body: JSON.stringify({ kind: "api_key", provider, api_key: apiKey }),
      },
    ),

  /** #operator-tab v2 — DELETE /api/credentials/{provider}. The bridge
   *  restores any pre-existing env copy for this process. */
  deleteCredential: (provider: string) =>
    request<undefined>(
      `/credentials/${encodeURIComponent(provider)}`,
      { method: "DELETE" },
    ),

  marketsHealth: () =>
    request<{ status: string; provider: string }>("/markets/health"),

  tickerBar: () => request<TickerBarResponse>("/markets/ticker-bar"),

  macroBar: () => request<MacroBarResponse>("/markets/macro-bar"),

  /** GET /api/telemetry/llm-burn — per-call LLM cost aggregator (VAULT
   *  Session I, 2026-05-25). Default window is last 24h. group_by defaults
   *  server-side to "provider"; pass "session" or "both" for the by_session
   *  drill-down. Wire keys are snake_case + the response shape camelizes
   *  cleanly through the existing translator. */
  llmBurn: async (opts?: {
    since?: string;
    until?: string;
    group_by?: "provider" | "session" | "both" | "workspace" | "all";
  }): Promise<LLMBurnSummary> => {
    const params = new URLSearchParams();
    if (opts?.since)    params.set("since", opts.since);
    if (opts?.until)    params.set("until", opts.until);
    if (opts?.group_by) params.set("group_by", opts.group_by);
    const qs = params.toString();
    const raw = await request<unknown>(`/telemetry/llm-burn${qs ? `?${qs}` : ""}`);
    const summary = camelize<LLMBurnSummary>(raw);
    // camelize() rewrites EVERY object key — including the by_workspace / by_session
    // MAP keys, which are DATA ("<type>:<name>" workspace / session id), NOT schema
    // field names. A name like "deal_2" would mangle to "deal2", so no raw-name-keyed
    // lookup (ContextRail's project rows, TknBudgetTab.usageInWs) would ever hit it.
    // Rebuild those two maps with VERBATIM keys but camelized values.
    const rawObj = (raw ?? {}) as Record<string, unknown>;
    if (rawObj.by_workspace && typeof rawObj.by_workspace === "object") {
      summary.byWorkspace = Object.fromEntries(
        Object.entries(rawObj.by_workspace as Record<string, unknown>)
          .map(([k, v]): [string, unknown] => [k, camelize(v)]),
      ) as unknown as LLMBurnSummary["byWorkspace"];
    }
    if (rawObj.by_session && typeof rawObj.by_session === "object") {
      summary.bySession = Object.fromEntries(
        Object.entries(rawObj.by_session as Record<string, unknown>)
          .map(([k, v]): [string, unknown] => [k, camelize(v)]),
      ) as unknown as LLMBurnSummary["bySession"];
    }
    return summary;
  },

  /** GET /api/usage/plans — per-provider plan-cap rows for LLMUsagePanel
   *  (#14b — Tier 1 sweep, 2026-05-27). Hardcoded three-row table v1:
   *  anthropic Max (5h messages) / openai Plus (3h messages) / m27 Standard
   *  (24h GBP). `used_pct` is a fraction 0.0-1.0+. Camelizes through the
   *  existing translator. */
  usagePlans: async (): Promise<PlansResponse> => {
    const raw = await request<unknown>("/usage/plans");
    return camelize<PlansResponse>(raw);
  },

  compsPull: (body: {
    symbol: string;
    peers_limit?: number;
    years?: number;
    write_note?: boolean;
  }) =>
    request<CompsResult>("/workflows/comps", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  equityResearch: (body: {
    symbol: string;
    years?: number;
    peers_limit?: number;
    news_days?: number;
    news_limit?: number;
    write_note?: boolean;
  }) =>
    request<EquityResearchResult>("/workflows/equity-research", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** POST /api/workflows/comps-build — one stage of the operator-gated comps
   *  pipeline (#21-comps). Body + response are snake_case verbatim (not
   *  camelized, like the other workflow routes). The modal threads the HMAC
   *  approval tokens from each StageResult into the next call's body. */
  compsBuild: (body: CompsBuildBody) =>
    request<CompsBuildResult>("/workflows/comps-build", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** POST /api/workflows/lbo in INTAKE mode (#lbo-dashboard-wiring): fires the
   *  #63 cooperative suspension and returns the 202 awaiting payload whose
   *  `options` carry the server-defined deal-assumption boxes manifest. The
   *  operator answers via `resumeSkill`. snake_case verbatim. */
  lboIntake: (body: LBOIntakeRequest) =>
    request<SkillAwaiting>("/workflows/lbo", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** POST /api/workflows/lbo-intake-agent (#lbo-agent-leg Phase 2): the
   *  governed in-bridge agent reads the deal docs (paths must sit under the
   *  skill's declared fs_roots), judges via the gated llm() — local by
   *  default; an operator #llm-routing-override window lifts judgment to the
   *  claude lane for that fire — and suspends: clarify questions first if it
   *  has any, then the standard boxes manifest with cited prefill. Same #63
   *  resume flow as lboIntake. snake_case verbatim. */
  lboAgentIntake: (body: LBOAgentIntakeRequest) =>
    request<SkillAwaiting>("/workflows/lbo-intake-agent", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** GET /api/skills/suspended — the "waiting on you" list (pending,
   *  non-expired suspensions, newest first). Used to pick an LBO intake back
   *  up after the modal was closed. */
  skillsSuspended: (workspaceType?: string) => {
    const params = new URLSearchParams();
    if (workspaceType) params.set("workspace_type", workspaceType);
    const qs = params.toString();
    return request<{ count: number; pending: SuspendedSkill[] }>(
      `/skills/suspended${qs ? `?${qs}` : ""}`,
    );
  },

  /** POST /api/skills/{run_id}/resume — deliver the operator's answer to a
   *  suspended skill. Completes (the skill's result) or re-suspends (another
   *  202 awaiting payload — e.g. boxes failed validation). 409 = stale token /
   *  already resumed; 410 = lapsed → re-fire the intake. */
  resumeSkill: (runId: string, body: { resume_token: string; input?: unknown }) =>
    request<LBOResumeResponse>(`/skills/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  drafts: (project?: string, limit = 50) => {
    const params = new URLSearchParams();
    if (project) params.set("project", project);
    params.set("limit", String(limit));
    return request<{ items: DraftItem[] }>(`/drafts?${params.toString()}`);
  },

  dailyToday: () => request<DailyResponse>("/daily/today"),

  /** GET /api/daily/recent — the most-recent daily notes (metadata only) for
   *  the Notes "recent" rail. Snake_case verbatim (no camelize), like
   *  `dailyToday`. `limit` is bounded 1..30 server-side. */
  dailyRecent: (limit = 6) =>
    request<RecentDailyResponse>(`/daily/recent?limit=${limit}`),

  morningBriefToday: (dateIso?: string) =>
    request<MorningBriefData>(
      dateIso ? `/morning-brief/today?date=${encodeURIComponent(dateIso)}` : "/morning-brief/today"
    ),

  dailyDigestToday: (dateIso?: string) =>
    request<DailyDigestData>(
      dateIso ? `/daily-digest/today?date=${encodeURIComponent(dateIso)}` : "/daily-digest/today"
    ),

  proposalsPending: () => request<PendingProposalsResponse>("/proposals/pending"),

  /** GET /api/proposals/{id}/content — the full markdown write-up for one
   *  proposal (#inbox-proposal-detail). Snake_case verbatim (no camelize). */
  proposalContent: (id: string) =>
    request<ProposalContentResponse>(`/proposals/${encodeURIComponent(id)}/content`),

  /** GET /api/proposals/resolved — recently-resolved proposals for the Inbox
   *  "Recently resolved" rail (#inbox-resolved-feed). Newest-first, snake_case. */
  proposalsResolved: (limit = 12) =>
    request<ResolvedProposalsResponse>(`/proposals/resolved?limit=${limit}`),

  // ── Proposal actions (Session G, 2026-05-25) — body keys stay snake_case
  // to match Pydantic; response shapes don't carry compound keys so no
  // camelize/snakeify roundtrip needed. #59-harness: every state-mutating
  // helper accepts an optional runId; mints one when absent.
  routeProposal: (id: string, body: RouteProposalBody, runId?: string) =>
    request<RouteProposalResponse>(`/proposals/${encodeURIComponent(id)}/route`, {
      method: "POST",
      runId: runId ?? newRunId(),
      body: JSON.stringify(body),
    }),

  rejectProposal: (id: string, body: RejectProposalBody, runId?: string) =>
    request<RejectProposalResponse>(`/proposals/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      runId: runId ?? newRunId(),
      body: JSON.stringify(body),
    }),

  skipProposal: (id: string, body: SkipProposalBody = {}, runId?: string) =>
    request<SkipProposalResponse>(`/proposals/${encodeURIComponent(id)}/skip`, {
      method: "POST",
      runId: runId ?? newRunId(),
      body: JSON.stringify(body),
    }),

  // #58-harness2 — POST /api/proposals/{id}/request-revision (#58 backend
  // shipped routines `51958ad`). Writes a `.revision.json` sidecar; pending
  // scanner excludes the proposal until the source routine re-fires. Server
  // 409 if a revision is already pending; 422 on empty/whitespace feedback.
  requestRevision: (id: string, body: RequestRevisionBody, runId?: string) =>
    request<RequestRevisionResponse>(`/proposals/${encodeURIComponent(id)}/request-revision`, {
      method: "POST",
      runId: runId ?? newRunId(),
      body: JSON.stringify(body),
    }),

  // Open Actions (live aggregator over Projects/<X>/**/*.md + Companies/<target>.md)
  projectActions: (project: string) =>
    request<ActionsResponse>(
      `/projects/${encodeURIComponent(project)}/actions`
    ),

  toggleAction: (project: string, body: ToggleRequest) =>
    request<ToggleResponse>(
      `/projects/${encodeURIComponent(project)}/actions/toggle`,
      { method: "POST", body: JSON.stringify(body) }
    ),

  // Issues register (#issues-register v2) — parses the deal's
  // `14 Issues & Outstanding.md` into typed issues for the grouped Open
  // Actions panel. 404 on non-vault projects; callers tolerate failure
  // (panel degrades to ungrouped actions on an older bridge).
  projectIssues: (project: string) =>
    request<IssuesResponse>(
      `/projects/${encodeURIComponent(project)}/issues`
    ),

  // ── Project chat (#42 · per-deal conversational memory) ───────────────────
  // POST runs one turn: project-filtered recall → local LLM synthesis →
  // atomic-append of BOTH the user + assistant turns to Projects/{code}/_chat.md
  // → returns the assistant turn. Wire shape is snake_case verbatim (no
  // camelize) — sibling to projectActions/toggleAction. Errors: 404 unknown
  // project · 400 empty message · 409 (plain-string detail) corrupt/unreadable
  // _chat.md — surface that detail inline · 500 generic synthesis failure.
  projectChat: (code: string, body: ChatRequest) =>
    request<ChatResponse>(`/projects/${encodeURIComponent(code)}/chat`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // GET reads stored history without writing (panel mount). Returns the
  // wrapped { project, turns } shape. `load_history` is fail-soft server-side,
  // so this degrades to an empty thread on a broken log rather than 409-ing;
  // only 404 (unknown project) is a hard error here.
  projectChatHistory: (code: string) =>
    request<ChatHistoryResponse>(
      `/projects/${encodeURIComponent(code)}/chat/history`
    ),

  // POST /api/projects/{code}/chat/stream — SSE variant (#42 v2). Consumes the
  // `text/event-stream` body and invokes `handlers` per event as they arrive:
  // start → delta* → (done | error). Resolves once the stream ends (incl. a
  // server `error` event — that's a terminal outcome, not a throw). Throws:
  //   • StreamUnavailableError — endpoint absent / non-SSE response (→ caller
  //     falls back to the non-stream `projectChat`).
  //   • DOMException "AbortError" — the passed `signal` aborted (deal switch /
  //     unmount); the caller treats the send as abandoned.
  //   • a generic Error — a network fault on the stream itself.
  // Wire payloads are snake_case verbatim (no camelize), like `projectChat`.
  projectChatStream: async (
    code: string,
    body: ChatRequest,
    handlers: {
      onStart?: (e: ChatStreamStart) => void;
      onDelta: (e: ChatStreamDelta) => void;
      onDone: (e: ChatStreamDone) => void;
      onError: (e: ChatStreamError) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> => {
    const res = await fetch(`${BASE}/projects/${encodeURIComponent(code)}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });

    const ct = res.headers.get("content-type") ?? "";
    if (!ct.includes("text/event-stream") || !res.body) {
      // Route/method absent (old bridge → 404/405) or any 200 non-SSE body →
      // the streaming endpoint isn't usable here; fall back to the one-shot POST.
      if (res.status === 404 || res.status === 405 || res.ok) {
        throw new StreamUnavailableError(res.status);
      }
      // A real error status with a (JSON) body — streaming IS implemented but the
      // request failed BEFORE the stream opened (e.g. 400/409/500; nothing was
      // persisted). Surface it directly rather than silently replaying the send
      // via the non-stream POST (codex SEV-2 #3).
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = (await res.json()) as { detail?: unknown };
        if (typeof body?.detail === "string") detail = body.detail;
        else if (body?.detail) detail = JSON.stringify(body.detail);
      } catch { /* no JSON body */ }
      throw new ApiError(res.status, detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    // First terminal event wins; everything after it is ignored so a stray
    // post-terminal frame can't append a turn after rollback (codex SEV-2 #1).
    let terminal: "done" | "error" | null = null;

    const dispatch = (event: string, data: string): void => {
      if (terminal) return;
      let payload: Record<string, unknown> = {};
      if (data) {
        try { payload = JSON.parse(data) as Record<string, unknown>; }
        catch { return; }   // skip a malformed (non-terminal) frame
      }
      switch (event) {
        case "start": handlers.onStart?.({ type: "start", project: String(payload.project ?? code) }); break;
        case "delta": handlers.onDelta({ type: "delta", text: String(payload.text ?? "") }); break;
        // Stamp the discriminant ourselves — the wire payload omits `type`
        // (codex SEV-3 #4) — so the object honours the ChatStream* union.
        case "done":  terminal = "done";  handlers.onDone({ ...payload, type: "done" } as unknown as ChatStreamDone); break;
        case "error": terminal = "error"; handlers.onError({ ...payload, type: "error" } as unknown as ChatStreamError); break;
        default: break;     // unknown event name — ignore (forward-compat)
      }
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        // Strip CRs so only `\n\n` framing matters (some proxies inject CRLF).
        buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const parsed = parseSseFrame(frame);
          if (parsed) dispatch(parsed.event, parsed.data);
        }
        if (terminal) break;   // stop reading once the turn is resolved
      }
      // Flush any pending multi-byte remainder, then a trailing frame not
      // terminated by a blank line (a terminal event may omit the final blank).
      buffer += decoder.decode().replace(/\r/g, "");
      const tail = parseSseFrame(buffer);
      if (tail) dispatch(tail.event, tail.data);
    } finally {
      reader.releaseLock();
    }

    // Stream closed with NO done/error (dropped connection, or a malformed
    // terminal frame that was skipped). Nothing is confirmed persisted, so raise
    // — the caller rolls the optimistic turn back instead of leaving it stuck
    // with no reply + an unlocked composer (codex SEV-2 #2).
    if (!terminal) {
      throw new Error("chat stream ended without a terminal event");
    }
  },

  // ── Sessions (OUTSTANDING.md ## CONTRACTS · sessions, locked 2026-05-24) ──

  createSession: async (body: {
    workspace_type: WorkspaceType;
    workspace_name: string;
    mode?: SessionMode;
    verb?: string;
    title?: string;
  }): Promise<ServerSession> => {
    const raw = await request<unknown>("/sessions", {
      method: "POST",
      body: JSON.stringify({ mode: "chat", ...body }),
    });
    return camelize<ServerSession>(raw);
  },

  listSessions: async (params?: {
    workspace_type?: WorkspaceType;
    workspace_name?: string;
    archived?: boolean;
  }): Promise<{ sessions: ServerSession[] }> => {
    const qs = new URLSearchParams();
    if (params?.workspace_type) qs.set("workspace_type", params.workspace_type);
    if (params?.workspace_name) qs.set("workspace_name", params.workspace_name);
    if (params?.archived !== undefined) qs.set("archived", String(params.archived));
    const q = qs.toString();
    const raw = await request<unknown>(`/sessions${q ? `?${q}` : ""}`);
    return camelize<{ sessions: ServerSession[] }>(raw);
  },

  getSession: async (id: string): Promise<ServerSession> => {
    const raw = await request<unknown>(`/sessions/${encodeURIComponent(id)}`);
    return camelize<ServerSession>(raw);
  },

  getSessionMessages: async (id: string): Promise<{ messages: Message[] }> => {
    const raw = await request<unknown>(`/sessions/${encodeURIComponent(id)}/messages`);
    const out = camelize<{ messages: Message[] }>(raw);
    return { messages: out.messages.map(markUnwired) };
  },

  /** #chat-attach — POST /api/sessions/{id}/attachments (multipart, field
   *  `file`). Returns the extracted-text payload the composer forwards on send.
   *  Raw-fetch (NOT request()) so the browser owns the multipart boundary —
   *  see `uploadAttachment` above. */
  uploadAttachment,

  /** v1: returns one JSON envelope `{user_message, anton_message}`. SSE
   *  streaming is documented in the contract but lands behind OUTSTANDING #2.
   *  `sensitivity_override` is enforced server-side — must be same-tier-or-
   *  stricter than the workspace default; otherwise the call returns 403.
   *
   *  #59-harness — accepts optional `runId` (mints one when absent). Retries
   *  of the same logical send MUST thread the same runId so the backend's
   *  per-session lock recognises the second call as a same-id retry instead
   *  of different-id contention. */
  sendMessage: async (
    id: string,
    text: string,
    opts?: {
      sensitivity_override?: Sensitivity;
      runId?: string;
      model_override?: "minimax";
      /** #chat-attach — extracted-text payloads for any attached documents,
       *  forwarded alongside the turn text. Each entry is `{filename, text}`
       *  (the text the upload step extracted). Omitted entirely when empty. */
      attachments?: { filename: string; text: string }[];
    },
  ): Promise<{
    userMessage: Message;
    antonMessage: Message;
    route: string;
    lane: string;
    sensitivity: Sensitivity;
  }> => {
    const raw = await request<unknown>(`/sessions/${encodeURIComponent(id)}/messages`, {
      method: "POST",
      runId: opts?.runId ?? newRunId(),
      body: JSON.stringify({
        text,
        ...(opts?.sensitivity_override ? { sensitivity_override: opts.sensitivity_override } : {}),
        ...(opts?.model_override ? { model_override: opts.model_override } : {}),
        ...(opts?.attachments && opts.attachments.length
          ? { attachments: opts.attachments }
          : {}),
      }),
    });
    const out = camelize<{
      userMessage: Message;
      antonMessage: Message;
      route: string;
      lane: string;
      sensitivity: Sensitivity;
    }>(raw);
    return { ...out, antonMessage: markUnwired(out.antonMessage) };
  },

  archiveSession: (id: string) =>
    request<{ ok: boolean }>(`/sessions/${encodeURIComponent(id)}/archive`, { method: "POST" }),

  /** POST /api/sessions/{id}/rename (#session-ops) — set the session title. The
   *  title is stripped + length-validated server-side (422 on blank). */
  renameSession: async (id: string, title: string): Promise<ServerSession> => {
    const raw = await request<unknown>(`/sessions/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    return camelize<ServerSession>(raw);
  },

  /** POST /api/sessions/{id}/pin (#session-ops) — pin/unpin; pinned sorts first. */
  pinSession: async (id: string, pinned: boolean): Promise<ServerSession> => {
    const raw = await request<unknown>(`/sessions/${encodeURIComponent(id)}/pin`, {
      method: "POST",
      body: JSON.stringify({ pinned }),
    });
    return camelize<ServerSession>(raw);
  },

  /** DELETE /api/sessions/{id} (#session-ops) — hard-delete (irreversible; the
   *  raw transcript is kept server-side as audit). */
  deleteSession: (id: string) =>
    request<{ ok: boolean; id: string }>(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // ── Project overview (OUTSTANDING ## CONTRACTS · project overview, #11) ──
  projectOverview: async (name: string): Promise<ProjectOverview> => {
    const raw = await request<unknown>(`/projects/${encodeURIComponent(name)}/overview`);
    return camelize<ProjectOverview>(raw);
  },

  // ── Budget gate (#57) ────────────────────────────────────────────────────
  // Default GET returns rows that block the gate (open + acknowledged_paused);
  // pass include_acknowledged=true for full history. Ack body is snake_case on
  // the wire; server 422s on blank comment or raise_cap with new_cap_usd ≤ cap.
  budgetIncidents: async (opts?: { include_acknowledged?: boolean }): Promise<ListBudgetIncidentsResponse> => {
    const qs = opts?.include_acknowledged ? "?include_acknowledged=true" : "";
    const raw = await request<unknown>(`/budgets/incidents${qs}`);
    return camelize<ListBudgetIncidentsResponse>(raw);
  },

  ackBudgetIncident: async (id: string, body: AckBudgetIncidentBody, runId?: string): Promise<BudgetIncident> => {
    const raw = await request<unknown>(`/budgets/incidents/${encodeURIComponent(id)}/ack`, {
      method: "POST",
      runId: runId ?? newRunId(),
      body: JSON.stringify(body),
    });
    return camelize<BudgetIncident>(raw);
  },

  // ── Budget policies (TKN BUDGET tab) ──────────────────────────────────────
  /** GET /api/budgets — policies + current spend/tokens per scope. */
  listBudgets: async (): Promise<ListBudgetsResponse> => {
    const raw = await request<unknown>("/budgets");
    return camelize<ListBudgetsResponse>(raw);
  },

  /** POST /api/budgets — create/update a policy (upsert by scope). Body is
   *  snake_case on the wire. A token-only budget passes cap_usd:0 + cap_tokens. */
  upsertBudget: async (body: CreateBudgetBody): Promise<BudgetPolicyRow> => {
    const raw = await request<unknown>("/budgets", {
      method: "POST",
      body: JSON.stringify(body),
    });
    return camelize<BudgetPolicyRow>(raw);
  },

  /** DELETE /api/budgets?kind=&a=&b= — remove a policy by scope (204). */
  deleteBudget: (scope: { kind: string; a?: string | null; b?: string | null }): Promise<void> => {
    const qs = new URLSearchParams({ kind: scope.kind });
    if (scope.a) qs.set("a", scope.a);
    if (scope.b) qs.set("b", scope.b);
    return request<void>(`/budgets?${qs.toString()}`, { method: "DELETE" });
  },

  // ── Workspaces (#5 + #6) ─────────────────────────────────────────────────
  /** POST /api/workspaces — server returns 201 with the created workspace +
   *  resolved filesystem/vault paths. 409 on name collision (any configured
   *  root); 422 on invalid name; 500 on missing scaffold template. */
  createWorkspace: async (body: CreateWorkspaceBody): Promise<CreateWorkspaceResponse> => {
    const raw = await request<unknown>("/workspaces", {
      method: "POST",
      body: JSON.stringify(body),
    });
    return camelize<CreateWorkspaceResponse>(raw);
  },

  /** GET /api/workspaces — optionally filter by type. Items are returned
   *  sorted by last_touched DESC (server-side). */
  listWorkspaces: async (type?: WorkspaceType): Promise<ListWorkspacesResponse> => {
    const path = type ? `/workspaces?type=${encodeURIComponent(type)}` : "/workspaces";
    const raw = await request<unknown>(path);
    return camelize<ListWorkspacesResponse>(raw);
  },

  // ── Tier 2 skill providers (#llm-routing-tier-2) ───────────────────────────
  /** GET /api/skills/providers — the per-skill provider matrix (loopback-only).
   *  Camelizes through the existing translator, including the nested raw
   *  `override` sidecar dict. */
  skillsProviders: async (signal?: AbortSignal): Promise<SkillsProvidersResponse> => {
    const raw = await request<unknown>(
      "/skills/providers",
      signal ? { signal } : undefined,
    );
    return camelize<SkillsProvidersResponse>(raw);
  },

  /** GET /api/skills/taxonomy — the verb catalog for the TAXONOMY tab (#35).
   *  One row per registered skill, sourced from validated SKILL.md frontmatter.
   *  Camelizes through the existing translator. NEW endpoint (routines
   *  feat/taxonomy-endpoint-35) — a bridge that predates it returns 404, which
   *  the caller catches to fall back to the always-live providers matrix. */
  skillsTaxonomy: async (signal?: AbortSignal): Promise<SkillTaxonomyResponse> => {
    const raw = await request<unknown>(
      "/skills/taxonomy",
      signal ? { signal } : undefined,
    );
    return camelize<SkillTaxonomyResponse>(raw);
  },

  /** PATCH /api/skills/{key}/provider — write or clear the operator sidecar.
   *  Body stays snake_case verbatim (nested llm_params). Returns the fresh row.
   *  On 422 the bridge puts its SPECIFIC reason in `detail` (→ ApiError.message,
   *  e.g. "…confidential data never leaves the local box…") — surface it inline,
   *  don't generic-error it. 404 = unknown skill. */
  patchSkillProvider: async (key: string, body: PatchSkillProviderBody): Promise<SkillProviderRow> => {
    const raw = await request<unknown>(`/skills/${encodeURIComponent(key)}/provider`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    return camelize<SkillProviderRow>(raw);
  },

  /** GET /api/crew/providers — the per-crew cloud-promotion matrix
   *  (#crew-cloud-promotion, loopback-only). Camelized (incl. the nested raw
   *  `override` + `promotedRoles`). NEW endpoint — a bridge that predates it 404s,
   *  and the Crews section is then simply absent (graceful: the panel still renders
   *  its other routing readouts; the section returns once the bridge is restarted). */
  crewProviders: async (signal?: AbortSignal): Promise<CrewProvidersResponse> => {
    const raw = await request<unknown>("/crew/providers", signal ? { signal } : undefined);
    return camelize<CrewProvidersResponse>(raw);
  },

  /** PATCH /api/crew/{verb}/provider — write or clear a crew (or per-role) cloud
   *  promotion. Body snake_case verbatim. On 422 the bridge puts its reason in
   *  `detail` (e.g. a locked-crew refusal) → surface it inline. 404 = unknown crew. */
  patchCrewProvider: async (verb: string, body: PatchCrewProviderBody): Promise<CrewProviderRow> => {
    const raw = await request<unknown>(`/crew/${encodeURIComponent(verb)}/provider`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    return camelize<CrewProviderRow>(raw);
  },

  // ── MNPI cloud-attestations (#crew-cloud-promotion Phase C / P5) ────────────
  /** GET /api/mnpi/attestations — the currently-active per-provider attestations
   *  (not revoked, not expired). Loopback-only; camelized. A bridge that predates
   *  the endpoint 404s and the panel renders its empty/absent state (graceful). */
  mnpiAttestations: async (signal?: AbortSignal): Promise<ListAttestationsResponse> => {
    const raw = await request<unknown>("/mnpi/attestations", signal ? { signal } : undefined);
    return camelize<ListAttestationsResponse>(raw);
  },

  /** POST /api/mnpi/attestations/challenge — mint a single-use confirmation nonce
   *  (F-8 CSRF defence-in-depth). The grant must include the nonce; a headless
   *  CSRF page can call this but can't READ the (cross-origin) response. */
  mnpiChallenge: async (): Promise<ChallengeResponse> => {
    const raw = await request<unknown>("/mnpi/attestations/challenge", { method: "POST" });
    return camelize<ChallengeResponse>(raw);
  },

  /** POST /api/mnpi/attestations — grant a per-provider MNPI cloud-attestation
   *  (the most sensitive operator action; relaxes #no-mnpi-to-cloud for one
   *  provider). Body snake_case verbatim incl. the challenge nonce. 422 = a
   *  missing protection / bad duration (surface `detail` inline); 403 = a
   *  missing/expired/used nonce. */
  grantMnpiAttestation: async (body: GrantAttestationBody): Promise<AttestationDTO> => {
    const raw = await request<unknown>("/mnpi/attestations", {
      method: "POST",
      body: JSON.stringify(body),
    });
    return camelize<AttestationDTO>(raw);
  },

  /** POST /api/mnpi/attestations/{id}/revoke — revoke an attestation early. MNPI
   *  for that provider returns to the local-only floor immediately. 404 = unknown
   *  / already revoked. */
  revokeMnpiAttestation: async (id: string): Promise<AttestationDTO> => {
    const raw = await request<unknown>(
      `/mnpi/attestations/${encodeURIComponent(id)}/revoke`,
      { method: "POST" },
    );
    return camelize<AttestationDTO>(raw);
  },

  // ── LLM-routing posture (#llm-routing-postjune15 Mission B) ─────────────────
  /** GET /api/routing/lane-matrix (G4) — the (task_type × sensitivity) → lane
   *  grid, swept live from pick_lane (single-source). Loopback-only; camelized. */
  laneMatrix: async (signal?: AbortSignal): Promise<LaneMatrixResponse> => {
    const raw = await request<unknown>(
      "/routing/lane-matrix",
      signal ? { signal } : undefined,
    );
    return camelize<LaneMatrixResponse>(raw);
  },

  /** GET /api/routing/lane-status (G1) — the per-lane cloud-dispatch fallback
   *  ladder + each rung's configured state. Loopback-only; camelized. */
  laneStatus: async (signal?: AbortSignal): Promise<LaneStatusResponse> => {
    const raw = await request<unknown>(
      "/routing/lane-status",
      signal ? { signal } : undefined,
    );
    return camelize<LaneStatusResponse>(raw);
  },

  /** GET /api/routing/plan-tier (#plan-tier-toggle) — the live plan tier +
   *  provenance (who flipped it / when, or env-default). Loopback-only. */
  planTier: async (signal?: AbortSignal): Promise<PlanTierState> => {
    const raw = await request<unknown>(
      "/routing/plan-tier",
      signal ? { signal } : undefined,
    );
    return camelize<PlanTierState>(raw);
  },

  /** POST /api/routing/plan-tier (#plan-tier-toggle) — flip the LIVE plan tier.
   *  A confidentiality-boundary action: mints a single-use nonce, then flips —
   *  passing acknowledge_cloud_routing (required true to lift to enterprise, as
   *  that routes confidential material to cloud). The flip is live (no restart)
   *  + persisted. 403 = bad/expired nonce; 422 = enterprise without the ack. */
  setPlanTier: async (body: {
    tier: "bridge" | "enterprise";
    setBy: string;
    acknowledgeCloudRouting: boolean;
  }): Promise<PlanTierState> => {
    const ch = await request<unknown>("/routing/plan-tier/challenge", { method: "POST" });
    const nonce = camelize<ChallengeResponse>(ch).confirmationNonce;
    const raw = await request<unknown>("/routing/plan-tier", {
      method: "POST",
      body: JSON.stringify({
        tier: body.tier,
        set_by: body.setBy,
        acknowledge_cloud_routing: body.acknowledgeCloudRouting,
        confirmation_nonce: nonce,
      }),
    });
    return camelize<PlanTierState>(raw);
  },

  // ── Operator sensitivity overrides (#llm-routing-override) ──────────────────
  /** GET /api/sensitivity/overrides — active windows + the server clock (`asOf`)
   *  so the countdown can correct for client/server skew. Accepts an AbortSignal
   *  so the panel can cancel an in-flight poll on a focus-triggered refresh. */
  sensitivityOverrides: async (signal?: AbortSignal): Promise<ListOverridesResponse> => {
    const raw = await request<unknown>(
      "/sensitivity/overrides",
      signal ? { signal } : undefined,
    );
    return camelize<ListOverridesResponse>(raw);
  },

  /** POST /api/sensitivity/overrides/{id}/close — close a window early. Returns
   *  the closed override. 404 when already closed / expired / unknown (treat as
   *  "already gone" and just refresh the list). */
  closeSensitivityOverride: async (id: string): Promise<SensitivityOverride> => {
    const raw = await request<unknown>(
      `/sensitivity/overrides/${encodeURIComponent(id)}/close`,
      { method: "POST" },
    );
    return camelize<SensitivityOverride>(raw);
  },
};

// Re-exports — make boundary helpers visible to consumers that don't want to
// reach into the module body. `snakeify` is here for symmetry; the only POST
// body that needs it lives in `sendMessage` and uses explicit shapes already.
export { camelize, snakeify };

export { ApiError, SessionLockBusyError, StreamUnavailableError, ANTON_RUN_ID_HEADER };
