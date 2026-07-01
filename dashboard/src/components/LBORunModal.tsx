import { useEffect, useRef, useState } from "react";
import { X, RefreshCw, Check, AlertTriangle } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import { KpiCard } from "./ui/KpiCard";
import type {
  LBOBoxField, LBORunResult, SkillAwaiting, SuspendedSkill,
} from "../types";

/**
 * LBO run launcher (#lbo-dashboard-wiring, 2026-06-09; #lbo-agent-leg-phase1,
 * 2026-06-10). Two modes, mirroring CompsBuildModal's tabs:
 *
 *   • ORCHESTRATE (attended, recommended for a real deal) — collects deal
 *     name + context + doc paths and emits a paste-ready prompt for an
 *     operator-attended Claude Code session (agent-leg Phase 1, Option A of
 *     the 2026-06-10 design brief). That session reads the deal docs, builds
 *     the operating model with the operator, and fires THIS route's intake
 *     with `prefill` (cited boxes) + `client_fs` (the operating model) — then
 *     hands back here for box confirmation. The dash can't spawn a CLI, so
 *     this hands the operator a copy-paste prompt (comps Path-A precedent).
 *
 *   • RUN (intake) — the #63 cooperative suspend/resume intake, unchanged.
 *     NOT an upfront form:
 *
 *     1. INTAKE — deal name + free-text context fire POST /api/workflows/lbo
 *        (mode "intake"); the skill SUSPENDS and replies 202 with the
 *        deal-assumption boxes manifest. A pending intake survives closing the
 *        modal (and the bridge restarting) — reopening offers to resume it via
 *        GET /api/skills/suspended.
 *     2. BOXES — the form is rendered VERBATIM from the server manifest (the
 *        skill owns its fields; the dash owns zero LBO schema). The answer goes
 *        back via POST /api/skills/{run_id}/resume. A fixable answer (missing
 *        citations / failed validation) RE-SUSPENDS — same run, fresh token,
 *        server note shown inline — rather than burning the run.
 *     3. RESULT — IRR/MOIC + S&U headline + the workbook path.
 *
 *   NB the engine populates the LBO inputs only — the operating model in the
 *   template's Client_FS sheet must be built/refreshed upstream for a real
 *   deal. The Orchestrate mode IS that upstream leg (the attended session
 *   ships the model as the intake's `client_fs` block); a bare Run intake
 *   uses whatever model sits in the template.
 *
 * Pending-intake pickup is visible in BOTH modes (resuming from Orchestrate
 * switches to the Run pane's boxes step — the resume is the same #63 flow).
 *
 * Modal pattern mirrors CompsBuildModal (backdrop + ESC close, inline error,
 * dumb component — App owns open/close).
 */

interface Props {
  open: boolean;
  onClose: () => void;
  /** Seeded from the selected workspace (LBO is project-scoped — the bridge
   *  403s anything else; we pass it through honestly rather than masking). */
  workspace: { type: "project" | "bd" | "general"; name: string };
}

type Step = "intake" | "clarify" | "boxes" | "result";
type Mode = "orchestrate" | "agent" | "run";

/** A clarify-stage suspension from the in-bridge intake agent (Phase 2): the
 *  agent asks targeted questions BEFORE prefilling the boxes. Dispatched on
 *  the EXPLICIT `stage` marker the route stamps on every manifest option
 *  (codex slice-3 SEV-2 — never on prompt wording). */
function isClarifyAwaiting(a: SkillAwaiting): boolean {
  return a.skill === "lbo-intake-agent" && a.options?.[0]?.stage === "clarify";
}

// The attended-session brief the Orchestrate prompt points at (mirrors
// CompsBuildModal's BRIEF_PATH — the session-briefs umbrella, not this repo).
const BRIEF_PATH = "<repo>/session-briefs/SESSION-LBO-ORCHESTRATION.md";

export function LBORunModal({ open, onClose, workspace }: Props) {
  const [mode, setMode] = useState<Mode>("orchestrate");
  const [step, setStep] = useState<Step>("intake");
  const [dealName, setDealName] = useState("");
  const [dealContext, setDealContext] = useState("");
  // Orchestrate-only: deal-doc paths, one per line (deck PDF, Excel forecast…).
  const [docPaths, setDocPaths] = useState("");
  const [promptCopied, setPromptCopied] = useState(false);
  const [pending, setPending] = useState<SuspendedSkill[]>([]);

  const [awaiting, setAwaiting] = useState<SkillAwaiting | null>(null);
  const [note, setNote] = useState<string | null>(null);   // re-suspend note from the server
  const [values, setValues] = useState<Record<string, string>>({});
  const [invalid, setInvalid] = useState<Set<string>>(new Set());
  const [sourceNote, setSourceNote] = useState("");

  const [result, setResult] = useState<LBORunResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  // Synchronous double-fire guard: `busy` state only flips on the NEXT render,
  // so two clicks in one frame would race two requests — the loser's 409 then
  // paints a stale-token error over a good result (codex wiring review,
  // CONCERN 4). A ref blocks the second click before React re-renders.
  const inFlightRef = useRef(false);

  // Pending lbo intakes — offer pickup instead of firing a duplicate run.
  // Scoped to the CURRENT workspace: resume governance comes from the
  // suspension row, so offering another workspace's intake here would let a
  // mis-click run a different deal's engine from this context (codex wiring
  // review, CONCERN 3). Switch workspace to see its pending intakes.
  // Re-fetchable on demand (codex pane review, HIGH): in the attended flow the
  // EXTERNAL session fires the intake while this modal may sit open — the new
  // suspension must be reachable without close/reopen, so this is called on
  // open, on every tab switch, and from the Refresh button in the block.
  const refreshPending = () => {
    api.skillsSuspended(workspace.type)
      .then((r) => setPending(r.pending.filter(
        // Both intake flavours land here: the plain /lbo intake AND the
        // in-bridge agent's suspensions (clarify or prefilled boxes) — the
        // agent run IS an lbo intake once it reaches the boxes stage.
        (s) => (s.skill === "lbo" || s.skill === "lbo-intake-agent")
          && s.workspace_name === workspace.name,
      )))
      .catch(() => setPending([]));
  };

  useEffect(() => {
    if (!open) return;
    setMode("orchestrate");
    setStep("intake");
    setDealName(workspace.type === "project" ? workspace.name : "");
    setDealContext("");
    setDocPaths(""); setPromptCopied(false);
    setAwaiting(null); setNote(null); setValues({}); setInvalid(new Set());
    setSourceNote(""); setResult(null); setBusy(false); setError(null); setCopied(false);
    refreshPending();
    const t = setTimeout(() => firstFieldRef.current?.focus(), 50);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshPending reads only the same workspace deps
  }, [open, workspace.type, workspace.name]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const dealOk = /^[A-Za-z0-9_][A-Za-z0-9 _-]*$/.test(dealName) && dealName.length <= 64;
  const manifest: LBOBoxField[] = awaiting?.options ?? [];

  /** Seed the form from the manifest defaults (conventions prefilled;
   *  deal-specific fields arrive empty). Forces the Run pane visible — an
   *  intake/resume response must never land hidden behind the Orchestrate
   *  pane if the user switched tabs mid-flight (codex pane review, MEDIUM). */
  const enterBoxes = (a: SkillAwaiting, serverNote: string | null) => {
    setMode("run");
    setAwaiting(a);
    setNote(serverNote);
    setValues((prev) => {
      const next: Record<string, string> = {};
      for (const f of a.options ?? []) {
        // Keep what the operator already typed across a re-suspend loop.
        next[f.key] = prev[f.key] ?? (f.default != null ? String(f.default) : "");
      }
      return next;
    });
    setInvalid(new Set());
    setStep("boxes");
  };

  /** Stage dispatcher for any 202 awaiting payload: the agent's clarify
   *  suspension renders as a question form (answers wire shape, no citations
   *  gate); everything else is the boxes manifest. */
  const enterAwaiting = (a: SkillAwaiting, serverNote: string | null) => {
    if (isClarifyAwaiting(a)) {
      setMode("run");
      setAwaiting(a);
      setNote(serverNote);
      setValues({});            // clarify answers always start blank
      setInvalid(new Set());
      setStep("clarify");
      return;
    }
    enterBoxes(a, serverNote);
  };

  const fireIntake = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true); setError(null);
    try {
      const a = await api.lboIntake({
        mode: "intake",
        deal_name: dealName.trim(),
        workspace_type: workspace.type,
        workspace_name: workspace.name,
        workspace_sensitivity: "confidential",
        deal_context: dealContext.trim(),
      });
      enterBoxes(a, null);
    } catch (e) {
      setError(formatError(e));
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  const resumePending = (s: SuspendedSkill) => {
    // The boxes/clarify steps live in the Run pane — picking up from any tab
    // is the same #63 resume, so switch panes rather than duplicating the flow.
    setDealName(guessDeal(s));
    enterAwaiting(s, firstParagraph(s.prompt));
  };

  // ── Agent (in-bridge, Phase 2) — fire the governed lbo-intake-agent ───────
  const fireAgent = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true); setError(null);
    try {
      const a = await api.lboAgentIntake({
        deal_name: dealName.trim(),
        workspace_type: workspace.type,
        workspace_name: workspace.name,
        workspace_sensitivity: "confidential",
        doc_paths: docList,
        deal_context: dealContext.trim(),
      });
      enterAwaiting(a, null);
    } catch (e) {
      setError(formatError(e));
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  /** Deliver clarify answers ({"answers": {...}} wire shape — only non-empty
   *  ones; blanks stay open server-side). The agent then re-suspends into the
   *  boxes manifest with the answers merged into prefill. */
  const submitClarify = async () => {
    if (!awaiting || inFlightRef.current) return;
    const answers: Record<string, string> = {};
    for (const f of manifest) {
      const raw = (values[f.key] ?? "").trim();
      if (raw) answers[f.key] = raw;
    }
    inFlightRef.current = true;
    setBusy(true); setError(null);
    try {
      const r = await api.resumeSkill(awaiting.run_id, {
        resume_token: awaiting.resume_token,
        input: { answers },
      });
      if (isAwaiting(r)) {
        enterAwaiting(r, firstParagraph(r.prompt));
      } else {
        // Defensive: a clarify resume always re-suspends today; surface a
        // completed result honestly if the contract ever changes.
        setMode("run");
        setResult(r);
        setStep("result");
      }
    } catch (e) {
      setError(formatError(e));
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  const submitBoxes = async () => {
    if (!awaiting || inFlightRef.current) return;
    // Parse + client-side validation: required non-empty, numerics numeric.
    const boxes: Record<string, string | number> = {};
    const bad = new Set<string>();
    for (const f of manifest) {
      const raw = (values[f.key] ?? "").trim();
      if (!raw) { if (f.required) bad.add(f.key); continue; }
      if (f.type === "number" || f.type === "int") {
        const n = Number(raw);
        if (!Number.isFinite(n) || (f.type === "int" && !Number.isInteger(n))) { bad.add(f.key); continue; }
        boxes[f.key] = n;
      } else if (f.type === "select" && f.options?.every((o) => typeof o === "number")) {
        boxes[f.key] = Number(raw);
      } else {
        boxes[f.key] = raw;
      }
    }
    if (!sourceNote.trim()) bad.add("__source");
    setInvalid(bad);
    if (bad.size > 0) return;

    inFlightRef.current = true;
    setBusy(true); setError(null);
    try {
      const r = await api.resumeSkill(awaiting.run_id, {
        resume_token: awaiting.resume_token,
        input: {
          boxes,
          citations: [{
            source: sourceNote.trim(),
            fields: "all",
            provided_via: "dashboard-lbo-intake",
            run_id: awaiting.run_id,
          }],
        },
      });
      if (isAwaiting(r)) {
        // Fixable answer — same run, fresh token, server note inline.
        enterAwaiting(r, firstParagraph(r.prompt));
      } else {
        setMode("run");   // surface the result even if the user wandered to Orchestrate
        setResult(r);
        setStep("result");
      }
    } catch (e) {
      setError(formatError(e));
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  /** Reset the Run pane back to a fresh intake from the result view (mirrors
   *  the reset-on-open effect — clears the completed run, its boxes, and the
   *  source gate so the next deal starts clean). */
  const startOver = () => {
    setStep("intake");
    setResult(null);
    setAwaiting(null);
    setNote(null);
    setValues({});
    setInvalid(new Set());
    setSourceNote("");
    setError(null);
    // Match the open-time reset so a "new intake" starts truly clean (keeps the
    // current mode; deal fields + copy-state don't carry over from the last run).
    setDealName(workspace.type === "project" ? workspace.name : "");
    setDealContext("");
    setDocPaths("");
    setPromptCopied(false);
    setCopied(false);
  };

  // D4 provenance grouping: agent-prefilled fields (cited — review first)
  // render as their own group between the empty deal-specific fields and the
  // convention defaults. `provided_via` marks them (incl. the client_fs ack).
  const agentFilled  = manifest.filter((f) => f.provided_via != null);
  const dealSpecific = manifest.filter((f) => f.default == null && f.provided_via == null);
  const conventions  = manifest.filter((f) => f.default != null && f.provided_via == null);

  // ── Orchestrate (attended) — paste-ready prompt for the agent-leg session ──
  const docList = docPaths.split("\n").map((p) => p.trim()).filter(Boolean);
  // Project-gated (codex pane review, LOW): the orchestrated intake 403s from
  // a non-project workspace, so don't hand out a prompt that's doomed.
  const orchestrateOk = workspace.type === "project"
    && dealOk && dealContext.trim().length > 0 && docList.length > 0;
  const orchestratePrompt = buildOrchestratePrompt(
    dealName.trim(), dealContext.trim(), docList, workspace,
  );
  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(orchestratePrompt);
      setPromptCopied(true);
      setTimeout(() => setPromptCopied(false), 1800);
    } catch {
      setError("Clipboard write blocked — select + copy the prompt manually.");
    }
  };

  // Pending-intake pickup — visible in BOTH modes (an orchestrated intake
  // lands here too: the attended session fires it, the operator confirms it).
  // The ACTIVE run is excluded (codex pane review, MEDIUM): once a suspension
  // is loaded into the boxes step, offering its (possibly stale-token) row
  // again would let a mis-click wipe unsaved box edits. The Refresh button is
  // the attended flow's hand-back: the external session fires the intake while
  // this modal sits open, so the list must be re-fetchable in place (HIGH).
  const visiblePending = pending.filter((s) => s.run_id !== awaiting?.run_id);
  const pendingBlock = visiblePending.length > 0 && (
    <div className="mb-[14px] rounded-lg border border-line bg-bg-2 px-[12px] py-[10px]">
      <div className="mb-[8px] flex items-center justify-between">
        <span className="mono text-[10px] tracking-[0.12em] uppercase text-t3">Waiting on you</span>
        <button type="button" onClick={refreshPending} disabled={busy}
          className="flex items-center gap-[5px] text-[10px] tracking-[0.08em] uppercase text-t3 transition-colors hover:text-t1 disabled:opacity-40"
          title="Re-check for intakes fired by an attended session">
          <RefreshCw size={11} /> Refresh
        </button>
      </div>
      <div className="flex flex-col gap-[6px]">
        {visiblePending.map((s) => (
          <div key={s.run_id} className="flex items-center justify-between gap-[8px] rounded-lg border border-line bg-bg-1 px-[10px] py-[7px]">
            <span className="truncate text-[11.5px] text-t2">
              {guessDeal(s)} <span className="text-t4">·</span> {s.workspace_name} <span className="text-t4">·</span> expires {fmtExpiry(s.expires_at)}
            </span>
            <button type="button" onClick={() => resumePending(s)}
              className="shrink-0 rounded-lg border border-accent-line bg-accent-soft px-[12px] py-[4px] text-[11px] font-medium text-accent transition-colors hover:brightness-110">
              Resume
            </button>
          </div>
        ))}
      </div>
    </div>
  );
  // The attended flow's empty-state still needs the refresh affordance — the
  // whole point is a row APPEARING while the modal is open.
  const pendingRefreshHint = visiblePending.length === 0 && mode === "orchestrate" && (
    <div className="mb-[12px] flex items-center justify-between gap-[8px] text-[11px] text-t4">
      <span>No pending intakes yet — the attended session's fire will appear here.</span>
      <button type="button" onClick={refreshPending} disabled={busy}
        className="flex shrink-0 items-center gap-[5px] text-[10px] tracking-[0.08em] uppercase text-t3 transition-colors hover:text-t1 disabled:opacity-40">
        <RefreshCw size={11} /> Refresh
      </button>
    </div>
  );

  // Sub-line in the header that mirrors the active mode/step (verbatim copy).
  const headerSub =
    mode === "orchestrate" ? "orchestrate (attended) · agent leg Phase 1"
      : mode === "agent" ? "agent (in-bridge) · agent leg Phase 2"
      : step === "intake" ? "intake · suspend/resume (#63)"
      : step === "clarify" ? `agent clarifications · run ${awaiting?.run_id.slice(0, 8) ?? ""}`
      : step === "boxes" ? `deal-assumption boxes · run ${awaiting?.run_id.slice(0, 8) ?? ""}`
      : "engine result";

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-start justify-center bg-black/60 px-6 pt-[34px] backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
      role="dialog" aria-modal="true" aria-labelledby="lbo-run-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[680px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — confidential/gated (LBO) */}
        <div className="h-[3px] shrink-0 bg-amber" />
        {/* Head — title · sensitivity Chip · sub-line · close */}
        <div className="flex items-center justify-between gap-[14px] border-b border-line px-[22px] py-[16px]">
          <div className="flex min-w-0 items-center gap-[12px]">
            <h2 id="lbo-run-title" className="shrink-0 text-[15px] font-semibold tracking-[-0.01em] text-t1">
              LBO · Run
            </h2>
            <Chip label="Confidential" variant="confidential" />
            <span className="truncate text-[11.5px] text-t3">{headerSub}</span>
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={busy} />
        </div>

        {/* Mode tabs (mirror CompsBuildModal; locked while a request is in
            flight so an async response can't land hidden behind the other
            pane — codex pane review, MEDIUM) */}
        <div role="tablist" aria-label="LBO modes" className="flex gap-[2px] border-b border-line px-[22px] pt-[10px]">
          <TabBtn active={mode === "orchestrate"} disabled={busy} onClick={() => { setMode("orchestrate"); refreshPending(); }} label="Orchestrate (attended)" />
          <TabBtn active={mode === "agent"} disabled={busy} onClick={() => { setMode("agent"); refreshPending(); }} label="Agent (in-bridge)" />
          <TabBtn active={mode === "run"} disabled={busy} onClick={() => { setMode("run"); refreshPending(); }} label="Run (intake)" />
        </div>

        <div className="flex-1 overflow-y-auto px-[22px] py-[18px]">
          {error && (
            <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-red/40 bg-red/10 px-[11px] py-[8px] text-[11.5px] text-red">
              <AlertTriangle size={13} className="mt-[1px] shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* ── ORCHESTRATE (attended) ─────────────────────────────────── */}
          {mode === "orchestrate" && (
            <>
              {workspace.type !== "project" && <ProjectScopeWarning detail="the orchestrated intake will be refused (403)" type={workspace.type} />}
              {pendingBlock}
              {pendingRefreshHint}
              <p className="mb-[16px] text-[12.5px] leading-[160%] text-t2">
                Paste this into an attended <span className="text-t1">Claude Code</span> session. It reads the
                orchestration brief, reads the deal docs, builds the operating model with you in chat, then fires
                the intake with prefilled (cited) boxes + the Client_FS block — you confirm the boxes back here in
                the Run tab. The dashboard can't spawn a CLI — copy &amp; paste.
              </p>
              <div className="mb-[14px] grid grid-cols-1 gap-[12px]">
                <Field label="Deal name" value={dealName} onChange={setDealName} inputRef={firstFieldRef}
                  placeholder="Project-Apex" invalid={dealName.length > 0 && !dealOk} disabled={busy} />
                <TextArea label="Deal context" value={dealContext} onChange={setDealContext} disabled={busy}
                  rows={3} placeholder="Describe the deal — structure, scenario view, what the docs cover. Carried into the prompt + the intake." />
                <TextArea
                  label="Deal docs"
                  hint="— absolute paths, one per line (deck PDF, Excel forecast…)"
                  value={docPaths} onChange={setDocPaths} disabled={busy} mono
                  rows={3} placeholder={"C:\\…\\deal-deck.pdf\nC:\\…\\forecast.xlsx"} />
              </div>
              <div className="mb-[6px] mono text-[10px] tracking-[0.12em] uppercase text-t3">Paste-ready prompt</div>
              <pre className="max-h-[240px] overflow-y-auto rounded-lg border border-line bg-bg-2 px-[12px] py-[10px] text-[11px] leading-[1.5] text-t1 whitespace-pre-wrap">{orchestratePrompt}</pre>
              <div className="mt-[16px] flex items-center justify-end gap-[10px]">
                {!orchestrateOk && (
                  <span className="mr-auto text-[11px] text-t3">Fill deal name · deal context · at least one doc path.</span>
                )}
                <button type="button" onClick={() => void copyPrompt()} disabled={!orchestrateOk}
                  className={cn("flex items-center gap-[6px] rounded-lg px-[14px] py-[8px] text-[12px] font-medium transition-colors disabled:cursor-default disabled:opacity-40",
                    promptCopied ? "border border-green/45 text-green" : "bg-accent text-bg hover:brightness-110")}>
                  {promptCopied ? <><Check size={13} /> Copied</> : "Copy prompt"}
                </button>
              </div>
              <div className="mt-[10px] text-right text-[10px] tracking-[0.04em] text-t4">
                brief · {BRIEF_PATH.split("/").pop()}
              </div>
            </>
          )}

          {/* ── AGENT (in-bridge, Phase 2) ─────────────────────────────── */}
          {mode === "agent" && (
            <>
              {workspace.type !== "project" && <ProjectScopeWarning detail="the agent intake will be refused (403)" type={workspace.type} />}
              {pendingBlock}
              <p className="mb-[12px] text-[12.5px] leading-[160%] text-t2">
                The governed <span className="text-t1">in-bridge agent</span> reads the deal docs, extracts
                sourced assumptions (transcribe-only — anything unsourced becomes a question for you), asks
                clarifications if needed, then suspends into the standard boxes with cited prefill. You confirm
                in the Run tab; the engine fires on your confirmation.
              </p>
              <div className="mb-[14px] rounded-lg border border-line bg-bg-2 px-[12px] py-[9px] text-[11.5px] leading-[160%] text-t3">
                Confidential docs run on the <span className="text-t2">local model</span> by default. For
                frontier-grade judgment, open a <span className="text-t2">sensitivity-override window</span>
                {" "}(skill <span className="mono text-[10.5px] text-t2">lbo-intake-agent</span> · provider anthropic ·
                ceiling confidential) BEFORE firing — the agent's reasoning then runs on the claude lane for that
                window, audit-stamped. Doc paths must sit under the deal's project tree.
              </div>
              <div className="mb-[14px] grid grid-cols-1 gap-[12px]">
                <Field label="Deal name" value={dealName} onChange={setDealName}
                  placeholder="Project-Apex" invalid={dealName.length > 0 && !dealOk} disabled={busy} />
                <TextArea label="Deal context" value={dealContext} onChange={setDealContext} disabled={busy}
                  rows={3} placeholder="Structure, scenario view, what the docs cover — carried into the agent's judgment." />
                <TextArea
                  label="Deal docs"
                  hint="— absolute paths, one per line (max 8; pdf / xlsx / txt / md / csv)"
                  value={docPaths} onChange={setDocPaths} disabled={busy} mono
                  rows={3} placeholder={"<workspace-root>\\<Deal>\\…\\deal-deck.pdf\n<workspace-root>\\<Deal>\\…\\forecast.xlsx"} />
              </div>
              <div className="flex items-center justify-end gap-[10px]">
                {!(workspace.type === "project" && dealOk && docList.length > 0) && (
                  <span className="mr-auto text-[11px] text-t3">Project workspace · deal name · at least one doc path.</span>
                )}
                <Primary onClick={() => void fireAgent()}
                  disabled={!(workspace.type === "project" && dealOk && docList.length > 0 && docList.length <= 8) || busy}
                  busy={busy} label="Run agent intake" busyLabel="Agent reading docs… (local lane: minutes)" />
              </div>
            </>
          )}

          {/* ── INTAKE ─────────────────────────────────────────────────── */}
          {mode === "run" && step === "intake" && (
            <>
              {workspace.type !== "project" && <ProjectScopeWarning detail="firing will be refused (403)" type={workspace.type} />}
              {pendingBlock}
              <div className="grid grid-cols-1 gap-[12px]">
                <Field label="Deal name" value={dealName} onChange={setDealName} inputRef={firstFieldRef}
                  placeholder="Project-Apex" invalid={dealName.length > 0 && !dealOk} disabled={busy} />
                <TextArea label="Deal context" value={dealContext} onChange={setDealContext} disabled={busy}
                  rows={4} placeholder="Describe the deal + point at the docs (overview, forecast). Carried into the intake for the agent leg." />
              </div>
              <div className="mb-[16px] mt-[12px] rounded-lg border border-line bg-bg-2 px-[12px] py-[9px] text-[11.5px] leading-[160%] text-t3">
                The engine runs on the operating model currently in the template's Client_FS sheet —
                build/refresh it upstream for a real deal. Intake suspends with the deal-assumption
                boxes; you can close this modal and resume later from "Waiting on you".
              </div>
              <div className="flex justify-end">
                <Primary onClick={() => void fireIntake()} disabled={!dealOk || busy} busy={busy}
                  label="Start intake" busyLabel="Firing…" />
              </div>
            </>
          )}

          {/* ── CLARIFY (agent questions, Phase 2) ─────────────────────── */}
          {mode === "run" && step === "clarify" && awaiting && (
            <>
              {note && <ServerNote text={note} />}
              <p className="mb-[14px] text-[12.5px] leading-[160%] text-t2">
                The agent hit ambiguities it refuses to guess on. Answer what you can — blanks stay open and
                the boxes form follows either way.
              </p>
              <div className="mb-[16px] flex flex-col gap-[14px]">
                {manifest.map((f) => (
                  <div key={f.key} className="flex flex-col gap-[5px]">
                    <div className="text-[12px] leading-[150%] text-t1">{f.help ?? f.label}</div>
                    <input value={values[f.key] ?? ""} disabled={busy}
                      onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))}
                      placeholder={`answer (${f.key}) — leave blank to keep open`}
                      className={INPUT_CLASS} />
                  </div>
                ))}
              </div>
              <div className="flex items-center justify-between gap-[10px]">
                <span className="text-[10.5px] text-t4">expires {fmtExpiry(awaiting.expires_at)}</span>
                <Primary onClick={() => void submitClarify()} disabled={busy} busy={busy}
                  label="Send answers" busyLabel="Sending…" />
              </div>
            </>
          )}

          {/* ── BOXES ──────────────────────────────────────────────────── */}
          {mode === "run" && step === "boxes" && awaiting && (
            <>
              {note && <ServerNote text={note} />}
              <BoxGroup title="Deal-specific (required)" fields={dealSpecific} values={values} setValues={setValues} invalid={invalid} disabled={busy} />
              <BoxGroup title="Agent-prefilled (cited — review before running)" fields={agentFilled} values={values} setValues={setValues} invalid={invalid} disabled={busy} />
              <BoxGroup title="Conventions (prefilled — edit if needed)" fields={conventions} values={values} setValues={setValues} invalid={invalid} disabled={busy} />
              <div className="mb-[16px] flex flex-col gap-[5px]">
                <label className="mono text-[10px] tracking-[0.12em] uppercase text-t3">
                  Assumptions source <span className="text-red">*</span>
                  <span className="text-t4 normal-case tracking-normal"> — every assumption needs a source (deck page, CH filing, operator judgment + date)</span>
                </label>
                <input value={sourceNote} onChange={(e) => setSourceNote(e.target.value)} disabled={busy}
                  placeholder='e.g. "deck pp.30-33 + CH FY24 + operator structuring decisions 2026-06-09"'
                  className={cn(INPUT_BASE, invalid.has("__source") ? "border-red" : "border-line-2")} />
              </div>
              <div className="flex items-center justify-between gap-[10px]">
                <span className="text-[10.5px] text-t4">expires {fmtExpiry(awaiting.expires_at)}</span>
                <Primary onClick={() => void submitBoxes()} disabled={busy} busy={busy}
                  label="Run engine" busyLabel="Running engine… (~1 min)" />
              </div>
            </>
          )}

          {/* ── RESULT ─────────────────────────────────────────────────── */}
          {mode === "run" && step === "result" && result && (
            <>
              <div className="mb-[14px] grid grid-cols-3 gap-[10px]">
                <KpiCard label="Sponsor IRR · central" value={result.returns.irr_central_pct != null ? result.returns.irr_central_pct.toFixed(1) : "—"} unit="%" />
                <KpiCard label="MOIC · central" value={result.returns.moic_central_x != null ? result.returns.moic_central_x.toFixed(1) : "—"} unit="x" />
                <KpiCard label="Equity cheque" value={result.returns.equity_cheque_m.toFixed(1)} unit="m" />
              </div>
              <div className="mb-[12px] flex flex-col gap-[5px] rounded-lg border border-line bg-bg-2 px-[12px] py-[10px] text-[11.5px]">
                <KV k="FTEV" v={`${result.headline.ftev_m.toFixed(1)}m @ ${result.headline.entry_multiple}x entry`} />
                <KV k="Net debt at close" v={`${result.headline.net_debt_at_close_m.toFixed(2)}m (TLA ${result.headline.tla_quantum_m.toFixed(2)}m / TLB ${result.headline.tlb_quantum_m.toFixed(2)}m)`} />
                <KV k="Sponsor equity" v={`${result.headline.sponsor_equity_m.toFixed(2)}m`} />
                <KV k="Stub · hold" v={`${result.headline.stub_period} · ${result.returns.hold_years}y (exit ${result.headline.exit_multiple}x)`} />
                <KV k="Validation" v={result.validation.engine_rules_passed && result.validation.sources_and_uses_ties ? "Iron Law passed ✓" : `⚠ ${result.validation.engine_status}`} />
              </div>
              {result.warnings.length > 0 && (
                <div className="mb-[12px] flex flex-col gap-[4px]">
                  {result.warnings.map((w, i) => (
                    <div key={i} className="flex items-start gap-[6px] text-[11px] text-amber">
                      <AlertTriangle size={12} className="mt-[1px] shrink-0" /> <span>{w}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex items-center justify-between gap-[10px] rounded-lg border border-line bg-bg-2 px-[12px] py-[9px]">
                <span className="truncate mono text-[11px] text-t2" title={result.output_xlsx_path}>{result.output_xlsx_path}</span>
                <button type="button"
                  onClick={() => { void navigator.clipboard.writeText(result.output_xlsx_path).then(() => setCopied(true)); }}
                  className="flex shrink-0 items-center gap-[5px] rounded-lg border border-accent-line bg-accent-soft px-[11px] py-[4px] text-[11px] font-medium text-accent transition-colors hover:brightness-110">
                  {copied ? <><Check size={12} /> Copied</> : "Copy path"}
                </button>
              </div>
              <div className="mt-[14px] flex justify-end">
                <button type="button" onClick={startOver}
                  className="text-[11px] text-t3 underline decoration-dotted underline-offset-2 transition-colors hover:text-t1">
                  ← start a new intake
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── shared input chrome (mirrors the inset Field/Budget controls) ────────────
const INPUT_BASE =
  "w-full rounded-lg bg-bg-2 border px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors focus:border-accent-line placeholder:text-t4 disabled:opacity-60";
const INPUT_CLASS = cn(INPUT_BASE, "border-line-2");

// ── manifest-driven form pieces ──────────────────────────────────────────────

function BoxGroup({ title, fields, values, setValues, invalid, disabled }: {
  title: string;
  fields: LBOBoxField[];
  values: Record<string, string>;
  setValues: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  invalid: Set<string>;
  disabled: boolean;
}) {
  if (fields.length === 0) return null;
  return (
    <div className="mb-[16px]">
      <div className="mb-[9px] mono text-[10px] tracking-[0.12em] uppercase text-t3">{title}</div>
      <div className="grid grid-cols-3 gap-[10px]">
        {fields.map((f) => (
          <BoxInput key={f.key} field={f} value={values[f.key] ?? ""} invalid={invalid.has(f.key)} disabled={disabled}
            onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))} />
        ))}
      </div>
    </div>
  );
}

function BoxInput({ field, value, onChange, invalid, disabled }: {
  field: LBOBoxField; value: string; onChange: (v: string) => void; invalid: boolean; disabled: boolean;
}) {
  const unit = field.unit ? ` (${field.unit})` : "";
  const base = cn(INPUT_BASE, invalid ? "border-red" : "border-line-2");
  return (
    <div className="flex flex-col gap-[5px]">
      <label className="mono flex min-h-[28px] items-end text-[10px] leading-[14px] tracking-[0.1em] uppercase text-t3" title={field.help}>
        <span>{field.label}{unit}{field.help ? <span className="text-t4"> ⓘ</span> : null}</span>
      </label>
      {field.type === "select" && field.options ? (
        <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} className={base}>
          {field.options.map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
        </select>
      ) : (
        <input value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}
          placeholder={field.type === "date" ? "YYYY-MM-DD" : (field.required ? "required" : undefined)} className={base} />
      )}
      {(field.source || field.provided_via) && (
        // D4 per-box provenance: where the agent transcribed this value from.
        // A provided_via-only field (e.g. the client_fs ack — agent-proposed,
        // not document-cited) still shows its producer visibly (codex slice-3
        // SEV-3), not just in a hover title.
        <div className="truncate text-[9.5px] text-t4"
          title={`${field.source ?? "no document citation"} · via ${field.provided_via ?? "agent"}`}>
          {field.source ? `src · ${field.source}` : `via · ${field.provided_via}`}
        </div>
      )}
    </div>
  );
}

// ── small presentational helpers (mirror CompsBuildModal) ────────────────────

function TabBtn({ active, disabled, onClick, label }: {
  active: boolean; disabled?: boolean; onClick: () => void; label: string;
}) {
  return (
    <button type="button" role="tab" aria-selected={active} disabled={disabled} onClick={onClick}
      className={cn("-mb-[1px] border-b-2 px-[12px] py-[9px] text-[12px] transition-colors disabled:cursor-default disabled:opacity-40",
        active ? "border-accent font-medium text-accent" : "border-transparent text-t3 hover:text-t1")}>
      {label}
    </button>
  );
}

function Field({ label, value, onChange, placeholder, invalid, disabled, inputRef }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
  invalid?: boolean; disabled?: boolean; inputRef?: React.Ref<HTMLInputElement>;
}) {
  return (
    <div className="flex flex-col gap-[5px]">
      <label className="mono text-[10px] tracking-[0.1em] uppercase text-t3">{label}</label>
      <input ref={inputRef} value={value} placeholder={placeholder} disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={cn(INPUT_BASE, invalid ? "border-red" : "border-line-2")} />
    </div>
  );
}

/** Labelled textarea — same inset chrome as Field, with an optional dim hint
 *  appended to the label and a mono body for path lists. */
function TextArea({ label, hint, value, onChange, placeholder, rows, disabled, mono }: {
  label: string; hint?: string; value: string; onChange: (v: string) => void;
  placeholder?: string; rows: number; disabled?: boolean; mono?: boolean;
}) {
  return (
    <div className="flex flex-col gap-[5px]">
      <label className="mono text-[10px] tracking-[0.1em] uppercase text-t3">
        {label}{hint && <span className="normal-case tracking-normal text-t4"> {hint}</span>}
      </label>
      <textarea value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}
        rows={rows} placeholder={placeholder}
        className={cn(INPUT_BASE, "resize-y border-line-2", mono && "mono text-[11.5px]")} />
    </div>
  );
}

function Primary({ disabled, busy, onClick, label, busyLabel }: {
  disabled?: boolean; busy?: boolean; onClick: () => void; label: string; busyLabel: string;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className="rounded-lg bg-accent px-[16px] py-[8px] text-[12px] font-medium text-bg transition-colors hover:brightness-110 disabled:cursor-default disabled:opacity-40">
      {busy ? busyLabel : label}
    </button>
  );
}

/** Project-scope amber warning band (LBO is project-scoped — non-project
 *  workspaces 403). `detail` carries the mode-specific failure phrase. */
function ProjectScopeWarning({ detail, type }: { detail: string; type: string }) {
  return (
    <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-amber/40 bg-amber/10 px-[11px] py-[8px] text-[11.5px] text-amber">
      <AlertTriangle size={13} className="mt-[1px] shrink-0" />
      <span>LBO is project-scoped — {detail} from a {type} workspace. Switch to the deal's project first.</span>
    </div>
  );
}

/** Inline server re-suspend note (amber band). */
function ServerNote({ text }: { text: string }) {
  return (
    <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-amber/40 bg-amber/10 px-[11px] py-[8px] text-[11.5px] text-amber">
      <AlertTriangle size={13} className="mt-[1px] shrink-0" />
      <span>{text}</span>
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return <div className="flex justify-between gap-[8px]"><span className="text-t3">{k}</span><span className="tabular text-t1">{v}</span></div>;
}

// ── helpers ───────────────────────────────────────────────────────────────────

/** The paste-ready prompt for the attended agent-leg session (Phase 1, Option
 *  A — comps Path-A shape). References the orchestration brief by absolute
 *  path; the session reads it cold and follows its gates. The deal fields +
 *  doc paths ride along so the session starts with zero back-and-forth. */
function buildOrchestratePrompt(
  dealName: string,
  dealContext: string,
  docPaths: string[],
  workspace: { type: string; name: string },
): string {
  const ctx = dealContext
    ? dealContext.split("\n").map((l) => `    ${l}`)
    : ["    <describe the deal>"];
  const docs = docPaths.length
    ? docPaths.map((p) => `  - ${p}`)
    : ["  - <absolute doc path>"];
  return [
    `Orchestrate an LBO build (attended, agent-leg Phase 1). First read the brief:`,
    `  ${BRIEF_PATH}`,
    ``,
    `Deal:`,
    `  deal_name: ${dealName || "<deal>"}`,
    `  workspace: ${workspace.type}:${workspace.name} (confidential)`,
    `  deal_context:`,
    ...ctx,
    ``,
    `Deal docs (read these first; render image-heavy PDF pages, parse Excel forecasts):`,
    ...docs,
    ``,
    `Confirm the bridge is healthy (GET http://127.0.0.1:8765/api/health), then per the brief:`,
    `  • Build the 10-period operating model from the docs — every assumption [A]-flagged + sourced.`,
    `  • Confirm the model with me IN CHAT before firing anything.`,
    `  • Assemble the client_fs JSON (FULL currency values; 5 data rows + zero rows; FYE dates as join keys).`,
    `  • POST /api/workflows/lbo {mode:"intake", deal_name:"${dealName || "<deal>"}", workspace_type:"${workspace.type}", workspace_name:"${workspace.name}", deal_context, prefill (each box cited), client_fs}.`,
    `  • Hand back to me — I confirm the boxes in the dashboard LBO modal; the engine fires on resume.`,
    `  • Iron-Law gates on the result: engine validation empty + S&U tie TRUE + per-period EBITDA join.`,
    ``,
    `Source everything. No MNPI to cloud tools — the confidential deal docs stay in your attended session.`,
    `Don't restart the bridge.`,
  ].join("\n");
}

function isAwaiting(r: unknown): r is SkillAwaiting {
  return !!r && typeof r === "object" && (r as { status?: string }).status === "suspended";
}

/** The deal name from a suspension's prompt ("LBO intake for 'X': …" or the
 *  agent's "… needs clarification on N item(s) for 'X' …"). */
function guessDeal(s: SuspendedSkill): string {
  const m = /LBO intake for '([^']+)'/.exec(s.prompt)
    ?? /clarification on \d+ item\(s\) for '([^']+)'/.exec(s.prompt);
  return m?.[1] ?? s.workspace_name;
}

/** First paragraph of a (possibly note-prefixed) suspension prompt. */
function firstParagraph(prompt: string): string {
  return prompt.split("\n\n")[0] ?? prompt;
}

function fmtExpiry(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return `Workspace/MNPI refused (403): ${e.message}`;
    if (e.status === 409) return `Stale or already-resumed run (409): ${e.message} — reopen the modal to pick up the current pending intake.`;
    if (e.status === 410) return `Intake expired (410) — start a fresh one.`;
    if (e.status === 502) return `Engine run failed (502): ${e.message}`;
    if (e.status === 504) return `Engine timed out (504): ${e.message}`;
    return `Failed (${e.status}): ${e.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
