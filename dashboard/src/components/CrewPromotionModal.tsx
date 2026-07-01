import { useEffect, useMemo, useState } from "react";
import { X, AlertTriangle, ShieldCheck, ShieldAlert } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { IconButton } from "./ui/IconButton";
import { Chip, type ChipVariant } from "./ui/Chip";
import type { AttestationDTO, CloudModelAlias, CrewProviderRow, PatchCrewProviderBody } from "../types";

interface Props {
  /** Crew to promote. Modal closes when null. NON-promotable (MNPI/confidential-
   *  locked) crews must NOT be passed — the bridge 422s a cloud promotion on them
   *  and the section disables the trigger. */
  row: CrewProviderRow | null;
  /** Absolute crew_overrides.yaml path, for the operator-commit reminder. */
  sidecarPath?: string;
  onClose: () => void;
  /** Called after a successful PATCH (promote or clear) so the section refetches. */
  onSaved: () => void;
}

// The crew sidecar `preferred_provider` vocabulary is {anthropic, openai, local}.
// "local" is the explicit keep-local / clear sentinel (the bridge clears the
// role/crew-level entry). Crews promote a GENERATION role to a frontier lane while
// extraction stays cheap-local — so the role selector is the point.
const PROVIDER_OPTS: { value: string; label: string }[] = [
  { value: "local",     label: "Local (keep on Ollama)" },
  { value: "anthropic", label: "Claude (anthropic)" },
  { value: "openai",    label: "ChatGPT (openai)" },
];

// Cloud MODEL pins (Claude lane only) — mirrors ProviderOverrideModal. "" = lane
// default (Opus). openai is single-model (gpt-5) so the pin is ignored there.
const MODEL_OPTIONS: { value: "" | CloudModelAlias; label: string }[] = [
  { value: "",        label: "Lane default (Opus)" },
  { value: "opus",    label: "Opus (opus)" },
  { value: "sonnet",  label: "Sonnet (sonnet)" },
  { value: "haiku",   label: "Haiku (haiku)" },
  { value: "opus-1m", label: "Opus · 1M context (opus-1m)" },
];
const MODEL_VALUES = new Set<string>(MODEL_OPTIONS.map((m) => m.value));

const CREW_LEVEL = "";   // role selector value for the crew-level default

function laneToProvider(lane?: string): string {
  if (!lane) return "local";
  if (lane.startsWith("codex")) return "openai";
  if (lane.startsWith("claude")) return "anthropic";
  return "local";
}

// Presentational only — map the crew's sensitivity lock to a header Chip. The
// lock string drives the colour (confidential = amber, MNPI = red); anything
// else (or none) reads as a neutral "inherits workspace tier" chip. This does
// NOT gate anything — the bridge + attestation checks below are the real gate.
function sensitivityChip(lock?: string | null): { label: string; variant: ChipVariant } {
  if (lock === "MNPI") return { label: "MNPI", variant: "mnpi" };
  if (lock === "confidential") return { label: "confidential", variant: "confidential" };
  if (lock) return { label: lock, variant: "neutral" };
  return { label: "inherits workspace tier", variant: "neutral" };
}

/**
 * #crew-cloud-promotion · per-crew (per-role) cloud-promotion modal.
 *
 * Writes the operator sidecar (PATCH /api/crew/<verb>/provider). One change per
 * save (a role, or the crew-level default). "Local" clears that role/level. The
 * bridge re-derives sensitivity SERVER-SIDE and force-locals confidential/MNPI —
 * a promoted role still only reaches cloud via the gated /api/crew/_llm, and the
 * subprocess never holds keys. On a 422 we surface the bridge's reason verbatim.
 */
export function CrewPromotionModal({ row, sidecarPath, onClose, onSaved }: Props) {
  const [role, setRole]   = useState<string>(CREW_LEVEL);
  const [provider, setProvider] = useState("local");
  const [model, setModel] = useState<string>("");      // "" = lane default
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attestations, setAttestations] = useState<AttestationDTO[]>([]);

  // Current routing per role, from the resolved promotion intent + local defaults.
  const promoted = useMemo(() => (row?.promotedRoles ?? {}), [row]);
  const localModels = useMemo(() => (row?.modelsDefault ?? {}), [row]);

  // Seed provider/model for a given role from the crew's current state.
  const seedFor = (r: string) => {
    if (!row) return;
    if (r === CREW_LEVEL) {
      const cp = row.override?.preferredProvider;
      setProvider(cp === "anthropic" || cp === "openai" ? cp : "local");
    } else {
      setProvider(laneToProvider(promoted[r]));
    }
    setModel("");   // model defaults to lane default; operator re-picks to pin
  };

  // Re-seed whenever a fresh crew lands.
  useEffect(() => {
    if (!row) return;
    setRole(CREW_LEVEL);
    seedFor(CREW_LEVEL);
    setError(null);
    setSubmitting(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row]);

  // MNPI crews: fetch the active per-provider attestations so the modal can gate
  // Promote on an active attestation for the chosen provider + name the one that
  // authorises the lift. Non-MNPI crews don't need it (cleared to []). The bridge
  // is the real gate; a fetch miss leaves the gate CLOSED (fail-closed UX).
  useEffect(() => {
    if (!row || row.sensitivityLock !== "MNPI") { setAttestations([]); return; }
    let live = true;
    void api.mnpiAttestations()
      .then((r) => { if (live) setAttestations(r.attestations); })
      .catch(() => { if (live) setAttestations([]); });
    return () => { live = false; };
  }, [row]);

  // ESC to close (when not mid-submit).
  useEffect(() => {
    if (!row) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !submitting) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [row, submitting, onClose]);

  if (!row) return null;

  const providerIsAnthropic = provider === "anthropic";
  const providerIsOpenai = provider === "openai";
  const isClear = provider === "local";
  const hasAnyPromotion = Object.keys(promoted).length > 0 || row.override != null;

  // MNPI → cloud needs an ACTIVE per-provider attestation. The bridge is the real
  // gate; this mirrors it client-side so Promote is disabled + explained when the
  // chosen provider isn't attested. Keyed by the canonical provider key, which is
  // also the sidecar vocabulary ("anthropic"/"openai") — no mapping needed.
  const isMnpi = row.sensitivityLock === "MNPI";
  const attForProvider = attestations.find((a) => a.provider === provider) ?? null;
  const needsAttestation = isMnpi && !isClear;
  const attestationBlocks = needsAttestation && !attForProvider;

  const onRoleChange = (r: string) => { setRole(r); seedFor(r); setError(null); };

  const run = async (body: PatchCrewProviderBody) => {
    setSubmitting(true);
    setError(null);
    try {
      await api.patchCrewProvider(row.verb, body);
      onSaved();
      onClose();
    } catch (e) {
      setError(formatError(e));
      setSubmitting(false);
    }
  };

  const save = () => {
    const roleField = role === CREW_LEVEL ? {} : { role };
    if (isClear) {
      void run({ ...roleField, clear: true });
      return;
    }
    const body: PatchCrewProviderBody = { ...roleField, preferred_provider: provider };
    if (providerIsAnthropic && model !== "" && MODEL_VALUES.has(model)) body.preferred_model = model;
    void run(body);
  };

  const sens = sensitivityChip(row.sensitivityLock);

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-6 backdrop-blur-sm bg-bg/80"
      onClick={(e) => { if (e.target === e.currentTarget && !submitting) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="crew-promote-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[540px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — neutral/command (crew promotion) */}
        <div className="h-[3px] shrink-0 bg-accent" />
        {/* Head — title + crew verb + sensitivity Chip · close IconButton */}
        <div className="flex items-center justify-between gap-[12px] border-b border-line px-[22px] py-[15px]">
          <div className="flex min-w-0 items-center gap-[10px]">
            <h2 id="crew-promote-title" className="text-[15px] font-semibold tracking-[-0.01em] text-t1">
              Promote crew role to cloud
            </h2>
            <span className="truncate font-mono text-[12px] text-t3">{row.verb}</span>
            <Chip label={sens.label} variant={sens.variant} />
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={submitting} />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-[22px] py-[18px]">
          {/* Current per-role routing */}
          <div className="mb-[18px] rounded-lg border border-line bg-bg-2 px-[14px] py-[11px]">
            <span className="text-[10px] uppercase tracking-[0.12em] text-t3">Current routing</span>
            <div className="mt-[7px] flex flex-col gap-[4px]">
              {row.roles.map((r) => {
                const lane = promoted[r];
                return (
                  <div key={r} className="flex items-baseline justify-between gap-[10px] text-[12px]">
                    <span className="text-t1">{r}</span>
                    {lane ? (
                      <span className="flex items-center gap-[5px] whitespace-nowrap text-[11px] text-accent">
                        <span className="h-[5px] w-[5px] rounded-full bg-accent" /> cloud · {lane}
                      </span>
                    ) : (
                      <span className="flex items-center gap-[5px] whitespace-nowrap text-[11px] text-t3">
                        <span className="h-[5px] w-[5px] rounded-full border border-line-2" /> local · {localModels[r] ?? "qwen3"}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Role */}
          <label className="mb-[7px] block text-[10px] uppercase tracking-[0.14em] text-t3">Role</label>
          <select
            value={role}
            onChange={(e) => onRoleChange(e.target.value)}
            disabled={submitting}
            className="mb-[7px] w-full rounded-lg border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors focus:border-accent-line disabled:opacity-60"
          >
            <option value={CREW_LEVEL}>All roles (crew default)</option>
            {row.roles.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <div className="mb-[16px] text-[11px] leading-[160%] text-t4">
            Promote a single GENERATION role (e.g. the synthesiser) for quality-per-dollar; extraction roles stay cheap-local. Crew default applies to every role without its own setting.
          </div>

          {/* Provider */}
          <label className="mb-[7px] block text-[10px] uppercase tracking-[0.14em] text-t3">Lane</label>
          <select
            value={provider}
            onChange={(e) => { setProvider(e.target.value); setError(null); }}
            disabled={submitting}
            className="mb-[16px] w-full rounded-lg border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors focus:border-accent-line disabled:opacity-60"
          >
            {PROVIDER_OPTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>

          {/* Cloud model pin (Claude lane only) */}
          <div className="mb-[7px] flex items-center justify-between">
            <label className="text-[10px] uppercase tracking-[0.14em] text-t3">Cloud model</label>
            {!providerIsAnthropic && (
              <span className="text-[10px] tracking-[0.02em] text-t4">Claude lane only</span>
            )}
          </div>
          <select
            value={model}
            onChange={(e) => { setModel(e.target.value); setError(null); }}
            disabled={submitting || !providerIsAnthropic}
            title={providerIsAnthropic ? "Pin the Claude model for this role" : "A model pin only applies on the Claude (anthropic) lane"}
            className="mb-[7px] w-full rounded-lg border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors focus:border-accent-line disabled:opacity-50"
          >
            {MODEL_OPTIONS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
          <div className="mb-[16px] text-[11px] leading-[160%] text-t4">
            {isClear
              ? "“Local” removes this role/level's promotion — it reverts to the cheap-local Ollama model."
              : "Promoted-role LLM calls route BACK through the bridge's gated /api/crew/_llm (the subprocess holds no cloud keys); the bridge re-checks sensitivity and force-locals confidential/MNPI."}
          </div>

          {/* Codex-for-crews latency guard (#crew-cloud-promotion follow-up): the
              routing layer permits Codex where attested, but `codex exec` is too
              slow to return within a crew's per-call timeout. Warn, don't disable —
              chat & skills are unaffected. */}
          {providerIsOpenai && !isClear && (
            <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-accent-2-line bg-accent-2-soft px-[11px] py-[8px] text-[11px] leading-[160%] text-accent-2">
              <AlertTriangle size={13} className="mt-[2px] shrink-0" />
              <span>
                Codex (openai) currently times out on a crew's per-call timeout — Claude (anthropic) is
                recommended for crew promotion. Codex works fine for chat &amp; skills.
              </span>
            </div>
          )}

          {/* MNPI → cloud requires an active per-provider P5 attestation. Mirror
              the bridge gate client-side: confirm the authorising attestation when
              present, block + explain when absent. The bridge re-checks regardless. */}
          {needsAttestation && (
            attForProvider ? (
              <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-accent-line bg-accent-soft px-[11px] py-[8px] text-[11px] leading-[160%] text-accent">
                <ShieldCheck size={13} className="mt-[2px] shrink-0" />
                <span>
                  Routes MNPI to cloud under attestation <span className="font-semibold">{attForProvider.id}</span>{" "}
                  ({attForProvider.provider}, expires {new Date(attForProvider.expiresAt).toLocaleDateString()}).
                  The gate re-verifies it on every promoted call.
                </span>
              </div>
            ) : (
              <div className="mb-[14px] flex items-start gap-[8px] rounded-lg border border-red/45 bg-red/10 px-[11px] py-[8px] text-[11px] leading-[160%] text-red">
                <ShieldAlert size={13} className="mt-[2px] shrink-0" />
                <span>
                  No active P5 attestation for <span className="font-semibold">{provider}</span> — MNPI stays
                  local. Grant one in the MNPI cloud-attestations panel (Routing posture) first, or choose a
                  provider that is attested.
                </span>
              </div>
            )
          )}

          {/* Inline error — bridge's reason verbatim on 422 */}
          {error && (
            <div className="mt-[2px] rounded-lg border border-red/45 bg-red/10 px-[11px] py-[8px] text-[11.5px] text-red">
              {error}
            </div>
          )}

          {sidecarPath && (
            <div className="mt-[14px] text-[10.5px] leading-[160%] text-t4">
              Writes the operator sidecar — commit <span className="font-mono text-t3">{sidecarPath}</span> to persist (vault-owned, CLAUDE.md §5.7).
            </div>
          )}
        </div>

        {/* Foot — status note · ghost Cancel + accent primary action */}
        <div className="flex items-center justify-between gap-[12px] border-t border-line px-[22px] py-[13px]">
          <span className="text-[11px] text-t4">
            {hasAnyPromotion ? "This crew has active promotions." : "No promotions yet — all roles local."}
          </span>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="rounded-lg px-[14px] py-[8px] text-[12.5px] text-t3 transition-colors hover:text-t1 disabled:cursor-default disabled:opacity-40"
            >Cancel</button>
            <button
              type="button"
              onClick={save}
              disabled={submitting || attestationBlocks}
              title={attestationBlocks ? "No active P5 attestation for this provider — grant one first" : undefined}
              className="rounded-lg border border-accent-line bg-accent-soft px-[16px] py-[8px] text-[12.5px] font-medium text-t1 transition-[filter] hover:brightness-110 disabled:cursor-default disabled:opacity-40 disabled:hover:brightness-100"
            >
              {submitting ? "Saving…" : isClear ? "Keep local" : "Promote"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function formatError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return e.message || "Refused (422).";
    if (e.status === 404) return "Crew not found — the registry may have changed.";
    return `Failed (${e.status}): ${e.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
