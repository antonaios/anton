import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import type { CompsBuildBody, CompsBuildResult } from "../types";

/**
 * Comps-build launcher (#21-comps, COMPS-REDESIGN-2026-06-01). Two modes:
 *
 *   • ORCHESTRATE (Path A, recommended) — collects deal context and emits a
 *     paste-ready prompt for an attended Claude Code session, which runs the
 *     Anthropic research Skills (equity-research:screen / buyer-list /
 *     deep-research) and drives the stages with operator approval. The dash
 *     can't spawn a CLI, so this hands the operator a copy-paste prompt.
 *
 *   • RUN STAGES (manual) — a thin 4-stage stepper that fires
 *     POST /api/workflows/comps-build directly, threading the HMAC approval
 *     tokens between stages. It APPROVES THE BRIDGE'S PROPOSAL VERBATIM at
 *     each gate: the token signs the proposal, so down-selecting a subsector
 *     / peer / deal here would invalidate it (422). To narrow the universe,
 *     use the Orchestrate flow (submit a narrower candidate set) or trim the
 *     stamped workbook in Excel — consistent with the redesign principle
 *     (operator applies the numeric/valuation judgment in the final Excel).
 *     LFY+1 is left blank by design (operator fills it in Excel).
 *
 * Modal pattern mirrors BudgetAckModal (backdrop + ESC close, inline error,
 * dumb component — App owns open/close).
 */

interface Props {
  open: boolean;
  onClose: () => void;
  /** Default deal context — seeded from the active project workspace. */
  initialDeal?: { dealName?: string; target?: string; parentSector?: string };
}

type Mode = "orchestrate" | "manual";

// Accumulated approval state threaded across stages. Each token signs the
// bridge's proposal for its stage; we echo the proposal as the proposed_*
// set + send a (possibly-narrowed) approved_* subset on the next call.
//
// #21-comps-step-3: proposed* mirror the bridge's full Stage-N output so the
// operator can NARROW (deselect items) without re-firing the prior stage.
// The bridge verifies the HMAC against proposed_* and enforces
// approved_* ⊆ proposed_*. Initial state: approved_* = proposed_* (operator
// approves verbatim); each checkbox toggle narrows approved_* only.
interface Flow {
  proposedSubsectors: string[];
  approvedSubsectors: string[];
  subsectorsToken: string;
  proposedPeers: Record<string, string[]>;
  approvedPeers: Record<string, string[]>;
  proposedDeals: Record<string, string[]>;
  approvedDeals: Record<string, string[]>;
  peersToken: string;
  dealsToken: string;
  stage2BlocksToken: string;
  approvedAssumptions: Array<Record<string, unknown>>;
  assumptionsToken: string;
}

const EMPTY_FLOW: Flow = {
  proposedSubsectors: [], approvedSubsectors: [], subsectorsToken: "",
  proposedPeers: {}, approvedPeers: {},
  proposedDeals: {}, approvedDeals: {},
  peersToken: "", dealsToken: "",
  stage2BlocksToken: "", approvedAssumptions: [], assumptionsToken: "",
};

const BRIEF_PATH = "<repo>/session-briefs/SESSION-COMPS-ORCHESTRATION.md";

export function CompsBuildModal({ open, onClose, initialDeal }: Props) {
  const [mode, setMode] = useState<Mode>("orchestrate");
  const [dealName, setDealName] = useState("");
  const [target, setTarget] = useState("");
  const [parentSector, setParentSector] = useState("");

  // Manual stepper state.
  const [step, setStep] = useState(0);          // 0 inputs · 1 subsectors · 2 peers/deals · 3 acquire · 4 done
  const [result, setResult] = useState<CompsBuildResult | null>(null);
  const [flow, setFlow] = useState<Flow>(EMPTY_FLOW);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const firstFieldRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setMode("orchestrate");
      setDealName(initialDeal?.dealName ?? "");
      setTarget(initialDeal?.target ?? "");
      setParentSector(initialDeal?.parentSector ?? "");
      setStep(0); setResult(null); setFlow(EMPTY_FLOW);
      setBusy(false); setError(null); setCopied(false);
      const t = setTimeout(() => firstFieldRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [open, initialDeal]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const dealOk = /^[A-Za-z0-9_][A-Za-z0-9 _-]*$/.test(dealName) && dealName.length <= 64;
  const ctxOk = dealOk && target.trim().length > 0 && parentSector.trim().length > 0;
  const sectorSlug = parentSector.trim().toLowerCase().replace(/\s+/g, "-");

  const post = async (body: CompsBuildBody): Promise<CompsBuildResult | null> => {
    setBusy(true); setError(null);
    try {
      return await api.compsBuild(body);
    } catch (e) {
      setError(formatError(e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const baseBody = (stage: 0 | 1 | 2 | 3): CompsBuildBody => ({
    deal_name: dealName.trim(), target: target.trim(), parent_sector: sectorSlug, stage,
  });

  // ── Stage 0 → subsectors ──────────────────────────────────────────────────
  const runStage0 = async () => {
    const r = await post(baseBody(0));
    if (!r) return;
    setResult(r);
    const proposedSubs = asStringArray(r.approval_payload?.proposed);
    setFlow({ ...EMPTY_FLOW,
      proposedSubsectors: proposedSubs,
      approvedSubsectors: [...proposedSubs],   // initial: approve all (verbatim)
      subsectorsToken: r.subsectors_approval_token ?? "",
    });
    setStep(1);
  };

  // ── Subsector narrow toggle (called from Step-1 checkbox UI) ──────────────
  const toggleSubsector = (ss: string) => {
    setFlow((f) => {
      const approved = new Set(f.approvedSubsectors);
      if (approved.has(ss)) approved.delete(ss); else approved.add(ss);
      // Preserve the bridge's original proposal order.
      const ordered = f.proposedSubsectors.filter((s) => approved.has(s));
      return { ...f, approvedSubsectors: ordered };
    });
  };

  // ── Stage 1 → peers + deals ────────────────────────────────────────────────
  // Sends BOTH proposed_subsectors (full Stage-0 proposal, the HMAC-signed
  // universe) and approved_subsectors (the operator's narrowed subset). When
  // they're equal (operator approved verbatim) the backend's behaviour is
  // identical to pre-Step-3.
  const runStage1 = async () => {
    const r = await post({
      ...baseBody(1),
      proposed_subsectors: flow.proposedSubsectors,
      approved_subsectors: flow.approvedSubsectors,
      subsectors_approval_token: flow.subsectorsToken,
    });
    if (!r) return;
    setResult(r);
    const proposed = (r.approval_payload?.proposed ?? {}) as {
      coco_by_subsector?: Record<string, Array<{ ticker?: string }>>;
      cotrans_by_subsector?: Record<string, Array<{ deal_id?: string; target?: string }>>;
    };
    // Reconstruct the EXACT payloads the bridge signed (proposed_peers /
    // proposed_deals) so the Stage-2 HMAC verify matches. approved_* start
    // equal to proposed_* (= "approve verbatim"); the Step-2 UI can narrow.
    const proposedPeers: Record<string, string[]> = {};
    const proposedDeals: Record<string, string[]> = {};
    for (const ss of flow.approvedSubsectors) {
      proposedPeers[ss] = (proposed.coco_by_subsector?.[ss] ?? []).map((c) => c.ticker ?? "");
      proposedDeals[ss] = (proposed.cotrans_by_subsector?.[ss] ?? []).map((d) => d.deal_id ?? d.target ?? "");
    }
    setFlow((f) => ({ ...f,
      proposedPeers,
      approvedPeers: deepCopy(proposedPeers),
      proposedDeals,
      approvedDeals: deepCopy(proposedDeals),
      peersToken: r.peers_approval_token ?? "",
      dealsToken: r.deals_approval_token ?? "",
    }));
    setStep(2);
  };

  // ── Peer / deal narrow toggles (called from Step-2 checkbox UI) ───────────
  const togglePeer = (subsector: string, ticker: string) => {
    setFlow((f) => {
      const current = new Set(f.approvedPeers[subsector] ?? []);
      if (current.has(ticker)) current.delete(ticker); else current.add(ticker);
      // Preserve bridge proposal order.
      const ordered = (f.proposedPeers[subsector] ?? []).filter((t) => current.has(t));
      return { ...f, approvedPeers: { ...f.approvedPeers, [subsector]: ordered } };
    });
  };
  const toggleDeal = (subsector: string, dealId: string) => {
    setFlow((f) => {
      const current = new Set(f.approvedDeals[subsector] ?? []);
      if (current.has(dealId)) current.delete(dealId); else current.add(dealId);
      const ordered = (f.proposedDeals[subsector] ?? []).filter((d) => current.has(d));
      return { ...f, approvedDeals: { ...f.approvedDeals, [subsector]: ordered } };
    });
  };

  // ── Stage 2 → acquire data ─────────────────────────────────────────────────
  const runStage2 = async () => {
    const r = await post({
      ...baseBody(2),
      approved_subsectors: flow.approvedSubsectors,
      proposed_peers_by_subsector: flow.proposedPeers,
      approved_peers_by_subsector: flow.approvedPeers,
      proposed_deals_by_subsector: flow.proposedDeals,
      approved_deals_by_subsector: flow.approvedDeals,
      peers_approval_token: flow.peersToken,
      deals_approval_token: flow.dealsToken,
    });
    if (!r) return;
    setResult(r);
    const assumptions = r.approval_payload?.kind === "assumptions"
      ? asDictArray(r.approval_payload?.proposed) : [];
    setFlow((f) => ({ ...f,
      stage2BlocksToken: r.stage_2_blocks_approval_token ?? "",
      approvedAssumptions: assumptions,             // echoed verbatim = "blank" LFY+1 path
      assumptionsToken: r.assumptions_approval_token ?? "",
    }));
    setStep(3);
  };

  // ── Stage 3 → stamp ────────────────────────────────────────────────────────
  const runStage3 = async () => {
    const r = await post({
      ...baseBody(3),
      approved_subsectors: flow.approvedSubsectors,
      proposed_peers_by_subsector: flow.proposedPeers,
      approved_peers_by_subsector: flow.approvedPeers,
      proposed_deals_by_subsector: flow.proposedDeals,
      approved_deals_by_subsector: flow.approvedDeals,
      peers_approval_token: flow.peersToken,
      deals_approval_token: flow.dealsToken,
      stage_2_blocks_approval_token: flow.stage2BlocksToken,
      ...(flow.approvedAssumptions.length
        ? { approved_assumptions: flow.approvedAssumptions, assumptions_approval_token: flow.assumptionsToken }
        : {}),
    });
    if (!r) return;
    setResult(r);
    setStep(4);
  };

  // Helper: shallow copy of the proposed dict-of-lists so toggling one
  // subsector's tickers doesn't mutate the other subsectors' lists by reference.
  function deepCopy<T extends Record<string, string[]>>(o: T): T {
    const out = {} as T;
    for (const k of Object.keys(o)) (out as Record<string, string[]>)[k] = [...o[k]];
    return out;
  }

  const orchestratePrompt = buildOrchestratePrompt(dealName.trim(), target.trim(), sectorSlug);
  const copyPrompt = async () => {
    try { await navigator.clipboard.writeText(orchestratePrompt); setCopied(true); setTimeout(() => setCopied(false), 1800); }
    catch { setError("Clipboard write blocked — select + copy the prompt manually."); }
  };

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
      role="dialog" aria-modal="true" aria-labelledby="comps-build-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[640px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — intake/gated (comps) */}
        <div className="h-[3px] shrink-0 bg-amber" />
        {/* Header */}
        <div className="flex flex-none items-center justify-between gap-[12px] border-b border-line px-[22px] py-[16px]">
          <div className="flex min-w-0 items-center gap-[10px]">
            <h2 id="comps-build-title" className="text-[16px] font-semibold tracking-[-0.01em] text-t1">
              Comps build
            </h2>
            <Chip label="Operator-gated · Stage 0–3" variant="accent" />
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={busy} />
        </div>

        {/* Scrollable body */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* Mode tabs */}
          <div className="flex gap-[2px] border-b border-line px-[22px] pt-[12px]">
            <TabBtn active={mode === "orchestrate"} onClick={() => setMode("orchestrate")} label="Orchestrate (attended)" />
            <TabBtn active={mode === "manual"} onClick={() => setMode("manual")} label="Run stages (manual)" />
          </div>

          {/* Shared deal-context form */}
          <div className="border-b border-line px-[22px] py-[16px]">
            <div className="grid grid-cols-3 gap-[10px]">
              <Field label="Deal name" value={dealName} onChange={setDealName} inputRef={firstFieldRef}
                placeholder="Project-Apex" invalid={dealName.length > 0 && !dealOk} disabled={busy && mode === "manual"} />
              <Field label="Target" value={target} onChange={setTarget} placeholder="IHG / target co"
                disabled={busy && mode === "manual"} />
              <Field label="Parent sector" value={parentSector} onChange={setParentSector} placeholder="hospitality"
                disabled={busy && mode === "manual"} hint={parentSector && sectorSlug !== parentSector.trim() ? `→ ${sectorSlug}` : undefined} />
            </div>
            {dealName.length > 0 && !dealOk && (
              <div className="mt-[8px] text-[10.5px] text-red">Deal name: letters/digits/space/_/- only, ≤64 chars.</div>
            )}
          </div>

          {mode === "orchestrate"
            ? <OrchestratePane ctxOk={ctxOk} prompt={orchestratePrompt} copied={copied} onCopy={copyPrompt} />
            : <ManualPane
                step={step} result={result} flow={flow} busy={busy} ctxOk={ctxOk}
                onStage0={runStage0} onStage1={runStage1} onStage2={runStage2} onStage3={runStage3}
                onToggleSubsector={toggleSubsector}
                onTogglePeer={togglePeer}
                onToggleDeal={toggleDeal}
                onReset={() => { setStep(0); setResult(null); setFlow(EMPTY_FLOW); setError(null); }}
              />
          }

          {/* Inline error */}
          {error && (
            <div className="mx-[22px] mb-[16px] rounded-lg border border-red/40 bg-red/10 px-[11px] py-[8px] text-[11.5px] text-red whitespace-pre-wrap">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex flex-none items-center justify-between gap-[12px] border-t border-line px-[22px] py-[12px]">
          <span className="text-[10.5px] tracking-[0.04em] text-t3">
            <span className="mono mr-[5px] rounded border border-line-2 px-[5px] py-[1px] text-[9.5px] text-t2">ESC</span>
            to close
          </span>
          <span className="mono text-[10px] tracking-[0.04em] text-t4">brief · {BRIEF_PATH.split("/").pop()}</span>
        </div>
      </div>
    </div>
  );
}

// ── Orchestrate pane (A3) ─────────────────────────────────────────────────────
function OrchestratePane({ ctxOk, prompt, copied, onCopy }:
  { ctxOk: boolean; prompt: string; copied: boolean; onCopy: () => void }) {
  return (
    <div className="px-[22px] py-[18px]">
      <p className="mb-[14px] text-[12px] leading-[1.55] text-t2">
        Path A: paste this into an attended <span className="text-t1">Claude Code</span> session. It reads the
        orchestration brief, runs the research Skills (equity-research:screen / buyer-list / deep-research),
        and drives the 4 stages with your approval at each gate. The dashboard can't spawn a CLI — copy &amp; paste.
      </p>
      <div className="mono mb-[6px] text-[10px] tracking-[0.12em] uppercase text-t3">Paste-ready prompt</div>
      <pre className="max-h-[260px] overflow-y-auto whitespace-pre-wrap rounded-lg border border-line bg-bg-2 px-[12px] py-[10px] text-[11px] leading-[1.5] text-t1">{prompt}</pre>
      <div className="mt-[16px] flex items-center justify-end gap-[10px]">
        {!ctxOk && <span className="mr-auto text-[10.5px] text-t3">Fill deal name · target · parent sector above.</span>}
        <button type="button" onClick={onCopy} disabled={!ctxOk}
          className={cn("rounded-lg px-[16px] py-[8px] text-[12px] font-medium transition-colors disabled:cursor-default disabled:opacity-40",
            copied ? "border border-ok-bright text-ok-bright" : "bg-accent text-bg hover:brightness-110")}>
          {copied ? "Copied ✓" : "Copy prompt"}
        </button>
      </div>
    </div>
  );
}

// ── Manual stepper pane ───────────────────────────────────────────────────────
function ManualPane({ step, result, flow, busy, ctxOk, onStage0, onStage1, onStage2, onStage3, onToggleSubsector, onTogglePeer, onToggleDeal, onReset }: {
  step: number; result: CompsBuildResult | null; flow: Flow; busy: boolean; ctxOk: boolean;
  onStage0: () => void; onStage1: () => void; onStage2: () => void; onStage3: () => void;
  onToggleSubsector: (ss: string) => void;
  onTogglePeer: (subsector: string, ticker: string) => void;
  onToggleDeal: (subsector: string, dealId: string) => void;
  onReset: () => void;
}) {
  return (
    <div className="px-[22px] py-[16px]">
      <StepRail step={step} />
      <div className="mb-[14px] mt-[10px] text-[11px] leading-[1.5] text-t3">
        Each gate signs the bridge's proposal. Uncheck items to <span className="text-t2">narrow</span>{" "}
        without re-firing (#21-comps-step-3). Adding items is refused; the HMAC binds to the bridge's proposed set.
        LFY+1 is left blank by design.
      </div>

      {step === 0 && (
        <Primary disabled={!ctxOk || busy} busy={busy} onClick={onStage0} label="Propose subsectors →" busyLabel="Proposing…" />
      )}

      {step === 1 && result && (
        <>
          <NarrowableSubsectorList
            proposed={flow.proposedSubsectors}
            approved={flow.approvedSubsectors}
            onToggle={onToggleSubsector}
            disabled={busy}
          />
          <Warnings result={result} />
          <Primary disabled={busy || flow.approvedSubsectors.length === 0} busy={busy} onClick={onStage1}
            label={`Approve ${flow.approvedSubsectors.length} of ${flow.proposedSubsectors.length} subsector${flow.proposedSubsectors.length === 1 ? "" : "s"} · propose peers & deals →`}
            busyLabel="Running screen + deep-research…" />
        </>
      )}

      {step === 2 && result && (
        <>
          <NarrowablePeersDeals
            proposedPeers={flow.proposedPeers}
            approvedPeers={flow.approvedPeers}
            proposedDeals={flow.proposedDeals}
            approvedDeals={flow.approvedDeals}
            onTogglePeer={onTogglePeer}
            onToggleDeal={onToggleDeal}
            disabled={busy}
          />
          {trackerWrites(result) > 0 && (
            <div className="mb-[10px] text-[11px] text-ok-bright">+{trackerWrites(result)} new deal(s) written to the tracker (SSOT)</div>
          )}
          <Warnings result={result} />
          <Primary disabled={busy} busy={busy} onClick={onStage2} label="Approve universe · acquire sourced data →" busyLabel="Acquiring…" />
        </>
      )}

      {step === 3 && result && (
        <>
          <AcquireSummary result={result} assumptions={flow.approvedAssumptions.length} />
          <Warnings result={result} />
          <Primary disabled={busy} busy={busy} onClick={onStage3} label="Stamp workbook →" busyLabel="Stamping template…" />
        </>
      )}

      {step === 4 && result && (
        <DoneBlock result={result} onReset={onReset} />
      )}
    </div>
  );
}

function StepRail({ step }: { step: number }) {
  const labels = ["Input", "Subsectors", "Peers · Deals", "Acquire", "Stamp"];
  return (
    <div className="flex flex-wrap items-center gap-[5px]">
      {labels.map((l, i) => (
        <div key={l} className="flex items-center gap-[5px]">
          <span className={cn("inline-flex items-center gap-[5px] rounded-md border px-[8px] py-[3px] text-[10px] uppercase tracking-[0.06em]",
            i < step ? "border-ok-bright/50 text-ok-bright" : i === step ? "border-accent-line bg-accent-soft text-accent" : "border-line text-t3")}>
            <span className="mono text-[9px] tracking-normal opacity-70">{i + 1}</span>
            {l}
          </span>
          {i < labels.length - 1 && <span className={cn("h-px w-[10px] shrink-0", i < step ? "bg-ok-bright/50" : "bg-line-2")} />}
        </div>
      ))}
    </div>
  );
}

// #21-comps-step-3: narrowable subsector list. Each subsector is a checkbox
// — unchecked items drop out of approved_subsectors while proposed_subsectors
// stays equal to the bridge's full signed proposal. Operator can DROP, never
// ADD (the bridge's HMAC binds to the proposed set).
function NarrowableSubsectorList({ proposed, approved, onToggle, disabled }: {
  proposed: string[]; approved: string[]; onToggle: (ss: string) => void; disabled: boolean;
}) {
  const approvedSet = new Set(approved);
  return (
    <div className="mb-[12px]">
      <div className="mono mb-[7px] text-[10px] tracking-[0.12em] uppercase text-t3">
        Proposed subsectors · {approved.length}/{proposed.length} approved
      </div>
      <div className="flex flex-wrap gap-[6px]">
        {proposed.map((s) => {
          const on = approvedSet.has(s);
          return (
            <button key={s} type="button" disabled={disabled}
              onClick={() => onToggle(s)}
              className={cn(
                "rounded-md border px-[9px] py-[4px] text-[11px] transition-colors disabled:cursor-default disabled:opacity-40",
                on
                  ? "border-accent-line bg-accent-soft text-accent"
                  : "border-line bg-bg-2 text-t3 line-through"
              )}>
              {on ? "✓ " : ""}{s}
            </button>
          );
        })}
        {proposed.length === 0 && <span className="text-[11px] text-t3">none proposed</span>}
      </div>
      {approved.length < proposed.length && (
        <div className="mt-[7px] text-[10.5px] text-t3">
          Narrowing to {approved.length} of {proposed.length} — bridge verifies HMAC over full proposal + enforces subset.
        </div>
      )}
    </div>
  );
}

// #21-comps-step-3: narrowable peers + deals (dict-of-lists per subsector).
// Each ticker / deal_id is a togglable chip; operator can DROP individual
// items per subsector (or zero them all out for an entire subsector).
function NarrowablePeersDeals({ proposedPeers, approvedPeers, proposedDeals, approvedDeals, onTogglePeer, onToggleDeal, disabled }: {
  proposedPeers: Record<string, string[]>; approvedPeers: Record<string, string[]>;
  proposedDeals: Record<string, string[]>; approvedDeals: Record<string, string[]>;
  onTogglePeer: (subsector: string, ticker: string) => void;
  onToggleDeal: (subsector: string, dealId: string) => void;
  disabled: boolean;
}) {
  const subs = Array.from(new Set([...Object.keys(proposedPeers), ...Object.keys(proposedDeals)]));
  const totProposedPeers = subs.reduce((n, ss) => n + (proposedPeers[ss]?.filter(Boolean).length ?? 0), 0);
  const totApprovedPeers = subs.reduce((n, ss) => n + (approvedPeers[ss]?.filter(Boolean).length ?? 0), 0);
  const totProposedDeals = subs.reduce((n, ss) => n + (proposedDeals[ss]?.filter(Boolean).length ?? 0), 0);
  const totApprovedDeals = subs.reduce((n, ss) => n + (approvedDeals[ss]?.filter(Boolean).length ?? 0), 0);
  return (
    <div className="mb-[12px] flex flex-col gap-[8px]">
      <div className="mono text-[10px] tracking-[0.12em] uppercase text-t3">
        Proposed universe · {totApprovedPeers}/{totProposedPeers} peers · {totApprovedDeals}/{totProposedDeals} deals
      </div>
      {subs.map((ss) => {
        const peerSet = new Set(approvedPeers[ss] ?? []);
        const dealSet = new Set(approvedDeals[ss] ?? []);
        const peers = (proposedPeers[ss] ?? []).filter(Boolean);
        const deals = (proposedDeals[ss] ?? []).filter(Boolean);
        return (
          <div key={ss} className="rounded-lg border border-line bg-bg-2 px-[12px] py-[10px]">
            <div className="mb-[6px] text-[11.5px] font-medium text-t1">{ss}</div>
            {peers.length > 0 && (
              <div className="mb-[6px]">
                <div className="mono mb-[4px] text-[9.5px] tracking-[0.1em] uppercase text-t3">peers · {peerSet.size}/{peers.length}</div>
                <div className="flex flex-wrap gap-[5px]">
                  {peers.map((p) => {
                    const on = peerSet.has(p);
                    return (
                      <button key={p} type="button" disabled={disabled}
                        onClick={() => onTogglePeer(ss, p)}
                        className={cn(
                          "rounded-md border px-[7px] py-[2px] text-[10.5px] transition-colors disabled:cursor-default disabled:opacity-40",
                          on ? "border-accent-line bg-accent-soft text-accent" : "border-line bg-bg text-t3 line-through"
                        )}>
                        {p}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
            {deals.length > 0 && (
              <div>
                <div className="mono mb-[4px] text-[9.5px] tracking-[0.1em] uppercase text-t3">deals · {dealSet.size}/{deals.length}</div>
                <div className="flex flex-wrap gap-[5px]">
                  {deals.map((d) => {
                    const on = dealSet.has(d);
                    return (
                      <button key={d} type="button" disabled={disabled}
                        onClick={() => onToggleDeal(ss, d)}
                        className={cn(
                          "rounded-md border px-[7px] py-[2px] text-[10.5px] transition-colors disabled:cursor-default disabled:opacity-40",
                          on ? "border-accent-line bg-accent-soft text-accent" : "border-line bg-bg text-t3 line-through"
                        )}>
                        {d}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        );
      })}
      {subs.length === 0 && <span className="text-[11px] text-t3">no subsectors</span>}
    </div>
  );
}

function AcquireSummary({ result, assumptions }: { result: CompsBuildResult; assumptions: number }) {
  const blocks = result.blocks ?? [];
  let ccy = 0;
  for (const b of blocks) {
    const flags = (b as { ccy_flags?: unknown[] }).ccy_flags;
    if (Array.isArray(flags)) ccy += flags.length;
  }
  return (
    <div className="mb-[12px] flex flex-col gap-[4px] rounded-lg border border-line bg-bg-2 px-[12px] py-[10px] text-[11.5px] text-t2">
      <div>{blocks.length} block(s) acquired · sourced from the markets provider + tracker</div>
      {ccy > 0 && <div className="text-amber">{ccy} currency flag(s) — FYI annotations (e.g. GBp vs GBP), not blockers</div>}
      {assumptions > 0
        ? <div className="text-amber">{assumptions} peer(s) with no consensus LFY+1 → left blank (fill in Excel)</div>
        : <div className="text-t3">no LFY+1 gaps</div>}
    </div>
  );
}

function DoneBlock({ result, onReset }: { result: CompsBuildResult; onReset: () => void }) {
  const [copied, setCopied] = useState(false);
  const path = result.template_path ?? "";
  const copyPath = async () => {
    try { await navigator.clipboard.writeText(path); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* noop */ }
  };
  return (
    <div className="flex flex-col gap-[14px]">
      <div className="rounded-lg border border-ok-bright/40 bg-ok-bright/5 px-[14px] py-[12px]">
        <div className="mono mb-[8px] text-[10px] tracking-[0.1em] uppercase text-ok-bright">Workbook stamped ✓</div>
        <div className="grid grid-cols-2 gap-[10px] text-[11.5px] text-t2">
          <KV k="EV / EBITDA · median" v={fmtX(result.headline_ev_ebitda_median)} />
          <KV k="EV / Revenue · median" v={fmtX(result.headline_ev_revenue_median)} />
          <KV k="Peers" v={result.peer_count != null ? String(result.peer_count) : "—"} />
          <KV k="Deals" v={result.deal_count != null ? String(result.deal_count) : "—"} />
        </div>
      </div>
      <div>
        <div className="mono mb-[5px] text-[10px] tracking-[0.12em] uppercase text-t3">Output (open in Excel)</div>
        <div className="flex items-center gap-[8px]">
          <code className="flex-1 break-all rounded-lg border border-line bg-bg-2 px-[10px] py-[7px] text-[10.5px] text-t1">{path || "—"}</code>
          <button type="button" onClick={copyPath} disabled={!path}
            className={cn("mono rounded-lg border px-[12px] py-[7px] text-[10px] tracking-[0.06em] uppercase transition-colors disabled:opacity-40",
              copied ? "border-ok-bright text-ok-bright" : "border-line-2 text-t2 hover:border-accent-line hover:text-t1")}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
      {result.prior_archived_path && (
        <div className="text-[10.5px] text-t3">prior version archived → {result.prior_archived_path}</div>
      )}
      <div className="flex justify-end">
        <button type="button" onClick={onReset}
          className="rounded-lg border border-line-2 px-[14px] py-[7px] text-[11.5px] text-t2 transition-colors hover:border-accent-line hover:text-t1">
          Build another →
        </button>
      </div>
    </div>
  );
}

// ── small building blocks ─────────────────────────────────────────────────────
function TabBtn({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("-mb-[1px] border-b-2 px-[12px] py-[9px] text-[11.5px] transition-colors",
        active ? "border-accent text-accent" : "border-transparent text-t3 hover:text-t1")}>
      {label}
    </button>
  );
}

function Field({ label, value, onChange, placeholder, invalid, disabled, hint, inputRef }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
  invalid?: boolean; disabled?: boolean; hint?: string; inputRef?: React.Ref<HTMLInputElement>;
}) {
  return (
    <div className="flex flex-col gap-[6px]">
      <label className="mono text-[10px] tracking-[0.1em] uppercase text-t3">{label}{hint && <span className="text-t4 normal-case tracking-normal"> {hint}</span>}</label>
      <input ref={inputRef} value={value} placeholder={placeholder} disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={cn("w-full rounded-lg border bg-bg-2 px-[10px] py-[7px] text-[12px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60",
          invalid ? "border-red" : "border-line-2")} />
    </div>
  );
}

function Primary({ disabled, busy, onClick, label, busyLabel }: {
  disabled?: boolean; busy?: boolean; onClick: () => void; label: string; busyLabel: string;
}) {
  return (
    <div className="flex justify-end">
      <button type="button" onClick={onClick} disabled={disabled}
        className="rounded-lg bg-accent px-[16px] py-[8px] text-[12px] font-medium text-bg transition-colors hover:brightness-110 disabled:cursor-default disabled:opacity-40">
        {busy ? busyLabel : label}
      </button>
    </div>
  );
}

function Warnings({ result }: { result: CompsBuildResult }) {
  if (!result.warnings?.length) return null;
  return (
    <div className="mb-[10px] flex flex-col gap-[3px]">
      {result.warnings.map((w, i) => <div key={i} className="text-[10.5px] text-amber">⚠ {w}</div>)}
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return <div className="flex justify-between gap-[8px]"><span className="text-t3">{k}</span><span className="tabular text-t1">{v}</span></div>;
}

// ── helpers ───────────────────────────────────────────────────────────────────
function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
function asDictArray(v: unknown): Array<Record<string, unknown>> {
  return Array.isArray(v) ? (v as Array<Record<string, unknown>>) : [];
}
function trackerWrites(r: CompsBuildResult): number {
  const planned = r.approval_payload?.tracker_writes_planned;
  return Array.isArray(planned) ? planned.length : 0;
}
function fmtX(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? `${v.toFixed(1)}x` : "—";
}
function buildOrchestratePrompt(dealName: string, target: string, sectorSlug: string): string {
  return [
    `Orchestrate a comps build (Path A, operator-attended). First read the brief:`,
    `  ${BRIEF_PATH}`,
    ``,
    `Then run the pipeline for this deal, surfacing each gate to me for approval:`,
    `  deal_name:     ${dealName || "<deal>"}`,
    `  target:        ${target || "<target>"}`,
    `  parent_sector: ${sectorSlug || "<sector-slug>"}`,
    ``,
    `Confirm the bridge is healthy (GET http://127.0.0.1:8765/api/health), then:`,
    `  • Stage 0 — POST /api/workflows/comps-build (stage 0); show me the proposed subsectors.`,
    `  • Stage 1 — run equity-research:screen + investment-banking:buyer-list + deep-research for the`,
    `    approved subsectors, submit the candidates, and show me the proposed peers & deals.`,
    `  • Stage 2 — acquire sourced data; surface ccy flags. Leave LFY+1 blank (I'll fill in Excel).`,
    `  • Stage 3 — stamp the workbook; give me the output path + headline medians.`,
    ``,
    `Source every figure. No MNPI in cloud-skill prompts. Don't restart the bridge mid-build.`,
  ].join("\n");
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return `Gate refused (422): ${e.message}`;
    if (e.status === 403) return `Workspace/MNPI refused (403): ${e.message}`;
    if (e.status === 502) return `Template stamp failed (502): ${e.message}`;
    return `Failed (${e.status}): ${e.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
