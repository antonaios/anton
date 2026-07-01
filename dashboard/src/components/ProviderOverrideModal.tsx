import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { Chip, type ChipVariant } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import type { CloudModelAlias, PatchSkillProviderBody, SkillLLMParams, SkillProviderRow } from "../types";

interface Props {
  /** Row to override. Modal closes when null. Confidential/MNPI rows must NOT
   *  be passed (the bridge 422s a cloud override on them; the page disables the
   *  trigger). */
  row: SkillProviderRow | null;
  /** Absolute sidecar path, for the operator-commit reminder footnote. */
  sidecarPath?: string;
  onClose: () => void;
  /** Called after a successful PATCH (save or clear) so the page refetches. */
  onSaved: () => void;
}

// The sidecar `preferred_provider` vocabulary is {anthropic, openai,
// ollama-only, prefer_local}. "ollama-only" + "prefer_local" both map to the
// "ollama" allow-list key: ollama-only forces the local lane (fail-closed),
// prefer_local downgrades a public/internal CLOUD pick to local to save tokens
// (#llm-routing-postjune15 P2). An option is offered only when its allow-list
// key is in the row's allowed_providers (so a legitimate pick can't 422 on
// preferred∉allowed).
const OPTION_DEFS: { value: string; allowKey: string; label: string }[] = [
  { value: "anthropic",    allowKey: "anthropic", label: "Claude (anthropic)" },
  { value: "openai",       allowKey: "openai",    label: "ChatGPT (openai)" },
  { value: "ollama-only",  allowKey: "ollama",    label: "Ollama (local only)" },
  { value: "prefer_local", allowKey: "ollama",    label: "Prefer local (downgrade)" },
];

function providerOptions(allowed: string[]): { value: string; label: string }[] {
  const allow = allowed.length ? allowed : ["anthropic", "openai", "ollama"];
  return OPTION_DEFS.filter((o) => allow.includes(o.allowKey))
    .map(({ value, label }) => ({ value, label }));
}

// Operator-selectable cloud MODEL pins (#llm-routing-postjune15 P2 Task 3).
// Consumed only on the Claude (anthropic) lane; "" = lane default (no pin).
// opus-1m carries the 1M context window; P4 pinned the id (Opus 4.8 [1m] on the
// CLI / native 4.8 on the API). Local / codex lanes ignore the pin.
const MODEL_OPTIONS: { value: "" | CloudModelAlias; label: string }[] = [
  { value: "",        label: "Lane default" },
  { value: "opus",    label: "Opus (opus)" },
  { value: "sonnet",  label: "Sonnet (sonnet)" },
  { value: "haiku",   label: "Haiku (haiku)" },
  { value: "opus-1m", label: "Opus · 1M context (opus-1m)" },
];

const MODEL_VALUES = new Set<string>(MODEL_OPTIONS.map((m) => m.value));

// Map the row's sensitivity string onto a Chip variant for the header tag.
// Mirrors the sensitivity map (public=green, internal=t2, confidential=amber,
// mnpi=red). Confidential/MNPI rows are never passed here (the page disables the
// trigger + the bridge fail-closes), but the mapping stays exhaustive so a
// mislabelled row still reads correctly rather than silently neutral.
function sensitivityVariant(s: string): ChipVariant {
  const k = s.toLowerCase();
  if (k === "public") return "public";
  if (k === "confidential") return "confidential";
  if (k === "mnpi") return "mnpi";
  return "internal";
}

// Shared inset control chrome — matches the Field primitive (bg-bg-2 + line-2
// hairline, focus → accent-line), reused inline for the native <select> and the
// numeric <input> so they sit on the v5 inset surface.
const CONTROL_CLASS =
  "w-full rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[8px] text-[13px] text-t1 outline-none transition-colors focus:border-accent-line";
const LABEL_CLASS = "mono text-t3 text-[10px] tracking-[0.1em] uppercase";

/**
 * #llm-routing-tier-2 · per-skill provider override modal.
 *
 * Writes the operator sidecar (PATCH /api/skills/<key>/provider). Distinct from
 * the per-window sensitivity override (right-rail, temporary) — this override is
 * PERSISTENT until cleared. Mirrors BudgetAckModal's shell (backdrop + ESC +
 * inline error). On a 422 we surface the bridge's SPECIFIC reason string
 * verbatim (it explains why — disallowed provider, cloud-on-confidential, etc.).
 */
export function ProviderOverrideModal({ row, sidecarPath, onClose, onSaved }: Props) {
  const options = useMemo(() => providerOptions(row?.allowedProviders ?? []), [row]);

  const [provider, setProvider]   = useState("");
  const [model, setModel]         = useState<string>("");   // "" = lane default (no pin)
  const [tempEnabled, setTempEnabled] = useState(false);
  const [temp, setTemp]           = useState(0.2);
  const [maxTokens, setMaxTokens] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError]         = useState<string | null>(null);

  // Snapshot of the seeded values, so Save can send ONLY the fields the operator
  // actually changed (preserving the bridge's PATCH merge semantics — a temp-only
  // edit must not freeze the resolved provider into the sidecar, and vice versa).
  const initRef = useRef<{ provider: string; model: string; tempEnabled: boolean; temp: number; maxTokens: string } | null>(null);

  // Seed the form whenever a fresh row lands.
  useEffect(() => {
    if (!row) return;
    // Default the dropdown to the current effective provider when it's a valid
    // option, else the existing sidecar pick, else the first allowed option.
    const opts = providerOptions(row.allowedProviders);
    const cur = opts.find((o) => o.value === row.effectiveProvider)?.value
      ?? (row.override?.preferredProvider
            && opts.some((o) => o.value === row.override?.preferredProvider)
            ? row.override.preferredProvider
            : undefined)
      ?? opts[0]?.value
      ?? "";
    setProvider(cur);

    // Seed the cloud MODEL pin from the SIDECAR override ONLY ("" = no pin /
    // lane default). Deliberately NOT from effective/frontmatter: the dropdown
    // represents the operator's sidecar override, so (a) an unchanged inherited
    // value is never frozen into the sidecar, and (b) the operator CAN
    // explicitly pin a currently-inherited model (Codex SEV-2). The resolved
    // effective model is shown separately in the "Currently" summary. A
    // hand-edited bad alias seeds to "" (the row's effective_error flags it,
    // and Clear override removes it).
    const curModelRaw = row.override?.preferredModel ?? "";
    const curModel = MODEL_VALUES.has(curModelRaw) ? curModelRaw : "";
    setModel(curModel);

    const curTemp = row.effectiveLlmParams.temperature ?? row.llmParams.temperature ?? null;
    const curTempEnabled = curTemp != null;
    const curTempVal = curTemp != null ? curTemp : 0.2;
    setTempEnabled(curTempEnabled);
    setTemp(curTempVal);

    const curMax = row.effectiveLlmParams.maxTokens ?? row.llmParams.maxTokens ?? null;
    const curMaxStr = curMax != null ? String(curMax) : "";
    setMaxTokens(curMaxStr);

    initRef.current = { provider: cur, model: curModel, tempEnabled: curTempEnabled, temp: curTempVal, maxTokens: curMaxStr };

    setError(null);
    setSubmitting(false);
  }, [row]);

  // ESC to close (when not mid-submit).
  useEffect(() => {
    if (!row) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !submitting) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [row, submitting, onClose]);

  if (!row) return null;

  const maxTokensNum = maxTokens.trim() ? Number.parseInt(maxTokens.trim(), 10) : null;
  const maxTokensValid = maxTokens.trim() === ""
    || (Number.isFinite(maxTokensNum) && (maxTokensNum ?? 0) > 0);

  // Change-tracking that respects BOTH the bridge's merge semantics AND the fact
  // that a sent llm_params REPLACES the sidecar dict wholesale. The form is seeded
  // from the EFFECTIVE resolution, which may inherit temperature/max_tokens from
  // frontmatter — so we must not let an untouched inherited sibling ride along
  // into the sidecar (Codex SEV-2, rounds 1+2). An outgoing llm_params key is kept
  // only when the operator CHANGED it, or it ALREADY existed in the sidecar
  // (row.override) — never merely because it was inherited.
  const init = initRef.current;
  const initMaxNum = init?.maxTokens.trim() ? Number.parseInt(init.maxTokens.trim(), 10) : null;
  const sidecarLlm: SkillLLMParams = row.override?.llmParams ?? {};

  const providerChanged = !!init && provider !== init.provider;
  const tempChanged = !!init && (tempEnabled !== init.tempEnabled || (tempEnabled && temp !== init.temp));
  const maxChanged  = maxTokensNum !== initMaxNum;

  // Cloud MODEL pin (#llm-routing-postjune15 P2 Task 3). The pin is only
  // consumed on the Claude lane, so we only WRITE it when provider==anthropic.
  const providerIsAnthropic = provider === "anthropic";
  const modelChanged = !!init && model !== init.model;
  const modelPinnedInSidecar = row.override?.preferredModel != null;
  // The partial PATCH can SET/replace a model pin but cannot individually clear
  // one (null === "leave unchanged"; only clear:true drops the whole entry). So
  // block the single transition it can't express — a sidecar-pinned model →
  // lane default — and steer the operator to Clear override.
  const modelClearUnsupported =
    providerIsAnthropic && modelChanged && model === "" && modelPinnedInSidecar;
  const modelWillSend =
    providerIsAnthropic && modelChanged && model !== "" && !modelClearUnsupported;

  // The llm_params dict that should END UP in the sidecar after this save.
  const outLlm: { temperature?: number; max_tokens?: number } = {};
  if (tempChanged) { if (tempEnabled) outLlm.temperature = temp; }
  else if (sidecarLlm.temperature != null) outLlm.temperature = sidecarLlm.temperature;
  if (maxChanged) { if (maxTokensNum != null) outLlm.max_tokens = maxTokensNum; }
  else if (sidecarLlm.maxTokens != null) outLlm.max_tokens = sidecarLlm.maxTokens;

  // What the sidecar already holds — for a true change check (don't rewrite an
  // identical dict, and don't create an empty entry that accomplishes nothing).
  const existingLlm: { temperature?: number; max_tokens?: number } = {};
  if (sidecarLlm.temperature != null) existingLlm.temperature = sidecarLlm.temperature;
  if (sidecarLlm.maxTokens != null) existingLlm.max_tokens = sidecarLlm.maxTokens;

  const llmChanged = JSON.stringify(outLlm) !== JSON.stringify(existingLlm);
  const dirty = providerChanged || llmChanged || modelWillSend;
  const canSave = dirty && maxTokensValid && !submitting && !modelClearUnsupported;

  // Send only what changed: preferred_provider only if it changed; llm_params only
  // if the resulting sidecar dict differs from what's already there. outLlm carries
  // the FULL intended dict (changed keys + genuinely pre-existing sidecar keys)
  // because the bridge replaces llm_params wholesale — inherited-from-frontmatter
  // siblings are deliberately excluded so a later frontmatter edit isn't masked.
  const buildBody = (): PatchSkillProviderBody => {
    const body: PatchSkillProviderBody = {};
    if (providerChanged) body.preferred_provider = provider;
    if (modelWillSend) body.preferred_model = model;
    if (llmChanged) body.llm_params = outLlm;
    return body;
  };

  const run = async (body: PatchSkillProviderBody) => {
    setSubmitting(true);
    setError(null);
    try {
      await api.patchSkillProvider(row.key, body);
      onSaved();
      onClose();
    } catch (e) {
      // 422 carries the bridge's specific reason in the message — surface it.
      setError(formatError(e));
      setSubmitting(false);
    }
  };

  const hasOverride = row.override != null;
  const allowedList = (row.allowedProviders.length ? row.allowedProviders : ["anthropic", "openai", "ollama"]).join(", ");

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-6 backdrop-blur-sm bg-black/70"
      onClick={(e) => { if (e.target === e.currentTarget && !submitting) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="provider-override-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[520px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — intake/gated (override) */}
        <div className="h-[3px] shrink-0 bg-amber" />
        {/* Header — title + skill key/sensitivity chip + descriptive subtitle + close */}
        <div className="flex items-start justify-between gap-[12px] border-b border-line px-[22px] py-[16px]">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-[10px]">
              <h2 id="provider-override-title" className="shrink-0 text-[15px] font-semibold tracking-[-0.01em] text-t1">
                Override provider
              </h2>
              <span className="truncate font-mono text-[11.5px] text-t3" title={row.key}>{row.key}</span>
              <Chip label={row.sensitivity} variant={sensitivityVariant(row.sensitivity)} className="shrink-0 rounded-[5px] uppercase tracking-[0.08em]" />
            </div>
            <p className="mt-[5px] text-[12px] leading-[1.5] text-t3">
              Pins provider, model &amp; sampling for this skill — overrides the resolution chain.
            </p>
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={submitting} />
        </div>

        {/* Body */}
        <div className="overflow-y-auto px-[22px] py-[18px]">
          {/* Current resolution summary */}
          <div className="mb-[18px] rounded-lg border border-line bg-bg-2 px-[12px] py-[10px] text-[11px] text-t2">
            <div className="flex items-baseline justify-between">
              <span className={LABEL_CLASS}>Resolved now</span>
              <span className="mono text-[10px] uppercase tracking-[0.08em] text-t3">
                via {row.effectiveSource}
              </span>
            </div>
            <div className="mt-[4px] text-[12.5px] text-t1">
              {row.effectiveProvider}
              {row.effectiveModel && (
                <span className="text-t3"> · model {row.effectiveModel}</span>
              )}
              {row.effectiveLlmParams.temperature != null && (
                <span className="text-t3"> · temp {row.effectiveLlmParams.temperature}</span>
              )}
              {row.effectiveLlmParams.maxTokens != null && (
                <span className="text-t3"> · max {row.effectiveLlmParams.maxTokens}</span>
              )}
            </div>
            <div className="mt-[3px] text-[10.5px] text-t3">
              allowed: {allowedList}
            </div>
          </div>

          {/* Provider + cloud model pin — two-column density */}
          <div className="mb-[6px] grid grid-cols-2 gap-[18px]">
            {/* Provider */}
            <div>
              <label className="mb-[6px] block">
                <span className={LABEL_CLASS}>Provider</span>
              </label>
              <select
                value={provider}
                onChange={(e) => { setProvider(e.target.value); setError(null); }}
                disabled={submitting}
                className={cn(CONTROL_CLASS, "disabled:opacity-60")}
              >
                {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>

            {/* Cloud model pin (Claude lane only) — pill row */}
            <div>
              <div className="mb-[6px] flex items-center justify-between">
                <span className={LABEL_CLASS}>Cloud model</span>
                {!providerIsAnthropic && (
                  <span className="text-[9.5px] tracking-[0.04em] text-t4">Claude lane only</span>
                )}
              </div>
              <div
                className="flex flex-wrap gap-[6px]"
                title={providerIsAnthropic ? "Pin the Claude model for this skill" : "A model pin only applies on the Claude (anthropic) lane"}
              >
                {MODEL_OPTIONS.map((m) => {
                  const active = model === m.value;
                  return (
                    <button
                      key={m.value}
                      type="button"
                      onClick={() => { setModel(m.value); setError(null); }}
                      disabled={submitting || !providerIsAnthropic}
                      className={cn(
                        "rounded-[6px] px-[10px] py-[6px] text-[11.5px] transition-colors disabled:cursor-default disabled:opacity-50",
                        active
                          ? "border border-accent-line bg-accent-soft text-t1"
                          : "border border-line text-t2 hover:text-t1",
                      )}
                    >
                      {m.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
          {modelClearUnsupported ? (
            <div className="mb-[16px] text-[10.5px] leading-relaxed text-accent">
              This bridge can set or replace a model pin but can't remove only the pin — pick a model,
              or use <span className="uppercase tracking-[0.06em]">Clear override</span> to reset the whole entry.
            </div>
          ) : model === "opus-1m" ? (
            <div className="mb-[16px] text-[9.5px] leading-relaxed text-t4">
              Pins Claude 4.8 at the 1M context window (standard 4.8 pricing — no premium). The id is pinned (P4 shipped); a live cloud call awaits the paused headless billing.
            </div>
          ) : (
            <div className="mb-[16px] text-[9.5px] leading-relaxed text-t4">
              Optional — pins the Claude model for this skill. Ignored on non-Claude lanes.
            </div>
          )}

          {/* Temperature + Max tokens — two-column density */}
          <div className="grid grid-cols-2 items-start gap-[18px]">
            {/* Temperature */}
            <div>
              <div className="mb-[6px] flex items-center justify-between">
                <label className={cn(LABEL_CLASS, "flex items-center gap-[8px]")}>
                  <input
                    type="checkbox"
                    checked={tempEnabled}
                    onChange={(e) => { setTempEnabled(e.target.checked); setError(null); }}
                    disabled={submitting}
                    className="accent-accent"
                  />
                  Set temperature
                </label>
                <span className={cn("tabular text-[12px]", tempEnabled ? "text-t1" : "text-t4")}>
                  {tempEnabled ? temp.toFixed(2) : "—"}
                </span>
              </div>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={temp}
                onChange={(e) => { setTemp(Number.parseFloat(e.target.value)); setError(null); }}
                disabled={submitting || !tempEnabled}
                className="mb-[4px] w-full accent-accent disabled:opacity-40"
              />
              <div className="flex justify-between text-[9.5px] tracking-[0.06em] text-t4">
                <span>0.0 strict</span><span>1.0 creative</span>
              </div>
            </div>

            {/* Max tokens (optional) */}
            <div>
              <label className="mb-[6px] block">
                <span className={LABEL_CLASS}>Max tokens</span>
                <span className="ml-[6px] text-[10px] normal-case tracking-normal text-t4">(optional)</span>
              </label>
              <input
                value={maxTokens}
                onChange={(e) => { setMaxTokens(e.target.value.replace(/[^0-9]/g, "")); setError(null); }}
                inputMode="numeric"
                placeholder="provider default"
                disabled={submitting}
                className={cn(CONTROL_CLASS, "tabular placeholder:text-t4 disabled:opacity-60")}
              />
              {!maxTokensValid && (
                <div className="mt-[4px] text-[10.5px] text-red">Must be a positive whole number.</div>
              )}
            </div>
          </div>

          {/* Inline error — bridge's reason verbatim on 422 */}
          {error && (
            <div className="mt-[14px] rounded-lg border border-red/50 bg-red/10 px-[10px] py-[7px] text-[11.5px] text-red">
              {error}
            </div>
          )}

          {!dirty && !error && !modelClearUnsupported && (
            <div className="mt-[14px] text-[10.5px] text-t4">
              No changes to save — adjust the provider, model, or temperature (only changed fields are written).
            </div>
          )}

          {sidecarPath && (
            <div className="mt-[14px] text-[10px] leading-relaxed text-t4">
              Writes the operator sidecar — commit <span className="font-mono text-t3">{sidecarPath}</span> to persist (vault-owned, CLAUDE.md §5.7).
            </div>
          )}
        </div>

        {/* Footer — Clear override (left) · Cancel + Save (right) */}
        <div className="flex items-center justify-between gap-[8px] border-t border-line px-[22px] py-[14px]">
          <button
            type="button"
            onClick={() => void run({ clear: true })}
            disabled={submitting || !hasOverride}
            title={hasOverride ? "Remove the operator override; revert to SKILL.md frontmatter" : "No operator override set"}
            className="rounded-lg border border-line-2 px-[12px] py-[7px] text-[11.5px] tracking-[0.02em] text-t2 transition-colors hover:border-accent-line hover:text-t1 disabled:cursor-default disabled:opacity-30 disabled:hover:border-line-2 disabled:hover:text-t2"
          >
            Clear override
          </button>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="rounded-lg px-[12px] py-[7px] text-[12.5px] text-t3 transition-colors hover:text-t1 disabled:cursor-default disabled:opacity-40"
            >Cancel</button>
            <button
              type="button"
              onClick={() => void run(buildBody())}
              disabled={!canSave}
              className="rounded-lg border border-accent-line bg-accent-soft px-[16px] py-[8px] text-[12.5px] font-medium text-t1 transition-colors hover:brightness-110 disabled:cursor-default disabled:opacity-40 disabled:hover:brightness-100"
            >
              {submitting ? "Saving…" : "Save (persistent)"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    // 422 detail is the bridge's specific reason (disallowed provider,
    // cloud-on-confidential, bad temperature…) — render it verbatim.
    if (e.status === 422) return e.message || "Invalid override (422).";
    if (e.status === 404) return "Skill not found — the registry may have changed.";
    return `Failed (${e.status}): ${e.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
