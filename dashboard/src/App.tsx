import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TopHeader }            from "./components/TopHeader";
import { SparkTicker }          from "./components/SparkTicker";
import { MacroTicker }          from "./components/MacroTicker";
import { type TabKey }          from "./components/MainTabs";
import { NavSidebar, type NavKey } from "./components/shell/NavSidebar";
import { WorkspaceSwitcher }     from "./components/shell/WorkspaceSwitcher";
import { WorkspacePickerModal } from "./components/WorkspacePickerModal";
import { SessionList } from "./components/SessionList";
import { ContextRail }         from "./components/ContextRail";
import { ProjectChatPanel }     from "./components/ProjectChatPanel";
import { ChatCanvas, type Attachment, type Message } from "./components/ChatCanvas";
import { WorkflowDrawer }       from "./components/WorkflowDrawer";
import { CollapsedSessions }    from "./components/CollapsedSessions";
import { CollapsedCoPanel }     from "./components/CollapsedCoPanel";
import { LiveModelCoPanel }     from "./components/LiveModelCoPanel";
import { useDeskLayout, useMediaQuery } from "./lib/useMediaQuery";
import { SensitivityOverridesPanel } from "./components/SensitivityOverridesPanel";
import { BudgetAckModal }       from "./components/BudgetAckModal";
import { CompsBuildModal }      from "./components/CompsBuildModal";
import { LBORunModal }          from "./components/LBORunModal";
import { DealTrackerModal }     from "./components/DealTrackerModal";
import { CrewTriageModal }      from "./components/CrewTriageModal";
import { CommandModal }         from "./components/CommandModal";
import { VaultTab }             from "./components/VaultTab";
import { DailyTab }             from "./components/DailyTab";
import { RunsTab }              from "./components/RunsTab";
import { DraftsTab }            from "./components/DraftsTab";
import { RecallTab }            from "./components/RecallTab";
import { NewsTab }              from "./components/NewsTab";
import { ActivityTab }          from "./components/ActivityTab";
import { InboxTab }             from "./components/InboxTab";
import { TknBudgetTab }         from "./components/TknBudgetTab";
import { SkillsProvidersTab }   from "./components/SkillsProvidersTab";
import { RoutingTab }           from "./components/RoutingTab";
import { TaxonomyTab }          from "./components/TaxonomyTab";
import { OperatorTab, hasUnsavedOperatorEdits } from "./components/OperatorTab";
import { api, ApiError, mapServerToListSession } from "./lib/api";
import { cn } from "./lib/cn";
import { partitionSessionsByWorkspace, pickActiveSession } from "./lib/sessions";
import { loadLastSession, saveLastSession } from "./lib/lastSession";
import { runSkillToMessage } from "./lib/skillMappers";
import { WIRED } from "./lib/wiring";
import type {
  WorkflowKey, ProjectOverview, WorkspaceListItem, LLMBurnSummary,
  PendingProposalsResponse, BudgetIncident, PlansResponse, ServerSession,
  Sensitivity,
} from "./types";


// ── v5 chat backbone — Phase 2 (2026-05-24) ─────────────────────────────────
// Sessions list + ChatCanvas now consume the live bridge sessions store
// (OUTSTANDING.md ## CONTRACTS · sessions). +NEW button is still a no-op —
// session creation lands with Session B (#4).

type WorkspaceType = "project" | "bd" | "general";

// Short tag shown in the top section heading (mirrors lib/api WS_TAG).
const WS_TAG_LABEL: Record<WorkspaceType, string> = { project: "PRJ", bd: "BD", general: "GEN" };

// 3-day calendar: today + next 2 days. Real wire is MS Graph OAuth2.
const WEEK_DAYS = [
  { initial: "F", date: 23, today: true, events: [
    { text: "Client meeting 11:00", flag: true },
    { text: "Counsel 15:00", flag: true },
  ]},
  { initial: "S", date: 24, events: [{ text: "Personal" }] },
  { initial: "S", date: 25, events: [{ text: "Bid 1 prep" }] },
];

// Build the workspace picker's switchable list from the live workspace lists,
// most-recently-touched first. (Replaces the old hardcoded PICKER_RECENT.)
function buildPickerWorkspaces(
  projects: WorkspaceListItem[], bds: WorkspaceListItem[], generals: WorkspaceListItem[],
): { type: WorkspaceType; name: string; age: string }[] {
  return [...projects, ...bds, ...generals]
    .slice()
    .sort((a, b) => (b.lastTouched ?? "").localeCompare(a.lastTouched ?? ""))
    .map((w) => ({ type: w.type, name: w.name, age: relativeAge(w.lastTouched) }));
}

// Relative age from an ISO timestamp, e.g. "2h ago" / "yesterday" / "3 days".
function relativeAge(iso: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days`;
  return `${Math.round(days / 7)}w`;
}

const nowHHMM = () => new Date().toISOString().slice(11, 16);

// ── Desk status one-liner phases (brief item 1) ─────────────────────────────
// A plain chat turn (App.handleSend) is a single POST with no chat-phase SSE
// from the bridge (streaming is OUTSTANDING #2 — see api.sendMessage), so we
// DERIVE a timed phase sequence client-side from the EXPECTED lane (the same
// project/bd→local-Ollama vs general→cloud rule pick_lane uses; cloud is
// Claude unless MiniMax is forced). Presentation only — no fabricated backend
// events. The sequence advances on a timer and HOLDS on the last phase until the
// real answer lands; ChatCanvas's StatusLine renders the dot + text + elapsed.
function chatPhases(workspaceType: WorkspaceType, model: "default" | "minimax"): string[] {
  const local = workspaceType !== "general";
  const lane = local
    ? "local Ollama"
    : model === "minimax" ? "cloud · MiniMax" : "cloud · Claude";
  return [
    "Thinking…",
    `Routing · ${lane}`,
    // Deal/BD chats are vault-grounded; a general cloud chat isn't, so it skips
    // the "reading sources" beat.
    ...(local ? ["Reading vault context…"] : []),
    "Iterating…",
    "Drafting the answer…",
  ];
}

// Each tile carries a one-line `desc` (the palette sub-line, Paper 37Q-0).
// Wired-skill descs paraphrase the skill's own SKILL.md `description`; planned/
// unwired tiles describe what the workflow will do (honest, not overstated).
const WORKFLOW_SECTIONS = [
  {
    title: "Research",
    tiles: [
      { label: "Company profile", key: "company-profile", pinned: true, kbd: "Ctrl+1", desc: "Public snapshot · 5y metrics · peers · news" },
      { label: "Market snapshot", key: "market-snapshot",  desc: "Current market & index snapshot" },
      { label: "Sector read",     key: "sector-read",      desc: "Sector news & trend read" },
      { label: "Comps pull",      key: "comps-pull",      pinned: true, kbd: "Ctrl+3", desc: "Quick-look current public trading multiples" },
      { label: "Precedents",      key: "precedents-pull",  desc: "Precedent M&A transaction multiples" },
      { label: "Deal tracker",    key: "deal-tracker-add", desc: "Log an M&A deal from a news article" },
    ],
  },
  {
    title: "Meetings",
    tiles: [
      { label: "Build agenda",      key: "build-agenda",     desc: "Assemble a meeting agenda from context" },
      { label: "Pre-read pack",     key: "pre-read-pack",    desc: "Pre-meeting research & talking points" },
      { label: "Pre-call Q&A",      key: "pre-call-qa",      desc: "Pre-call prep: recall + research + gaps" },
      { label: "Post-call cleanup", key: "post-call-cleanup", desc: "Capture call outcomes & action items" },
    ],
  },
  {
    title: "Valuation",
    tiles: [
      { label: "DCF run",       key: "dcf-run",        disabled: true, desc: "Discounted cash flow valuation model" },
      { label: "LBO run",       key: "lbo-run",        active: true,   desc: "LBO model · debt sizing · IRR grid" },
      { label: "Sensitivity",   key: "sensitivity",    pinned: true, kbd: "↵", desc: "Multi-axis valuation sensitivity grid" },
      { label: "3-statement",   key: "three-statement", desc: "3-statement model (P&L · BS · CF)" },
      { label: "Football field",key: "ff",             pinned: true, kbd: "Ctrl+2", desc: "Blended valuation football field" },
      { label: "Audit model",   key: "audit-model",    desc: "Validate model logic & assumptions" },
    ],
  },
  {
    title: "Vault & Ops",
    tiles: [
      { label: "Recall query",   key: "recall-query",   pinned: true, kbd: "Ctrl+5", desc: "Hybrid vault retrieval · BM25 + vectors" },
      { label: "Promote memory", key: "promote-memory", desc: "Promote an insight to the lessons register" },
      { label: "Reindex",        key: "reindex",        desc: "Rebuild the vault search indexes" },
      { label: "Newsletter run", key: "newsletter-run", desc: "Curated sector-news briefing" },
      { label: "HiNotes status", key: "meeting-notes-sync", desc: "Sync HiNotes meeting notes to the vault" },
      { label: "Actions decay",  key: "actions-decay",  desc: "Surface overdue & stale project actions" },
      { label: "BD decay",       key: "bd-decay",       desc: "Flag BD contacts past their decay threshold" },
      { label: "Lessons suggest",key: "lessons-suggest", desc: "Match prior-deal lessons to a project" },
    ],
  },
  {
    title: "Transaction materials",
    fullWidth: true,
    cols: 6,
    tiles: [
      { label: "Proposal",       key: "proposal",  desc: "Engagement / investment proposal draft" },
      { label: "Teaser",         key: "teaser",    desc: "Anonymised one-page deal teaser" },
      { label: "CIM draft",      key: "cim-draft", desc: "CIM draft · financials + market context" },
      { label: "Buyer list",     key: "buyer-list", desc: "Strategic buyer universe & scoring" },
      { label: "NDAs",           key: "ndas",      desc: "NDA drafting & restriction check" },
      { label: "IC memo ↑",      key: "ic-memo",   pinned: true, kbd: "Ctrl+4", desc: "IC memo · thesis · valuation · risks" },
    ],
  },
];

// #3b — drawer-tile-with-arg. WIRED skills that need a free-text argument: the
// value is the slash stub drafted into the composer when a tile is clicked with
// an empty composer (instead of firing argless → an immediate "type X first"
// miss), AND the prefix stripped back off when the operator completes it and
// re-clicks (so "/comps AAPL" fires with arg "AAPL"). Grows as needs-arg skills
// get wired; unwired tiles just surface their "not yet wired" stub, so they need
// no entry here.
const WORKFLOW_DRAFT: Partial<Record<WorkflowKey, string>> = {
  "recall-query":    "/recall ",
  "comps-pull":      "/comps ",
  "company-profile": "/profile ",
  "lessons-suggest": "/lessons ",   // #front-door — needs a project/sector arg
};

// #front-door — crews are a separate dispatch lane (subprocess), NOT
// WorkflowKeys. Rendered as their own drawer section; onDrawerFire branches on
// CREW_VERBS → fireCrew() instead of the skill fire()/runSkillToMessage path.
const CREW_SECTION = {
  title: "Crews",
  fullWidth: true,        // span the full drawer width + 6-col tile grid, aligned with "Transaction materials"
  cols: 6,
  tiles: [
    { label: "Triage (CIM)", key: "triage",  desc: "Multi-agent first-pass triage of a CIM" },
    { label: "Explore",      key: "explore", desc: "Multi-agent open-ended research" },
    { label: "Debate",       key: "debate",  desc: "Bull vs bear multi-agent debate" },
    { label: "Digest",       key: "digest",  desc: "Multi-agent document ingest & digest" },
  ],
};
const CREW_VERBS = new Set<string>(["triage", "explore", "debate", "digest"]);

export default function App() {
  const [tab, setTab]               = useState<TabKey>("agent");
  // #redesign Phase 3 — the 5-nav left sidebar drives `nav` (its source of
  // truth); `tab` still keys the existing per-screen body switch. A nav leaf
  // maps to a TabKey via navToTab(); recall/news have no tab body yet and render
  // placeholders gated on `nav`.
  const [nav, setNav]               = useState<NavKey>("desk");
  // #3b — composer draft lifted out of ChatCanvas so fire()/the drawer can read
  // it as the skill argument (composer-draft → fire()). `composerFocus` is bumped
  // to focus the textarea after a needs-arg tile drafts a command into it.
  const [draft, setDraft]           = useState("");
  // #chat-attach — composer attachments lifted to App and keyed PER SESSION.
  // Each ChatCanvas instance is keyed by session id and gets `attachmentsBySession
  // [sid] ?? []` plus a functional `onAttachmentsChange` bound to THAT sid, so an
  // upload that resolves AFTER a session switch patches its OWN session's bucket
  // (invisible to the now-active session) — structurally preventing a §5.2
  // cross-deal text leak. (A flat array shared across sessions could not.)
  const [attachmentsBySession, setAttachmentsBySession] =
    useState<Record<string, Attachment[]>>({});
  const [composerFocus, setComposerFocus] = useState(0);
  const [, setActive]               = useState<WorkflowKey>("recall-query");
  const [cmdOpen, setCmdOpen]       = useState(false);
  const [compsBuildOpen, setCompsBuildOpen] = useState(false);
  const [lboOpen, setLboOpen] = useState(false);
  const [dealTrackerOpen, setDealTrackerOpen] = useState(false);
  const [triageOpen, setTriageOpen] = useState(false);
  // #front-door — track in-flight crew SSE streams; abort on unmount so an
  // abandoned crew's stream doesn't linger to the server's ~600s TTL (review SEV-2).
  const crewAborts = useRef<Set<AbortController>>(new Set());
  useEffect(() => {
    const aborts = crewAborts.current;
    return () => { aborts.forEach((a) => a.abort()); };
  }, []);
  const [projects, setProjects]     = useState<string[]>([]);
  // Right-rail project-detail slot — Open actions ↔ Chat (#42). Tab-swap rather
  // than stacking so the rail stays bounded; ProjectPanel above stays visible.
  // `chatMounted` lazy-mounts the chat panel on first open, then it stays mounted
  // (CSS-hidden when Open actions is active) so a typed draft / in-flight send +
  // its error surfacing survive a tab swap (codex SEV-2).
  const [railDetail, setRailDetail] = useState<"actions" | "chat">("actions");
  const [chatMounted, setChatMounted] = useState(false);
  // #42 v2 Feature A — Cmd-K `/chat` focus signal. Bumped each time the `/chat`
  // command routes to the chat tab, so ProjectChatPanel focuses its composer.
  const [chatFocusSignal, setChatFocusSignal] = useState(0);

  // v5 workspace + session state
  // #restore-last-session — restore the last workspace + session from localStorage
  // so opening/refreshing ANTON lands you back where you were (not the hard-coded
  // default workspace).
  const [workspace, setWorkspace]   = useState<{ type: WorkspaceType; name: string }>(
    () => loadLastSession()?.workspace ?? { type: "project", name: "Project-Apex" },
  );
  const [pickerOpen, setPickerOpen] = useState(false);
  // Workflow palette overlay (v2: a `/`-triggered overlay, replacing the old
  // bottom drawer). Opened by "/" on an empty composer or ⌘E; closed by Esc /
  // backdrop / firing a workflow.
  const [wfOpen, setWfOpen] = useState(false);
  // Responsive Desk layout — compact (13″ → collapsed sessions strip) / wide /
  // ultra (55″ → + Live-Model co-panel). `sessionsExpanded` is the 13″ peek.
  const deskLayout = useDeskLayout();
  // Brief item 4 — the sessions-rail + Live-Model co-panel collapse is a 220ms
  // width-slide; reduced-motion falls back to the instant swap. One flag drives
  // both rails (and the chevron rotation).
  const reduceMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const [sessionsExpanded, setSessionsExpanded] = useState(false);
  // 55″ Live-Model co-panel is a drawer — collapsed by default, expand on demand.
  const [coPanelExpanded, setCoPanelExpanded] = useState(false);
  const [pickerInitialType, setPickerInitialType] = useState<WorkspaceType>("project");
  const [allSessions, setAllSessions] = useState<ServerSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string>(() => loadLastSession()?.sessionId ?? "");
  // True once the sessions list has been fetched at least once — gates the
  // auto-select effect so it can't wipe a restored selection before the list
  // arrives (#restore-last-session).
  const sessionsFetchedRef = useRef(false);
  // #minimax-chat-model — operator-selected chat model. "default" = the normal
  // sensitivity-routed lane; "minimax" forces the MiniMax cloud model for the turn.
  const [chatModel, setChatModel] = useState<"default" | "minimax">("default");
  const [messages, setMessages]     = useState<Message[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [sending, setSending]       = useState(false);

  // Project overview (right-rail tile) — Phase 2 part 4 (#12)
  // Project overview — feeds the ContextRail "Project" card (Paper A5-0 card 2).
  const [projectOverview, setProjectOverview] = useState<ProjectOverview | null>(null);
  const [, setOverviewLoading]   = useState(false);
  const [, setOverviewError]     = useState<string | null>(null);

  // Live workspaces lists (#6b) — fed into WorkspaceSelector dropdowns
  const [projectWorkspaces, setProjectWorkspaces] = useState<WorkspaceListItem[]>([]);
  const [bdWorkspaces, setBdWorkspaces]           = useState<WorkspaceListItem[]>([]);
  const [generalWorkspaces, setGeneralWorkspaces] = useState<WorkspaceListItem[]>([]);

  // LLM burn telemetry — Phase 2 part 6 (#14)
  const [llmBurn, setLlmBurn]               = useState<LLMBurnSummary | null>(null);
  const [, setLlmBurnLoading] = useState(false);
  const [, setLlmBurnError]     = useState<string | null>(null);

  // Plan-cap usage — #14b. Three per-provider rows (anthropic Max / openai
  // Plus / m27 Standard) from /api/usage/plans. Sibling cadence to llmBurn
  // (60s poll); errors surface inline in the panel header.
  const [plans, setPlans]               = useState<PlansResponse | null>(null);
  const [, setPlansLoading] = useState(false);
  const [, setPlansError]     = useState<string | null>(null);

  // Pending proposals — lifted from TopHeader + InboxTab (#7b Session F).
  // App owns the fetch + 2-min poll; chips and tab consume props. Action
  // handlers in InboxTab call `refreshProposals` so the chip count updates
  // immediately rather than lagging the next poll tick.
  const [proposals, setProposals]               = useState<PendingProposalsResponse | null>(null);
  const [proposalsLoading, setProposalsLoading] = useState(false);
  const [proposalsError, setProposalsError]     = useState<string | null>(null);

  // Budget incidents — #57 dashboard surface. Default GET returns rows that
  // currently BLOCK the gate (status open + acknowledged_paused). When the
  // list is non-empty, the right-rail BurnRatePanel is REPLACED by a red
  // BudgetBlockedBanner; clicking Acknowledge opens BudgetAckModal which
  // calls back through `refreshIncidents` on success. Tighter 60s poll than
  // proposals (cost-safety surface — find out fast).
  const [openIncidents, setOpenIncidents] = useState<BudgetIncident[]>([]);
  const [ackTarget, setAckTarget]         = useState<BudgetIncident | null>(null);

  // ⌘K / Ctrl-K to open the command palette anywhere on the page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdOpen((x) => !x);
      }
      // ⌘E / Ctrl-E — open the workflow palette (the `/` overlay's power-user shortcut).
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "e") {
        e.preventDefault();
        setWfOpen((x) => !x);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // One-shot projects fetch (Cmd-K filter dropdown still uses it). #6c-harness:
  // migrated from the deprecated /api/projects list endpoint to the dual-scan
  // /api/workspaces?type=project; map to names for the downstream string[] state.
  useEffect(() => {
    let cancelled = false;
    api.listWorkspaces("project")
      .then((r) => { if (!cancelled) setProjects(r.workspaces.map((w) => w.name)); })
      .catch(() => { /* bridge offline */ });
    return () => { cancelled = true; };
  }, []);

  // Sessions list — the left rail now shows TWO sections (selected project/BD
  // on top, General below), so we fetch the full list unfiltered and split it
  // client-side rather than scoping the query to the selected workspace. Still
  // re-fetched on workspace change to pick up sessions created elsewhere.
  useEffect(() => {
    let cancelled = false;
    setSessionsLoading(true);
    api.listSessions({ archived: false })
      .then((r) => { if (!cancelled) setAllSessions(r.sessions); })
      .catch(() => { if (!cancelled) setAllSessions([]); })
      .finally(() => { sessionsFetchedRef.current = true; if (!cancelled) setSessionsLoading(false); });
    return () => { cancelled = true; };
  }, [workspace.type, workspace.name]);

  // Split into the two left-rail sections (pure derive in lib/sessions). Top =
  // the selected project/BD workspace's sessions; bottom = all General ones.
  const partitioned = useMemo(
    () => partitionSessionsByWorkspace(allSessions, workspace),
    [allSessions, workspace.type, workspace.name],
  );
  const topSessions     = useMemo(() => partitioned.top.map(mapServerToListSession), [partitioned]);
  const generalSessions = useMemo(() => partitioned.general.map(mapServerToListSession), [partitioned]);

  // Auto-select on (re)load + workspace switch + delete: the active session must
  // belong to the SELECTED workspace's scope (its own sessions for a project/BD,
  // the General section for a general workspace), so the chat body never shows a
  // session from a different workspace than the header. Keeps the current pick if
  // still in scope; else the most-recent in-scope session; else "" (start screen).
  // Never cross-falls into General from inside a project (#session-workspace-sync).
  useEffect(() => {
    // Don't disturb a restored selection until the sessions list has loaded once
    // — otherwise the empty initial list would reset the restored id to "".
    if (!sessionsFetchedRef.current) return;
    setActiveSessionId((cur) => pickActiveSession(partitioned, workspace, cur));
  }, [partitioned, workspace]);

  // #restore-last-session — persist the active session + workspace so the next
  // open/refresh restores it (the lazy initial state + the ref-gated auto-select
  // above do the restore). Storage-safe; runs on every selection/workspace change.
  useEffect(() => {
    saveLastSession(activeSessionId, workspace);
  }, [activeSessionId, workspace]);

  // Project overview — refetch whenever the project workspace changes.
  // Non-project workspaces (BD / general) clear the overview so the panel
  // falls back to its placeholder state. 404 → user-friendly error in the
  // panel header, not a crash. #12b polish: clear stale data BEFORE the
  // fetch starts so the user never sees old project data flash through.
  useEffect(() => {
    if (workspace.type !== "project") {
      setProjectOverview(null);
      setOverviewError(null);
      setOverviewLoading(false);
      return;
    }
    let cancelled = false;
    setProjectOverview(null);     // #12b — clear stale before fetch
    setOverviewLoading(true);
    setOverviewError(null);
    api.projectOverview(workspace.name)
      .then((o) => { if (!cancelled) setProjectOverview(o); })
      .catch((e) => {
        if (cancelled) return;
        const msg = e instanceof ApiError
          ? (e.status === 404 ? "project not found in vault" : `${e.status}: ${e.message}`)
          : e instanceof Error ? e.message : "Unknown error";
        setOverviewError(msg);
        setProjectOverview(null);
      })
      .finally(() => { if (!cancelled) setOverviewLoading(false); });
    return () => { cancelled = true; };
  }, [workspace.type, workspace.name]);

  // Workspaces lists — fetched once on mount, refreshed by handleCreateWorkspace.
  // Stable callback so children + handlers can trigger a targeted refresh
  // without re-creating effect dependencies on every render.
  const refreshWorkspaces = useCallback(async (type?: WorkspaceType) => {
    try {
      if (!type || type === "project") {
        const r = await api.listWorkspaces("project");
        setProjectWorkspaces(r.workspaces);
      }
      if (!type || type === "bd") {
        const r = await api.listWorkspaces("bd");
        setBdWorkspaces(r.workspaces);
      }
      if (!type || type === "general") {
        const r = await api.listWorkspaces("general");
        setGeneralWorkspaces(r.workspaces);
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("listWorkspaces failed", e);
    }
  }, []);

  useEffect(() => { void refreshWorkspaces(); }, [refreshWorkspaces]);

  // LLM burn — refetch on mount + every 60s. Window defaults to the
  // server's "last 24h"; group_by=provider gives the per-provider rollup
  // the BurnRatePanel needs. Errors surface inline in the panel header
  // (don't crash the dashboard if telemetry fails).
  const refreshBurn = useCallback(async () => {
    setLlmBurnLoading(true);
    try {
      // group_by:"all" populates BOTH byProvider (nav-rail footer + Cost section)
      // AND byWorkspace (the enriched Cost rail's per-project row, brief item 3).
      // Same endpoint + cadence — just a wider grouping, no extra call.
      const r = await api.llmBurn({ group_by: "all" });
      setLlmBurn(r);
      setLlmBurnError(null);
    } catch (e) {
      setLlmBurnError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setLlmBurnLoading(false);
    }
  }, []);
  useEffect(() => {
    void refreshBurn();
    const id = window.setInterval(refreshBurn, 60_000);
    return () => window.clearInterval(id);
  }, [refreshBurn]);

  // Plan-cap usage — same cadence as burn. Independent state so a transient
  // plans-endpoint blip doesn't reset the burn snapshot (and vice versa).
  const refreshPlans = useCallback(async () => {
    setPlansLoading(true);
    try {
      const r = await api.usagePlans();
      setPlans(r);
      setPlansError(null);
    } catch (e) {
      setPlansError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setPlansLoading(false);
    }
  }, []);
  useEffect(() => {
    void refreshPlans();
    const id = window.setInterval(refreshPlans, 60_000);
    return () => window.clearInterval(id);
  }, [refreshPlans]);

  // Pending proposals — mount + 2-min poll. InboxTab calls back here after
  // every action so the REVIEW chip and InboxTab stay in lockstep.
  const refreshProposals = useCallback(async () => {
    setProposalsLoading(true);
    try {
      const r = await api.proposalsPending();
      setProposals(r);
      setProposalsError(null);
    } catch (e) {
      setProposalsError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setProposalsLoading(false);
    }
  }, []);
  useEffect(() => {
    void refreshProposals();
    const id = window.setInterval(refreshProposals, 120_000);
    return () => window.clearInterval(id);
  }, [refreshProposals]);

  // Budget incidents — mount + 60s poll. Errors don't crash the dashboard;
  // we just leave the previous snapshot in place so a transient bridge blip
  // doesn't cause the banner to flicker off.
  const refreshIncidents = useCallback(async () => {
    try {
      const r = await api.budgetIncidents();
      setOpenIncidents(r.incidents);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("budgetIncidents fetch failed", e);
    }
  }, []);
  useEffect(() => {
    void refreshIncidents();
    const id = window.setInterval(refreshIncidents, 60_000);
    return () => window.clearInterval(id);
  }, [refreshIncidents]);

  // Messages — load the full thread whenever the active session changes.
  // The bridge returns the full ordered list (no pagination in v1).
  useEffect(() => {
    // #chat-attach — no per-switch clear needed: attachments are keyed PER
    // SESSION (attachmentsBySession), so the prior session's bucket simply stays
    // its own and is never visible to the now-active session. A stale in-flight
    // upload resolving after the switch patches its OWN bucket, not this one.
    if (!activeSessionId) { setMessages([]); return; }
    let cancelled = false;
    setMessagesLoading(true);
    api.getSessionMessages(activeSessionId)
      .then((r) => {
        if (cancelled) return;
        // api.getSessionMessages already runs `markUnwired` on each row,
        // so the bubbles render with their `unwired` flag set.
        setMessages(r.messages);
      })
      .catch((e) => {
        if (cancelled) return;
        setMessages([]);
        // eslint-disable-next-line no-console
        console.warn("Failed to load messages for session", activeSessionId, e);
      })
      .finally(() => { if (!cancelled) setMessagesLoading(false); });
    return () => { cancelled = true; };
  }, [activeSessionId]);

  // Workflow drawer / Cmd-K → skill dispatch. Inserts an optimistic Anton
  // "running" placeholder into the active session and swaps it for the real
  // result (or an error/unwired bubble) when the bridge responds. Mappers
  // live in `lib/skillMappers.ts` so this stays UI-shaped.
  //
  // In-flight + session switch: the result lands in the session it was fired
  // from (we capture activeSessionId at fire-time). If the user navigates
  // away before the call resolves, the placeholder is silently dropped from
  // client state (v1; persists server-side once #22 dispatcher lands).
  const fire = async (key: WorkflowKey, promptText?: string) => {
    setActive(key);
    // comps-build is a multi-stage operator-gated flow, not a one-shot skill —
    // open its modal instead of inserting a running chat bubble.
    if (key === "comps-build") { setCompsBuildOpen(true); return; }
    // lbo-run is the #63 suspend/resume intake (#lbo-dashboard-wiring) — same
    // modal-first pattern; the result renders in-modal + lands in RunsTab via
    // the bridge audit, so no chat bubble/mapper here either.
    if (key === "lbo-run") { setLboOpen(true); return; }
    // deal-tracker-add opens a paste modal (article text required + a dry-run
    // preview of the extracted deal) rather than firing argless — #front-door.
    if (key === "deal-tracker-add") { setDealTrackerOpen(true); return; }
    const targetSessionId = activeSessionId;
    if (!targetSessionId) {
      // eslint-disable-next-line no-console
      console.warn("fire() called with no active session; ignored", key);
      return;
    }
    const placeholderId = `placeholder-${crypto.randomUUID()}`;
    const placeholder: Message = {
      id: placeholderId,
      role: "anton",
      who: "ANTON",
      time: nowHHMM(),
      running: true,
      runningText: prettyName(key),
      lane: "skill",
    };
    // Append placeholder only if we're still on the target session — if the
    // user already switched, there's no view to insert into right now.
    setMessages((prev) =>
      activeSessionId === targetSessionId ? [...prev, placeholder] : prev,
    );

    // Swap the placeholder for a finalised Message. If the user switched away,
    // the result is dropped (v1). Future: persist server-side so it shows on
    // hydration when they switch back.
    const finalize = (replacement: Message) => {
      setMessages((prev) => {
        // Use the latest active session at resolve-time, not at fire-time —
        // the closure captures the current value via React state.
        if (prev.some((m) => m.id === placeholderId)) {
          return prev.map((m) => (m.id === placeholderId ? { ...replacement, id: placeholderId, time: nowHHMM() } : m));
        }
        // Placeholder is gone — user switched sessions and the messages list
        // was replaced by hydration. Drop the result (logged for #22 follow-on).
        // eslint-disable-next-line no-console
        console.info(`Skill ${key} result dropped — placeholder no longer in view (session ${targetSessionId})`);
        return prev;
      });
    };

    if (!WIRED.includes(key)) {
      finalize({
        id: placeholderId,
        role: "anton",
        who: "ANTON",
        time: nowHHMM(),
        body: `"${prettyName(key)}" is not yet wired.`,
        unwired: true,
      });
      return;
    }

    try {
      const result = await runSkillToMessage(key, promptText, draft);
      finalize(result);
    } catch (e) {
      const msg = e instanceof ApiError
        ? `${e.status}: ${e.message}`
        : e instanceof Error ? e.message : "Unknown error";
      finalize({
        id: placeholderId,
        role: "anton",
        who: "ANTON",
        time: nowHHMM(),
        body: `Bridge call failed — ${msg}.`,
        failed: true,
      });
    }
  };

  // #3b — drawer-tile-with-arg flow. A tile that needs an argument drafts the
  // command into the composer (composer-draft) when it's empty, instead of
  // firing argless; once the operator has typed the arg, clicking fires with it.
  //   • modal skills (comps-build / lbo-run) → straight to fire() (it opens the modal).
  //   • needs-arg + empty composer → draft the slash stub in + focus, DON'T fire.
  //   • otherwise → fire with the composer text as the argument (slash stub, if
  //     the operator completed the drafted command, is stripped to the bare arg).
  const handleDrawerFire = (key: WorkflowKey) => {
    const stub = WORKFLOW_DRAFT[key];
    // Tiles without a needs-arg entry — modal skills (comps-build / lbo-run),
    // no-arg skills, and unwired tiles — fire as before and NEVER touch the
    // composer draft (so an unrelated tile can't discard unsent chat text).
    if (!stub) { void fire(key); return; }

    const stubVerb = stub.trim();          // e.g. "/comps"
    const typed = draft.trim();
    // Empty composer, OR only the bare verb drafted in (the operator clicked the
    // tile again without completing it) → (re)draft the stub + focus, rather than
    // firing argless into an immediate "type X first" miss.
    if (typed === "" || typed.toLowerCase() === stubVerb.toLowerCase()) {
      setDraft(stub);
      setComposerFocus((n) => n + 1);
      return;
    }
    // Strip the drafted slash command back to the bare argument, but ONLY on a
    // verb + whitespace boundary ("/comps AAPL" → "AAPL", incl. tabs); otherwise
    // the whole composer text is the argument (so "/compsAAPL" isn't mis-stripped).
    const rest = typed.slice(stubVerb.length);
    const arg = typed.toLowerCase().startsWith(stubVerb.toLowerCase()) && /^\s/.test(rest)
      ? rest.trim()
      : typed;
    setDraft("");                          // composer text consumed as the arg
    void fire(key, arg);
  };

  // Cmd-K `/chat [project]` → route to the right-rail Project chat tab (#42 v2
  // Feature A). Optionally switch to the named deal first, force the agent tab
  // (the rail only exists there), open + lazy-mount the chat tab, then bump the
  // focus signal so the panel auto-focuses its composer. `project` is already
  // resolved against the known list by CommandModal (undefined → current deal).
  // #front-door — crew dispatch. Crews aren't WorkflowKeys (separate subprocess
  // lane), so they bypass fire()/runSkillToMessage. POST → a lane:"crew" running
  // bubble; the SSE channel is sparse (human_input_required + the terminal
  // crew_completed — no per-role deltas), so we finalize from crew_completed and
  // point at the Runs audit (crewRunId) for the role-by-role record.
  const fireCrew = async (verb: string, args: Record<string, unknown> = {}) => {
    const targetSessionId = activeSessionId;
    if (!targetSessionId) {
      // eslint-disable-next-line no-console
      console.warn("fireCrew() with no active session; ignored", verb);
      return;
    }
    const placeholderId = `placeholder-${crypto.randomUUID()}`;
    // Workspace sensitivity the crew inherits (triage is MNPI-locked server-side
    // regardless); project/bd default confidential, general internal — all local.
    const tier = workspace.type === "general" ? "internal" : "confidential";
    setMessages((prev) =>
      activeSessionId === targetSessionId
        ? [...prev, {
            id: placeholderId, role: "anton", who: "ANTON", time: nowHHMM(),
            running: true, runningText: `${prettyCrew(verb)} crew · starting…`, lane: "crew",
          } as Message]
        : prev,
    );
    const finalize = (replacement: Message) => {
      setMessages((prev) =>
        prev.some((m) => m.id === placeholderId)
          ? prev.map((m) => (m.id === placeholderId ? { ...replacement, id: placeholderId, time: nowHHMM() } : m))
          : prev,
      );
    };
    try {
      const run = await api.crewRun(verb, {
        workspace: { type: workspace.type, name: workspace.name, sensitivity_tier: tier },
        args,
        session_id: targetSessionId,
      });
      setMessages((prev) => prev.map((m) =>
        m.id === placeholderId
          ? { ...m, crewRunId: run.run_id, runningText: `${prettyCrew(verb)} crew · running · ${run.run_id.slice(0, 8)}` }
          : m,
      ));
      const ac = new AbortController();
      crewAborts.current.add(ac);
      try {
        await api.crewEvents(run.sse_url, (event, data) => {
          // crew_completed is the terminal signal (crewEvents resolves on it); its
          // payload is sparse, so the authoritative status is read from the audit
          // record below. Here we stash a mid-run human-input ask onto the bubble
          // so it renders an inline reply box; submitCrewReply clears it on
          // reply / 404, and finalize() replaces the bubble when the run ends.
          if (event === "human_input_required") {
            // `data` defaults to {} in api.crewEvents, but JSON.parse("null")
            // would yield null — guard before dereffing so a malformed frame
            // can't throw out of the SSE callback.
            const msgId = data && typeof data.msg_id === "string" ? data.msg_id : "";
            if (!msgId) {
              // A well-formed ask always carries msg_id (backend contract); without
              // one we can't address a reply POST. Surface it rather than leaving a
              // silently-stuck bubble.
              // eslint-disable-next-line no-console
              console.warn("crew human_input_required without a usable msg_id; ignoring", data);
              return;
            }
            const prompt = data && typeof data.prompt === "string" ? data.prompt : "";
            setMessages((prev) => prev.map((m) =>
              m.id === placeholderId
                ? { ...m, crewAsk: { msgId, prompt }, runningText: `${prettyCrew(verb)} crew · awaiting your reply` }
                : m,
            ));
          }
        }, ac.signal);
      } finally {
        crewAborts.current.delete(ac);
      }
      // Authoritative final status from the assembled run record — never infer
      // success from the sparse crew_completed event (review HIGH/MED).
      const rec = await api.crewRunRecord(run.poll_url);
      const steps = (rec.roles_log ?? []).map((r) => ({ text: `${r.role} · ${r.status}`, ok: r.status !== "error" }));
      // Surface the crew's own conclusion (CrewOutput.summary — a triage headline,
      // an answered human-input reply, etc.) in the bubble instead of a bare role
      // count; capped so a long memo summary can't balloon the bubble.
      const rawSummary = (rec.summary ?? "").trim();
      const crewSummary = rawSummary.length > 280 ? `${rawSummary.slice(0, 280)}…` : rawSummary;
      finalize(rec.status === "ok"
        ? { id: placeholderId, role: "anton", who: "ANTON", time: nowHHMM(), body: crewSummary ? `${prettyCrew(verb)} crew complete — ${crewSummary} · Open Runs for the full record.` : `${prettyCrew(verb)} crew complete — ${steps.length} role${steps.length === 1 ? "" : "s"}. Open Runs for the full record.`, steps, lane: "crew", crewRunId: run.run_id, route: `ROUTED · LOCAL CREW → ${verb.toUpperCase()}` }
        : { id: placeholderId, role: "anton", who: "ANTON", time: nowHHMM(), body: `${prettyCrew(verb)} crew ${rec.status || "ended"}${rec.error ? ` — ${rec.error}` : ""}.`, steps: steps.length ? steps : undefined, lane: "crew", failed: true, crewRunId: run.run_id, route: `LOCAL CREW → ${verb.toUpperCase()}` });
    } catch (e) {
      // Abandoned mid-stream (unmount abort) — the bubble was already dropped; don't render a failure.
      if (e instanceof DOMException && e.name === "AbortError") return;
      const msg = e instanceof ApiError ? `${e.status}: ${e.message}` : e instanceof Error ? e.message : "Unknown error";
      finalize({ id: placeholderId, role: "anton", who: "ANTON", time: nowHHMM(), body: `Crew dispatch failed — ${msg}.`, lane: "crew", failed: true });
    }
  };

  // Answer a crew's mid-run human-input ask (the inline reply box on a
  // lane:"crew" bubble). Side-channel POST — does NOT touch the open SSE
  // stream, which stays awaiting crew_completed; the crew unblocks server-side
  // and resumes. On success we clear the ask + show a transient "resuming"
  // line. A 404 means the ask is no longer pending (already answered / timed
  // out): clear the box WITHOUT failing the bubble — crewEvents + the audit
  // record still finalize it. Other errors propagate so CrewReplyBox can
  // surface them inline and let the operator retry. Keyed by (crewRunId, msgId)
  // since this runs outside fireCrew's placeholderId closure.
  const submitCrewReply = async (runId: string, msgId: string, response: string) => {
    try {
      await api.crewHumanInput(runId, { msg_id: msgId, response });
      setMessages((prev) => prev.map((m) =>
        m.crewRunId === runId && m.crewAsk?.msgId === msgId
          ? { ...m, crewAsk: null, runningText: `${(m.runningText ?? "Crew").split(" · ")[0]} · resuming…` }
          : m,
      ));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setMessages((prev) => prev.map((m) =>
          m.crewRunId === runId && m.crewAsk?.msgId === msgId
            ? { ...m, crewAsk: null, runningText: `${(m.runningText ?? "Crew").split(" · ")[0]} · reply window closed` }
            : m,
        ));
        return;
      }
      throw e;   // genuine failure — CrewReplyBox shows it inline + lets the operator retry
    }
  };

  // Drawer tile dispatch — branch crew verbs (subprocess lane) from skill
  // WorkflowKeys. triage needs a server-readable PDF path → collect it first.
  const onDrawerFire = (key: string) => {
    if (CREW_VERBS.has(key)) {
      if (key === "triage") { setTriageOpen(true); return; }
      void fireCrew(key);
      return;
    }
    handleDrawerFire(key as WorkflowKey);
  };

  const openChat = (project?: string) => {
    if (project) setWorkspace({ type: "project", name: project });
    setTab("agent");
    setRailDetail("chat");
    setChatMounted(true);
    setChatFocusSignal((n) => n + 1);
  };

  const activeServer  = allSessions.find((s) => s.id === activeSessionId);
  const activeSession = activeServer ? mapServerToListSession(activeServer) : undefined;

  // #session-ops — chat-header ⋮ actions. Each mutates via the bridge then
  // refreshes the list; the auto-select effect above re-points activeSessionId
  // when an archived/deleted session drops out of the list.
  const refreshSessions = useCallback(async () => {
    try { const r = await api.listSessions({ archived: false }); setAllSessions(r.sessions); }
    catch { /* keep the current list on a transient failure */ }
  }, []);
  // Optimistic local update + a reconciling refresh (review S3): the UI reflects
  // the action at once and never shows a stale/deleted session if the refresh
  // fails; on a failed mutation the refresh restores the real server state.
  const handleRenameSession = useCallback(async (title: string) => {
    if (!activeSessionId) return;
    try {
      const updated = await api.renameSession(activeSessionId, title);
      setAllSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
    } catch (e) { console.warn("rename session failed", e); void refreshSessions(); }
  }, [activeSessionId, refreshSessions]);
  const handleArchiveSession = useCallback(async () => {
    if (!activeSessionId) return;
    const id = activeSessionId;
    setAllSessions((prev) => prev.filter((s) => s.id !== id));  // archived drops from the list
    try { await api.archiveSession(id); }
    catch (e) { console.warn("archive session failed", e); }
    finally { void refreshSessions(); }
  }, [activeSessionId, refreshSessions]);
  const handleTogglePinSession = useCallback(async () => {
    if (!activeSessionId) return;
    const next = !(activeServer?.pinned ?? false);
    try {
      const updated = await api.pinSession(activeSessionId, next);
      setAllSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
      void refreshSessions();   // re-sort — pinned-first ordering is server-side
    } catch (e) { console.warn("pin session failed", e); void refreshSessions(); }
  }, [activeSessionId, activeServer?.pinned, refreshSessions]);
  const handleDeleteSession = useCallback(async () => {
    if (!activeSessionId) return;
    const id = activeSessionId;
    setAllSessions((prev) => prev.filter((s) => s.id !== id));  // optimistic removal
    // #chat-attach — housekeeping: drop the deleted session's attachment bucket
    // so orphaned buckets (with their extracted text) don't linger in memory.
    setAttachmentsBySession((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try { await api.deleteSession(id); }
    catch (e) { console.warn("delete session failed", e); }
    finally { void refreshSessions(); }   // reconcile (restores it if the delete failed)
  }, [activeSessionId, refreshSessions]);

  // Selecting any session follows its workspace, so the header + chat body never
  // diverge (#session-workspace-sync). The rail shows General sessions under every
  // workspace; clicking one moves you INTO that General workspace (header + chat
  // both switch), rather than leaving the project header over a General chat.
  const handleSelectSession = useCallback((id: string) => {
    const s = allSessions.find((x) => x.id === id);
    if (s && (s.workspaceType !== workspace.type || s.workspaceName !== workspace.name)) {
      setWorkspace({ type: s.workspaceType, name: s.workspaceName });
    }
    setActiveSessionId(id);
  }, [allSessions, workspace.type, workspace.name]);

  // #minimax-chat-model — MiniMax is a cloud model, so it's only offered where
  // the session routes to the cloud (general workspaces at bridge tier). Reset a
  // stale "minimax" selection when the workspace isn't cloud-eligible so it can't
  // 403 the next send.
  const minimaxAllowed = workspace.type === "general";
  useEffect(() => {
    if (!minimaxAllowed && chatModel === "minimax") setChatModel("default");
  }, [minimaxAllowed, chatModel]);
  // Real context-window % from the active session (bridge-reported tokens ÷
  // model window); null until the bridge provides it — see the VAULT brief.
  const contextPct = activeServer?.contextWindow
    ? Math.min(100, Math.round((activeServer.contextTokens ?? 0) / activeServer.contextWindow * 100))
    : null;

  // Create a fresh chat session in the given workspace and make it the active
  // (main-chat) session. Server picks the default title ("Chat · {ws}"). We
  // refresh the full list (rather than optimistically prepending) so the
  // server-side last_active ordering stays canonical. Shared by the SESSIONS
  // +NEW button and by workspace creation (which lands you in a new session).
  const createAndSelect = async (workspace_type: WorkspaceType, workspace_name: string) => {
    try {
      const created = await api.createSession({ workspace_type, workspace_name, mode: "chat" });
      const refreshed = await api.listSessions({ archived: false });
      setAllSessions(refreshed.sessions);
      setActiveSessionId(created.id);
    } catch (e) {
      // Surface inline once an error region exists in SessionList — for now,
      // console.warn keeps the failure visible without crashing the canvas.
      // eslint-disable-next-line no-console
      console.warn("createSession failed", e);
    }
  };

  // WorkspacePickerModal CREATE handler. Wired async so the modal can surface
  // 409 / 422 / 500 errors inline. Re-throws so the modal's local error state
  // catches it. On a genuine create we switch to the new workspace AND open a
  // fresh session in it, so the main chat lands there. On 409 (the workspace
  // already exists) we just switch — the session auto-select picks its most
  // recent existing session.
  const handleCreateWorkspace = async (ws: { type: WorkspaceType; name: string }) => {
    try {
      const r = await api.createWorkspace({ type: ws.type, name: ws.name });
      await refreshWorkspaces(ws.type);
      setWorkspace({ type: ws.type, name: r.workspace.name });
      await createAndSelect(ws.type, r.workspace.name);
      setPickerOpen(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Existing workspace — treat as a switch rather than an error.
        setWorkspace({ type: ws.type, name: ws.name });
        setPickerOpen(false);
        return;
      }
      // Re-throw so the modal's local error state captures it
      throw e;
    }
  };

  // SessionList +NEW handlers. SESSIONS opens a new session in the selected
  // project/BD. GENERAL opens the workspace picker preset to General — the
  // same flow as "+ NEW WORKSPACE → General" — so the operator names the
  // general workspace; creation then lands in a fresh session there.
  const handleNewSession = () => { void createAndSelect(workspace.type, workspace.name); };
  const handleNewGeneralSession = () => { setPickerInitialType("general"); setPickerOpen(true); };

  // Wire ChatCanvas composer → POST /api/sessions/{id}/messages. Optimistic
  // user bubble appears immediately; on success it's replaced by the server-
  // authored pair. 403 (sensitivity refused) and generic failures render as
  // failed bubbles rather than crashing the canvas.
  const handleSend = async (
    text: string,
    attachmentsPayload: { filename: string; text: string }[] = [],
    sensitivityOverride?: Sensitivity,
  ) => {
    if (!activeSessionId || sending) return;
    // Files-only sends are allowed (no text); the composer's send guard already
    // ensures there's text OR ≥1 ready attachment before calling here.
    if (!text && attachmentsPayload.length === 0) return;

    // Snapshot the session this turn is fired from, so the success-path
    // attachment clear targets THIS session's bucket even if the user switches
    // away while the send is in flight.
    const sendSessionId = activeSessionId;
    const tempId = `temp-${crypto.randomUUID()}`;
    // Show the attached filenames on the optimistic user bubble so a files-only
    // turn isn't an empty bubble while the send is in flight.
    const attachLine = attachmentsPayload.length
      ? attachmentsPayload.map((a) => `📎 ${a.filename}`).join("\n")
      : "";
    const optimisticBody = [text, attachLine].filter(Boolean).join(text && attachLine ? "\n\n" : "");
    const optimistic: Message = {
      id: tempId,
      role: "user",
      who: "Operator",
      time: nowHHMM(),
      body: optimisticBody,
    };
    // Optimistic ANTON "running" placeholder (brief item 1) — gives the in-thread
    // status one-liner something to drive while the POST is in flight. The bridge
    // emits no chat-phase events for a plain chat turn, so we advance a
    // client-derived phase sequence on a timer; ChatCanvas renders the live line.
    const runId = `running-${crypto.randomUUID()}`;
    const phases = chatPhases(workspace.type, chatModel);
    const running: Message = {
      id: runId,
      role: "anton",
      who: "ANTON",
      time: nowHHMM(),
      running: true,
      runningText: phases[0],
      lane: "chat",
    };
    setMessages((prev) => [...prev, optimistic, running]);
    setSending(true);

    // Advance the phase line on a timer, holding on the final phase until the
    // answer lands. Guarded so it only patches the placeholder while it's still
    // present (a session switch replaces the list → the interval no-ops + is
    // cleared in `finally`). ~900ms/phase reads as deliberate work, not a flicker.
    let phaseIdx = 0;
    const phaseTimer = window.setInterval(() => {
      phaseIdx = Math.min(phaseIdx + 1, phases.length - 1);
      setMessages((prev) =>
        prev.some((m) => m.id === runId)
          ? prev.map((m) => (m.id === runId ? { ...m, runningText: phases[phaseIdx] } : m))
          : prev,
      );
      if (phaseIdx >= phases.length - 1) window.clearInterval(phaseTimer);
    }, 900);

    try {
      const res = await api.sendMessage(
        activeSessionId, text,
        {
          ...(chatModel === "minimax" ? { model_override: "minimax" as const } : {}),
          ...(attachmentsPayload.length ? { attachments: attachmentsPayload } : {}),
          ...(sensitivityOverride ? { sensitivity_override: sensitivityOverride } : {}),
        },
      );
      // Drop the temp user bubble + the running placeholder; insert the
      // server-authored pair. The status line resolves into the streamed answer.
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== tempId && m.id !== runId),
        res.userMessage,
        res.antonMessage,    // api.sendMessage already sets the unwired flag
      ]);
      // #chat-attach (MED) — clear the staged attachments ONLY on a successful
      // send, and only for the originating session's bucket. On a refused/failed
      // send (403/network) we leave them so the operator can retry.
      setAttachmentsBySession((prev) =>
        prev[sendSessionId]?.length ? { ...prev, [sendSessionId]: [] } : prev);
    } catch (e) {
      const isSensitivity = e instanceof ApiError && e.status === 403;
      const errBody = e instanceof ApiError
        ? (isSensitivity ? `🔒 Sensitivity refused: ${e.message}` : `Bridge call failed (${e.status}): ${e.message}`)
        : e instanceof Error ? `Bridge call failed: ${e.message}` : "Bridge call failed.";
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== tempId && m.id !== runId),
        { ...optimistic, id: `failed-${tempId}`, failed: true },
        {
          id: `err-${crypto.randomUUID()}`,
          role: "anton",
          who: "ANTON",
          time: nowHHMM(),
          body: errBody,
          failed: true,
        },
      ]);
    } finally {
      window.clearInterval(phaseTimer);
      setSending(false);
    }
  };

  // #redesign — left-nav selection. Maps the NavKey to its TabKey body (if any)
  // and carries the OPERATOR dirty-state guard that previously lived on
  // WorkspaceSelector.onTabChange. recall/news map to null → placeholders.
  const handleNavSelect = (key: NavKey) => {
    const nextTab = navToTab(key);
    // Guard on `nav` (the visible-screen truth), NOT the stale `tab` — recall/news
    // have no TabKey so `tab` doesn't track them. Confirm before leaving Operator
    // with unsaved edits.
    if (
      nav === "operator" && key !== "operator" && hasUnsavedOperatorEdits()
      && !window.confirm("Discard unsaved OPERATOR edits?")
    ) return;
    setNav(key);
    if (nextTab) setTab(nextTab);
  };

  return (
    <div className="grid grid-rows-[auto_auto_auto_minmax(0,1fr)] h-screen overflow-hidden bg-bg text-t1">
      <TopHeader
        workspaceSwitcher={
          <WorkspaceSwitcher
            workspace={workspace}
            onWorkspaceChange={setWorkspace}
            onOpenPicker={() => { setPickerInitialType("project"); setPickerOpen(true); }}
            projects={projectWorkspaces}
            bds={bdWorkspaces}
            generals={generalWorkspaces}
          />
        }
        onOpenCommand={() => setCmdOpen(true)}
      />
      <SparkTicker />
      <MacroTicker />

      {/* Body — teal ground (--bg #AECCCC); 14px padding + gap; self-contained rounded rails/cards (Paper). */}
      <div className="flex gap-[14px] p-[14px] min-h-0 overflow-hidden bg-bg">
        <NavSidebar active={nav} onSelect={handleNavSelect} reviewCount={proposals?.total} burn={llmBurn} />
        <div className="grow min-w-0 flex gap-[14px] min-h-0 overflow-hidden">
          {nav === "recall" ? (
            <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><RecallTab projects={projectWorkspaces} activeProject={workspace.type === "project" ? workspace.name : undefined} /></main>
          ) : nav === "news" ? (
            <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><NewsTab /></main>
          ) : nav === "activity" ? (
            <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><ActivityTab /></main>
          ) : tab === "agent" ? (
        <>
          {/* LEFT — sessions (off-white --surface card). Brief item 4: the
              13″ collapse is a 220ms WIDTH-SLIDE rather than an instant swap —
              a persistent wrapper animates its width (56 ⇄ 254) so the chat
              reflows smoothly; the inner component swaps under overflow-hidden.
              Reduced-motion → no transition (instant). The CollapsedSessions /
              SessionList chevrons rotate to signal state. */}
          {(() => {
            const collapsed = deskLayout === "compact" && !sessionsExpanded;
            return (
              <div
                className={cn(
                  // flex-row so the single inner rail stretches to full height
                  // (cross-axis stretch), matching the un-wrapped behaviour.
                  "shrink-0 flex overflow-hidden min-h-0",
                  !reduceMotion && "transition-[width] duration-[220ms] ease-[cubic-bezier(.4,0,.2,1)]",
                )}
                style={{ width: collapsed ? 56 : 254 }}
              >
                {collapsed ? (
                  <CollapsedSessions
                    workspace={workspace}
                    sessions={partitioned.topScoped ? topSessions : generalSessions}
                    activeId={activeSessionId}
                    onSelect={handleSelectSession}
                    onNew={partitioned.topScoped ? handleNewSession : handleNewGeneralSession}
                    onExpand={() => setSessionsExpanded(true)}
                  />
                ) : (
                  <div className="w-[254px] flex flex-col rounded-[16px] bg-bg-2 shadow-card overflow-hidden min-h-0 h-full">
                    <SessionList
                      topSessions={topSessions}
                      topTag={WS_TAG_LABEL[workspace.type]}
                      topName={workspace.name}
                      topScoped={partitioned.topScoped}
                      generalSessions={generalSessions}
                      activeId={activeSessionId}
                      onSelect={handleSelectSession}
                      onNew={handleNewSession}
                      onNewGeneral={handleNewGeneralSession}
                      loading={sessionsLoading}
                      onCollapse={deskLayout === "compact" ? () => setSessionsExpanded(false) : undefined}
                    />
                  </div>
                )}
              </div>
            );
          })()}

          {/* CENTER — chat canvas card with workflow drawer slot */}
          <div className="grow basis-0 min-w-0 flex flex-col rounded-[16px] bg-bg-2 shadow-card overflow-hidden min-h-0">
            <ChatCanvas
              // Key by session so per-session local state (inline-rename draft,
              // armed-delete) resets on switch — never leaks onto another session
              // (#session-ops review S2-1).
              key={activeSessionId || "none"}
              dealName={workspace.name}
              sessionTitle={activeSession?.title ?? "No active session"}
              sessionId={activeSessionId || ""}
              startedAt={activeSession?.ago ?? ""}
              contextPct={contextPct}
              messages={messages}
              loading={messagesLoading}
              sending={sending}
              onSend={handleSend}
              workspaceType={workspace.type}
              // Per-session attachment bucket + a functional updater BOUND to the
              // active session id. Because each ChatCanvas instance is keyed by
              // session id, its upload `.then`/`.catch` closures capture THIS sid
              // and patch only its own bucket — a stale upload resolving after a
              // session switch can never reach the now-active session (§5.2 leak).
              attachments={attachmentsBySession[activeSessionId] ?? []}
              onAttachmentsChange={(updater) =>
                setAttachmentsBySession((prev) => ({
                  ...prev,
                  [activeSessionId]: updater(prev[activeSessionId] ?? []),
                }))
              }
              draft={draft}
              onDraftChange={setDraft}
              focusSignal={composerFocus}
              onCrewReply={submitCrewReply}
              onSlash={() => setWfOpen(true)}
              pinned={activeServer?.pinned ?? false}
              onRename={handleRenameSession}
              onArchive={handleArchiveSession}
              onTogglePin={handleTogglePinSession}
              onDelete={handleDeleteSession}
              model={chatModel}
              onModelChange={setChatModel}
              minimaxAllowed={minimaxAllowed}
            />
          </div>

          {/* 55″ co-panel — Live-Model engine preview (Paper 5T3-0), badged Preview.
              A drawer: a 48px tab by default, expand to the full 520px panel.
              Brief item 4: the same 220ms width-slide as the sessions rail (the
              inner panel swaps under overflow-hidden; reduced-motion → instant).
              Still a static PREVIEW/ROADMAP — only the drawer-slide is added. */}
          {deskLayout === "ultra" && (
            <div
              className={cn(
                "shrink-0 flex overflow-hidden min-h-0",
                !reduceMotion && "transition-[width] duration-[220ms] ease-[cubic-bezier(.4,0,.2,1)]",
              )}
              style={{ width: coPanelExpanded ? 520 : 48 }}
            >
              {coPanelExpanded
                ? <LiveModelCoPanel onCollapse={() => setCoPanelExpanded(false)} />
                : <CollapsedCoPanel onExpand={() => setCoPanelExpanded(true)} />}
            </div>
          )}

          {/* RIGHT — Paper "Context rail" (teal #3F8B88, node 5W4-0) + conditional functional alerts */}
          <aside className="w-[350px] shrink-0 flex flex-col gap-[14px] min-h-0">
            <ContextRail
              workspace={workspace}
              weekRange="Fri – Sun"
              weekDays={WEEK_DAYS}
              projectOverview={projectOverview}
              burn={llmBurn}
              plans={plans}
              // Brief item 3 — the enriched Cost & limits section yields its slot
              // to the red BLOCKED banner when budget incidents are open (never
              // both). The banner now lives INSIDE the rail's COST section rather
              // than as a sibling card below, matching Paper Desk 1b.
              incidents={openIncidents}
              onAckIncident={setAckTarget}
              chatTab={railDetail}
              onChatTab={(t) => { setRailDetail(t); if (t === "chat") setChatMounted(true); }}
              chatSlot={chatMounted
                ? <ProjectChatPanel project={workspace.name} workspaceType={workspace.type} focusSignal={chatFocusSignal} />
                : null}
              onOpenVault={() => setTab("vault")}
            />
            <SensitivityOverridesPanel />
          </aside>
        </>
      ) : tab === "vault" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><VaultTab /></main>
      ) : tab === "daily" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><DailyTab /></main>
      ) : tab === "inbox" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card">
          <InboxTab
            pending={proposals}
            onRefresh={refreshProposals}
            loading={proposalsLoading}
            error={proposalsError}
          />
        </main>
      ) : tab === "runs" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><RunsTab /></main>
      ) : tab === "drafts" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><DraftsTab /></main>
      ) : tab === "budget" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><TknBudgetTab /></main>
      ) : tab === "routing" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><RoutingTab /></main>
      ) : tab === "providers" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><SkillsProvidersTab /></main>
      ) : tab === "taxonomy" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><TaxonomyTab /></main>
      ) : tab === "operator" ? (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 rounded-[16px] bg-bg-2 shadow-card"><OperatorTab /></main>
      ) : (
        <main className="mx-auto w-full max-w-[1480px] min-w-0 overflow-y-auto min-h-0 px-8 py-10 rounded-[16px] bg-bg-2 shadow-card">
          <div className="panel w-full px-6 py-8">
            <h2 className="text-lg font-semibold text-t1">{labelForTab(tab)}</h2>
            <p className="mt-2 text-base text-t2">Unknown tab.</p>
          </div>
        </main>
      )}
        </div>
      </div>

      <CommandModal
        open={cmdOpen}
        onClose={() => setCmdOpen(false)}
        onFire={(key, promptText) => { void fire(key, promptText); }}
        projects={projects}
        onChat={openChat}
      />

      {/* Workflow palette — `/`-triggered overlay (replaces the old bottom drawer). */}
      <WorkflowDrawer
        open={wfOpen}
        onClose={() => setWfOpen(false)}
        sections={[...WORKFLOW_SECTIONS, CREW_SECTION]}
        totalCount={34}
        onFire={onDrawerFire}
      />

      <WorkspacePickerModal
        open={pickerOpen}
        initialType={pickerInitialType}
        onClose={() => setPickerOpen(false)}
        onSwitch={(ws) => { setWorkspace(ws); setPickerOpen(false); }}
        onCreate={handleCreateWorkspace}
        workspaces={buildPickerWorkspaces(projectWorkspaces, bdWorkspaces, generalWorkspaces)}
        activeName={workspace.name}
        counts={{
          project: projectWorkspaces.length,
          bd:      bdWorkspaces.length,
          general: generalWorkspaces.length,
        }}
      />

      <BudgetAckModal
        incident={ackTarget}
        onClose={() => setAckTarget(null)}
        onAcked={() => void refreshIncidents()}
      />

      <CompsBuildModal
        open={compsBuildOpen}
        onClose={() => setCompsBuildOpen(false)}
        initialDeal={workspace.type === "project"
          ? { dealName: workspace.name, target: workspace.name }
          : undefined}
      />

      <LBORunModal
        open={lboOpen}
        onClose={() => setLboOpen(false)}
        workspace={workspace}
      />

      <DealTrackerModal
        open={dealTrackerOpen}
        onClose={() => setDealTrackerOpen(false)}
      />

      <CrewTriageModal
        open={triageOpen}
        onClose={() => setTriageOpen(false)}
        onRun={(args) => { setTriageOpen(false); void fireCrew("triage", args); }}
      />
    </div>
  );
}

function labelForTab(t: TabKey): string {
  switch (t) {
    case "agent":  return "Agent Mode";
    case "vault":  return "Knowledge Vault";
    case "daily":  return "Daily Notes";
    case "inbox":  return "Inbox";
    case "runs":   return "Run History";
    case "drafts": return "Draft Materials";
    case "budget": return "Token Budget";
    case "providers": return "Skill Providers";
    case "routing": return "Routing";
    case "taxonomy": return "Taxonomy";
    case "operator": return "Operator";
  }
}

// #redesign Phase 3 — NavKey → the existing TabKey body it renders. desk→agent,
// notes→daily, outputs→drafts, activity→runs (Vault folds in later). routing now
// has its OWN tab body (RoutingTab), split out of providers. recall/news have no
// tab body yet → null, rendered as placeholders off `nav`.
function navToTab(key: NavKey): TabKey | null {
  switch (key) {
    case "desk":      return "agent";
    case "inbox":     return "inbox";
    case "notes":     return "daily";
    case "outputs":   return "drafts";
    case "activity":  return null;   // renders <ActivityTab/> off nav (Scheduler · Runs · Vault)
    case "routing":   return "routing";
    case "providers": return "providers";
    case "budget":    return "budget";
    case "taxonomy":  return "taxonomy";
    case "operator":  return "operator";
    case "recall":    return null;
    case "news":      return null;
  }
}

function prettyName(key: WorkflowKey): string {
  return key
    .split("-")
    .map((w) => (w === "qa" ? "Q&A" : w === "ic" ? "IC" : w === "bd" ? "BD" : w))
    .map((w) => (w === w.toUpperCase() ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

// #front-door — crew verb → display label (e.g. "triage" → "Triage").
function prettyCrew(verb: string): string {
  return verb.charAt(0).toUpperCase() + verb.slice(1);
}
