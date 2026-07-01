import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import type { DealTrackerResult } from "../types";

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * #front-door · deal-tracker paste modal.
 *
 * The deal-tracker skill extracts a precedent-transaction row from a pasted
 * news article and appends it to the precedent tracker workbook. `text` (the
 * article body) is REQUIRED; `url` is provenance only (never fetched). The
 * operator previews the extracted deal (dry-run) before committing the append:
 * the route refuses (422) when no target company can be extracted, and reports
 * skipped_duplicate when the deal is already tracked. Per the skill's Iron Law,
 * only multiples STATED in the article are recorded — nothing inferred.
 *
 * Modal shell mirrors ProviderOverrideModal (backdrop + ESC + inline error).
 */
export function DealTrackerModal({ open, onClose }: Props) {
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DealTrackerResult | null>(null);

  // Reset whenever the modal (re)opens.
  useEffect(() => {
    if (!open) return;
    setUrl(""); setText(""); setBusy(false); setError(null); setResult(null);
  }, [open]);

  // ESC to close (when not mid-submit).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const canSubmit = text.trim().length > 0 && !busy;

  const run = async (dryRun: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.dealTracker({ url: url.trim(), text: text.trim(), dry_run: dryRun });
      setResult(res);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? (e.status === 422 ? `No deal extracted — ${e.message}` : `Failed (${e.status}): ${e.message}`)
          : e instanceof Error ? `Failed: ${e.message}` : "Failed — see console.",
      );
    } finally {
      setBusy(false);
    }
  };

  const deal = result?.deal;
  const appended = result?.status === "appended";
  const duplicate = result?.status === "skipped_duplicate";

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-6 bg-bg/70 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="deal-tracker-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[560px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — intake/gated (deal) */}
        <div className="h-[3px] shrink-0 bg-amber" />
        {/* Header — title + kind chip + descriptive subtitle + close */}
        <div className="flex items-start justify-between gap-[12px] border-b border-line px-[22px] py-[16px]">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-[10px]">
              <h2 id="deal-tracker-title" className="text-[15px] font-semibold tracking-[-0.01em] text-t1">
                Deal tracker
              </h2>
              <Chip label="skill · internal" variant="accent" className="mono" />
            </div>
            <p className="mt-[5px] text-[12px] leading-[1.5] text-t3">
              Paste a deal article — extract the parties and append a row to the precedent tracker.
            </p>
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={busy} />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-[22px] py-[18px]">
          <label className="mb-[7px] block text-[10px] uppercase tracking-[0.12em] text-t3">
            Source URL <span className="text-t4 normal-case tracking-normal">(optional — provenance only, not fetched)</span>
          </label>
          <input
            value={url}
            onChange={(e) => { setUrl(e.target.value); setError(null); setResult(null); }}
            disabled={busy}
            placeholder="https://…"
            className="mb-[16px] w-full rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
          />

          <label className="mb-[7px] block text-[10px] uppercase tracking-[0.12em] text-t3">Article text</label>
          <textarea
            value={text}
            onChange={(e) => { setText(e.target.value); setError(null); setResult(null); }}
            disabled={busy}
            rows={8}
            placeholder="Paste the article body — the extractor reads only this text (no inferred multiples)."
            className="w-full resize-y rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[9px] text-[12.5px] leading-relaxed text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
          />

          {/* Extracted preview / result */}
          {deal && (
            <div className={cn(
              "mt-[16px] rounded-lg border bg-bg-2 px-[14px] py-[12px] text-[11.5px] text-t2",
              appended ? "border-green/40" : duplicate ? "border-line-2" : "border-accent-line",
            )}>
              <div className="mb-[10px] flex items-center gap-[8px]">
                <Chip
                  label={appended ? "Appended to tracker" : duplicate ? "Already tracked" : "Extracted (preview)"}
                  variant={appended ? "sage" : duplicate ? "neutral" : "accent"}
                />
                {deal.target_company && (
                  <span className="flex items-center gap-[5px] text-[10.5px] text-t3">
                    <span className="h-[6px] w-[6px] shrink-0 rounded-full bg-green" />
                    target found
                  </span>
                )}
              </div>
              {/* Extracted fields as discrete bordered key-chips */}
              <div className="flex flex-wrap gap-[8px]">
                <div className="min-w-[120px] flex-1 rounded-md border border-line-2 px-[10px] py-[7px]">
                  <div className="mb-[2px] mono text-[9px] uppercase tracking-[0.12em] text-t4">Target</div>
                  <div className="text-[12.5px] text-t1">
                    {deal.target_company || <span className="text-red">no target company extracted</span>}
                  </div>
                </div>
                {deal.bidder_company && (
                  <div className="min-w-[120px] flex-1 rounded-md border border-line-2 px-[10px] py-[7px]">
                    <div className="mb-[2px] mono text-[9px] uppercase tracking-[0.12em] text-t4">Acquirer</div>
                    <div className="text-[12.5px] text-t1">{deal.bidder_company}</div>
                  </div>
                )}
                {deal.enterprise_value_m != null && (
                  <div className="min-w-[120px] flex-1 rounded-md border border-line-2 px-[10px] py-[7px]">
                    <div className="mb-[2px] mono text-[9px] uppercase tracking-[0.12em] text-t4">Value</div>
                    <div className="tabular text-[12.5px] text-t1">{deal.currency || ""}{deal.enterprise_value_m}m</div>
                  </div>
                )}
                {deal.announced_date && (
                  <div className="min-w-[120px] flex-1 rounded-md border border-line-2 px-[10px] py-[7px]">
                    <div className="mb-[2px] mono text-[9px] uppercase tracking-[0.12em] text-t4">Announced</div>
                    <div className="tabular text-[12.5px] text-t1">{deal.announced_date}</div>
                  </div>
                )}
              </div>
              {deal.reported_ebitda_multiple_y1 != null && (
                <div className="mt-[8px] tabular text-t3">{deal.reported_ebitda_multiple_y1}x EBITDA</div>
              )}
              {deal.target_sector ? <div className="mt-[3px] text-[10.5px] text-t4">{deal.target_sector}</div> : null}
              {result && result.status === "appended" && result.row != null && (
                <div className="mt-[6px] text-[10.5px] text-ok-bright">row {result.row} · {result.workbook_path.split(/[/\\]/).pop()}</div>
              )}
              {result && result.status === "skipped_duplicate" && result.existing_row != null && (
                <div className="mt-[6px] text-[10.5px] text-t3">existing row {result.existing_row}</div>
              )}
              {result && result.warnings.length > 0 && (
                <div className="mt-[6px] text-[10.5px] text-amber">
                  {result.warnings.length} warning{result.warnings.length === 1 ? "" : "s"}: {result.warnings[0]}
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="mt-[14px] rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[11.5px] text-red">
              {error}
            </div>
          )}
        </div>

        {/* Footer — provenance note + Close / Preview / Append */}
        <div className="flex items-center justify-between gap-[14px] border-t border-line px-[22px] py-[14px]">
          <span className="max-w-[230px] text-[10.5px] leading-relaxed text-t4">
            No inferred multiples — only figures stated in the article.
          </span>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-lg px-[12px] py-[7px] text-[12px] text-t3 transition-colors hover:text-t1 disabled:cursor-default disabled:opacity-40"
            >Close</button>
            <button
              type="button"
              onClick={() => void run(true)}
              disabled={!canSubmit}
              className="rounded-lg border border-line-2 px-[14px] py-[7px] text-[12px] text-t2 transition-colors hover:border-accent-line hover:text-t1 disabled:cursor-default disabled:opacity-40 disabled:hover:border-line-2 disabled:hover:text-t2"
            >
              {busy ? "…" : "Preview"}
            </button>
            <button
              type="button"
              onClick={() => void run(false)}
              disabled={!canSubmit || !deal || appended}
              title={!deal ? "Preview first" : appended ? "Already appended" : "Append this deal to the tracker"}
              className="rounded-lg bg-accent px-[16px] py-[7px] text-[12px] font-medium text-bg transition-[filter,opacity] hover:brightness-110 disabled:cursor-default disabled:opacity-40 disabled:hover:brightness-100"
            >
              {busy ? "Saving…" : "Append →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
