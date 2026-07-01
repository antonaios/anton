import { useEffect, useRef, useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import type { BudgetAckAction, BudgetIncident, BudgetScope } from "../types";

interface Props {
  /** Incident to ack. Modal closes when null. */
  incident: BudgetIncident | null;
  onClose: () => void;
  /** Called after a successful ack so App.tsx can refetch incidents — the
   *  banner disappears (raise_cap) or stays (leave_paused) based on what
   *  the next /incidents poll returns. */
  onAcked: () => void;
}

/**
 * #57 ack modal — opens from BudgetBlockedBanner row click.
 *
 *   - Action radio: raise_cap | leave_paused
 *   - Number input for new_cap_usd, shown only when raise_cap (must be >
 *     incident.capUsd; server 422s otherwise)
 *   - REQUIRED comment textarea (server 422 if blank; submit button mirrors
 *     this by staying disabled until the trimmed comment is non-empty)
 *   - POST /api/budgets/incidents/{id}/ack → on 200, close + onAcked()
 *
 * Pattern mirrors WorkspacePickerModal (backdrop click + ESC close, inline
 * error region for server failures). Keeps the modal dumb — App.tsx owns
 * the open/close state.
 */
export function BudgetAckModal({ incident, onClose, onAcked }: Props) {
  const [action, setAction] = useState<BudgetAckAction>("raise_cap");
  const [newCap, setNewCap] = useState<string>("");
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commentRef = useRef<HTMLTextAreaElement | null>(null);

  // Reset whenever a fresh incident lands (or modal opens after a close)
  useEffect(() => {
    if (incident) {
      setAction("raise_cap");
      // Seed with a sensible default — 2× current cap rounded to 2dp.
      setNewCap((incident.capUsd * 2).toFixed(2));
      setComment("");
      setError(null);
      setSubmitting(false);
      const t = setTimeout(() => commentRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [incident]);

  // ESC to close (when not mid-submit)
  useEffect(() => {
    if (!incident) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [incident, submitting, onClose]);

  if (!incident) return null;

  const trimmedComment = comment.trim();
  const newCapNumber   = action === "raise_cap" ? Number.parseFloat(newCap) : null;
  const newCapValid    = action !== "raise_cap"
    || (Number.isFinite(newCapNumber) && (newCapNumber ?? 0) > incident.capUsd);
  const canSubmit      = trimmedComment.length > 0 && newCapValid && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.ackBudgetIncident(incident.id, {
        action,
        comment: trimmedComment,
        ...(action === "raise_cap" ? { new_cap_usd: newCapNumber ?? 0 } : {}),
      });
      onAcked();
      onClose();
    } catch (e) {
      setError(formatError(e));
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-6 bg-black/60 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !submitting) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="budget-ack-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[520px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — budget-block (oxblood) */}
        <div className="h-[3px] shrink-0 bg-red" />
        {/* Head — title + BLOCKED Chip + scope/provider subtitle + close */}
        <div className="flex flex-none items-center gap-[10px] border-b border-line px-[20px] py-[14px]">
          <h2 id="budget-ack-title" className="shrink-0 text-[15px] font-semibold text-t1">
            Budget paused
          </h2>
          <Chip variant="oxblood" icon={AlertTriangle} label="BLOCKED" />
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-t3" title={`incident ${incident.id}`}>
            {scopeSubtitle(incident.scope)}
          </span>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={submitting} size={15} />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-[20px] py-[18px]">
          {/* Incident summary block */}
          <div className="mb-[18px] rounded-lg border border-line bg-bg-2 px-[14px] py-[12px]">
            <div className="mono mb-[5px] text-[10px] uppercase tracking-[0.12em] text-t3">
              {incident.scope.kind === "global" ? "Global scope" : `${incident.scope.kind} scope`}
            </div>
            <div className="mb-[10px] text-[13px] text-t1">{scopeLabel(incident.scope)}</div>
            {/* Cap / spend stat strip — oxblood figures preserve the over-budget severity signal */}
            <div className="grid grid-cols-3 divide-x divide-line overflow-hidden rounded-[10px] border border-line">
              <div className="px-[10px] py-[8px]">
                <div className="mono text-[9px] uppercase tracking-[0.12em] text-t3">Cap</div>
                <div className="tabular tabular-nums mono mt-[3px] text-[18px] leading-none text-t1">{fmtUsd(incident.capUsd)}</div>
              </div>
              <div className="px-[10px] py-[8px]">
                <div className="mono text-[9px] uppercase tracking-[0.12em] text-t3">Spend</div>
                <div className="tabular tabular-nums mono mt-[3px] text-[18px] leading-none text-red">{fmtUsd(incident.currentSpendUsd)}</div>
              </div>
              <div className="px-[10px] py-[8px]">
                <div className="mono text-[9px] uppercase tracking-[0.12em] text-t3">Of cap</div>
                <div className="tabular tabular-nums mono mt-[3px] text-[18px] leading-none text-red">{incident.currentPct.toFixed(1)}%</div>
              </div>
            </div>
            {/* Incident meta — id · opened time · full scope id (audit context) */}
            <div className="mono mt-[7px] truncate text-[10.5px] text-t3" title={scopeId(incident.scope)}>
              incident #{incident.id.slice(-8)} · opened {fmtTime(incident.openedAt)} · hard {incident.hardPct.toFixed(0)}%
            </div>
            <div className="mono mt-[2px] truncate text-[10px] text-t4">scope {scopeId(incident.scope)}</div>
          </div>

          {/* Explanatory paragraph — what the block means + what's unaffected */}
          <p className="mb-[18px] text-[12px] leading-[1.55] text-t3">
            Hard cap reached. All {providerName(incident.scope.b ?? incident.scope.a) ?? "cloud"} calls on this scope are paused until you
            raise the cap or the month rolls over. Local (Ollama) routing is unaffected.
          </p>

          {/* Action radio */}
          <div className="mono mb-[8px] text-[10px] uppercase tracking-[0.12em] text-t3">Action</div>
          <div className="mb-[16px] flex flex-col gap-[8px]" role="radiogroup" aria-label="Ack action">
            <ActionOption
              checked={action === "raise_cap"}
              onSelect={() => setAction("raise_cap")}
              disabled={submitting}
              label="Acknowledge + raise cap"
              hint="Increases the policy cap; the scope unblocks immediately on the next call."
            />
            <ActionOption
              checked={action === "leave_paused"}
              onSelect={() => setAction("leave_paused")}
              disabled={submitting}
              label="Acknowledge + leave paused"
              hint="Cap unchanged; scope stays blocked until you raise the cap or the period rolls over (1st of next month UTC)."
            />
          </div>

          {/* New cap input — only when raise_cap */}
          {action === "raise_cap" && (
            <div className="mb-[16px]">
              <label className="mono mb-[6px] block text-[10px] uppercase tracking-[0.12em] text-t3">
                New cap (USD) <span className="normal-case tracking-normal text-t4">— must be &gt; current {fmtUsd(incident.capUsd)}</span>
              </label>
              <input
                type="number"
                step="0.01"
                min={incident.capUsd}
                value={newCap}
                onChange={(e) => { setNewCap(e.target.value); setError(null); }}
                disabled={submitting}
                className={cn(
                  "tabular tabular-nums w-full rounded-lg border bg-bg-2 px-[12px] py-[8px] text-[13px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60",
                  newCap && !newCapValid ? "border-red/50" : "border-line-2",
                )}
              />
              {newCap && !newCapValid && (
                <div className="mt-[5px] text-[11px] text-red">
                  Must be a number greater than {fmtUsd(incident.capUsd)}.
                </div>
              )}
            </div>
          )}

          {/* Required comment */}
          <label className="mono mb-[6px] block text-[10px] uppercase tracking-[0.12em] text-t3">
            Comment <span className="normal-case tracking-normal text-red">(required · audit trail)</span>
          </label>
          <textarea
            ref={commentRef}
            value={comment}
            onChange={(e) => { setComment(e.target.value); setError(null); }}
            disabled={submitting}
            rows={3}
            placeholder="Why are you acking? Audit trail."
            className="w-full resize-y rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[8px] text-[13px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
          />

          {/* Inline error */}
          {error && (
            <div className="mt-[12px] rounded-lg border border-red/40 bg-red/10 px-[12px] py-[7px] text-[11.5px] text-red">
              {error}
            </div>
          )}
        </div>

        {/* Foot */}
        <div className="flex flex-none items-center justify-between gap-3 border-t border-line bg-bg-2/40 px-[20px] py-[12px]">
          <span className="flex items-center gap-[6px] text-[11px] text-t3">
            <span className="mono rounded border border-line-2 bg-bg-2 px-[6px] py-[1px] text-[10px] tracking-[0.06em] text-t3">Esc</span>
            close
          </span>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="rounded-lg border border-line-2 px-[14px] py-[7px] text-[12.5px] text-t2 transition-colors hover:border-accent-line hover:text-t1 disabled:cursor-default disabled:opacity-40"
            >Cancel</button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!canSubmit}
              className={cn(
                "rounded-lg px-[16px] py-[7px] text-[12.5px] font-medium transition-colors disabled:cursor-default disabled:opacity-40",
                action === "raise_cap"
                  ? "bg-red text-bg hover:brightness-110"
                  : "border border-red/50 text-red hover:bg-red/10 hover:border-red/70",
              )}
            >
              {submitting ? "Acking…" : action === "raise_cap" ? "Raise cap →" : "Leave paused →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ActionOption({
  checked, onSelect, disabled, label, hint,
}: { checked: boolean; onSelect: () => void; disabled?: boolean; label: string; hint: string }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "flex items-start gap-[10px] rounded-lg border px-[14px] py-[11px] text-left transition-colors",
        checked ? "border-accent-line bg-accent-soft" : "border-line bg-bg-2 hover:border-line-2",
        disabled && "cursor-default opacity-60",
      )}
    >
      <span
        className={cn(
          "mt-[3px] flex h-[14px] w-[14px] shrink-0 items-center justify-center rounded-full border transition-colors",
          checked ? "border-accent" : "border-line-2",
        )}
        aria-hidden="true"
      >
        {checked && <span className="h-[6px] w-[6px] rounded-full bg-accent" />}
      </span>
      <span className="min-w-0">
        <span className="block text-[12.5px] font-medium text-t1">{label}</span>
        <span className="mt-[3px] block text-[11px] leading-[150%] text-t3">{hint}</span>
      </span>
    </button>
  );
}

function scopeLabel(scope: BudgetScope): string {
  if (scope.kind === "global") return "global";
  return `${scope.a ?? "?"} / ${scope.b ?? "?"}`;
}

// Provider telemetry key → display name for the header subtitle. Falls back to
// a capitalised raw key for an unknown provider rather than dropping it.
function providerName(key?: string | null): string | null {
  if (!key) return null;
  const k = key.toLowerCase();
  if (k === "anthropic" || k === "claude") return "Claude";
  if (k === "openai" || k === "codex" || k === "chatgpt") return "ChatGPT";
  if (k === "ollama") return "Ollama";
  return key.charAt(0).toUpperCase() + key.slice(1);
}

// Workspace scope id ("deal:Helix" / "project:Helix" / "general") → a readable
// name ("Project Helix" / "General"), so the header reads in plain language.
function workspaceName(id?: string | null): string | null {
  if (!id) return null;
  const [, name] = id.includes(":") ? id.split(":") : [undefined, id];
  if (!name) return id;
  const titled = name.charAt(0).toUpperCase() + name.slice(1);
  return titled === "General" ? "General" : `Project ${titled}`;
}

// Header subtitle — names the paused scope (and provider, where present) in
// plain language, e.g. "Project Helix · Claude". Derived from the incident
// scope only; no model name is invented when the wire doesn't carry one.
function scopeSubtitle(scope: BudgetScope): string {
  if (scope.kind === "global") return "Global cost-gate";
  if (scope.kind === "provider") return providerName(scope.a) ?? "Provider";
  if (scope.kind === "workspace") return workspaceName(scope.a) ?? "Workspace";
  // workspace_provider — a (workspace) · b (provider)
  const parts = [workspaceName(scope.a), providerName(scope.b)].filter(Boolean);
  return parts.length ? parts.join(" · ") : scopeLabel(scope);
}

// Reconstruct the canonical scope-id string (e.g.
// "workspace_provider:deal:Helix:anthropic") from the structured scope — the
// audit identifier the bridge keys the cap on.
function scopeId(scope: BudgetScope): string {
  if (scope.kind === "global") return "global";
  return [scope.kind, scope.a, scope.b].filter(Boolean).join(":");
}

// ISO-8601 UTC → "HH:MM" (UTC) for the opened-at meta. Falls back to the raw
// string if it doesn't parse.
function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1)    return `$${v.toFixed(3)}`;
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return e.message || "Invalid input (422).";
    if (e.status === 404) return "Incident not found — it may have been resolved by another tab.";
    if (e.status === 409) return "Incident already acknowledged elsewhere.";
    return `Failed (${e.status}): ${e.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
