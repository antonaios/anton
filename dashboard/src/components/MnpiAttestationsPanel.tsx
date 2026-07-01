import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import type { AttestationDTO } from "../types";

/**
 * #crew-cloud-promotion Phase C / #llm-routing-postjune15 P5 — the operator
 * surface for per-provider MNPI cloud-attestations.
 *
 * An attestation records that a cloud provider carries DPA + ZDR + no-training;
 * under AGENTIC_PLAN_TIER=enterprise an active attestation lets EXPLICITLY-assigned
 * MNPI route to that provider's cloud lane. Granting one is the single most
 * sensitive operator action on the platform — it relaxes the #no-mnpi-to-cloud
 * floor for one provider — so the grant is deliberately friction-ful here (all
 * three protections must be ticked) and nonce-confirmed server-side
 * (challenge → grant). The empty store reproduces the absolute pre-P5 floor.
 */

const POLL_MS = 60_000;
const DAY_MS = 86_400_000;
const PROVIDERS = [
  { value: "anthropic", label: "Claude (anthropic)" },
  { value: "openai", label: "ChatGPT (openai)" },
];

function daysLeft(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / DAY_MS);
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

export function MnpiAttestationsPanel({ planTier }: { planTier?: string }) {
  const [atts, setAtts] = useState<AttestationDTO[] | null>(null);
  const [absent, setAbsent] = useState(false);   // endpoint 404 on a pre-P5 bridge
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Grant form (collapsed by default — a deliberate click to reach the dangerous action).
  const [showForm, setShowForm] = useState(false);
  const [provider, setProvider] = useState("anthropic");
  const [dpa, setDpa] = useState(false);
  const [zdr, setZdr] = useState(false);
  const [noTraining, setNoTraining] = useState(false);
  const [grantedBy, setGrantedBy] = useState("");
  const [durationDays, setDurationDays] = useState(365);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const enterprise = planTier === "enterprise";

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const r = await api.mnpiAttestations(ac.signal);
      if (ac.signal.aborted) return;
      setAtts(r.attestations);
      setAbsent(false);
      setError(null);
    } catch (e) {
      if (ac.signal.aborted) return;
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
        setAbsent(true);          // bridge predates P5 — render nothing
      } else {
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      }
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      abortRef.current?.abort();
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);

  const resetForm = () => {
    setProvider("anthropic"); setDpa(false); setZdr(false);
    setNoTraining(false); setGrantedBy(""); setDurationDays(365); setFormError(null);
  };

  const canGrant = dpa && zdr && noTraining && grantedBy.trim().length > 0 && !submitting;

  const grant = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      const ch = await api.mnpiChallenge();   // single-use nonce (F-8)
      await api.grantMnpiAttestation({
        provider,
        dpa, zdr, no_training: noTraining,
        granted_by: grantedBy.trim(),
        duration_seconds: Math.round(durationDays * 86_400),
        confirmation_nonce: ch.confirmationNonce,
      });
      setShowForm(false);
      resetForm();
      await load();
    } catch (e) {
      setFormError(
        e instanceof ApiError ? (e.message || `Refused (${e.status}).`) : "Failed — see console.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const revoke = async (a: AttestationDTO) => {
    if (!window.confirm(
      `Revoke the ${a.provider} attestation? MNPI for ${a.provider} returns to the ` +
      `local-only floor immediately.`,
    )) return;
    try {
      await api.revokeMnpiAttestation(a.id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  };

  if (absent) return null;   // pre-P5 bridge — graceful absence (like the Crews section)

  return (
    <div className="flex flex-col overflow-clip rounded-[14px] border border-line bg-bg-1 shadow-card">
      {/* Header strip — title + P5 outline badge, with the enterprise/tier note right-aligned */}
      <div className="flex flex-wrap items-center justify-between gap-[10px] border-b border-line px-[18px] py-[13px]">
        <div className="flex items-center gap-[9px]">
          <span className="text-[11px] font-semibold uppercase tracking-[0.1em] leading-[14px] text-t3">
            MNPI ATTESTATIONS
          </span>
          <span
            className="inline-flex items-center h-[18px] rounded-[5px] px-[7px] border border-red"
            title="An active attestation relaxes the #no-mnpi-to-cloud floor for one provider — only under the enterprise plan tier, and only for EXPLICIT operator-assigned MNPI."
          >
            <span className="text-[9px] font-bold tracking-[0.06em] leading-3 text-red">P5 · SENSITIVE</span>
          </span>
        </div>
        <span className="text-[11px] leading-[14px] text-amber" title="An attestation takes effect only under the enterprise plan tier.">
          enterprise required · current: {enterprise ? "Enterprise" : (planTier ? planTier[0].toUpperCase() + planTier.slice(1) : "Bridge")}
        </span>
      </div>

      <div className="flex flex-col gap-[8px] px-[18px] py-[13px]">
        {/* Body description — what the grant actually does */}
        <p className="text-[12.5px] leading-[155%] text-t2">
          The single most sensitive operator action — it relaxes the no-MNPI-to-cloud floor for one
          provider. Both an active attestation and the enterprise tier must hold before any MNPI run can
          reach cloud.
        </p>

        {/* Active attestations */}
        {atts == null ? (
          <span className="text-[10.5px] italic text-t3">Loading attestations…</span>
        ) : atts.length === 0 ? (
          <div className="flex items-center rounded-[10px] p-4 gap-[11px] bg-paper2">
            <svg width="16" height="16" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
              <rect x="3" y="7" width="10" height="6.5" rx="1.5" fill="none" stroke="var(--mist)" strokeWidth="1.4" />
              <path d="M5.2 7V5.4a2.8 2.8 0 0 1 5.6 0V7" fill="none" stroke="var(--mist)" strokeWidth="1.4" />
            </svg>
            <span className="text-[12.5px] leading-[150%] text-t3">
              MNPI is local-only for every provider — the pre-P5 floor. No attestations active.
            </span>
          </div>
        ) : (
          <div className="flex flex-col gap-[5px]">
            {atts.map((a) => {
              const left = daysLeft(a.expiresAt);
              const expiringSoon = left <= 30;
              return (
                <div key={a.id} className="flex flex-wrap items-center gap-[8px] text-[11px]">
                  <span className="rounded-sm border border-accent-line bg-accent-soft text-accent px-[7px] py-[2px] text-[10px] whitespace-nowrap">
                    ● {a.provider}
                  </span>
                  <span className="text-t3 text-[10px]" title="DPA + ZDR + no-training all asserted">
                    DPA · ZDR · no-training
                  </span>
                  <span
                    className={cn("text-[10px] whitespace-nowrap", expiringSoon ? "text-accent-2" : "text-t4")}
                    title={`Expires ${fmtDate(a.expiresAt)}`}
                  >
                    expires {fmtDate(a.expiresAt)} ({left}d)
                  </span>
                  <span className="text-t4 text-[9.5px]">by {a.grantedBy}</span>
                  <button
                    type="button"
                    onClick={() => void revoke(a)}
                    className="ml-auto rounded-md px-[8px] py-[2px] border border-line text-[9.5px] tracking-[0.06em] uppercase text-t3 hover:text-red hover:border-red/50 transition-colors"
                    title="Revoke now — MNPI for this provider returns to local-only immediately"
                  >
                    Revoke
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {error && (
          <div className="rounded-md border border-red/50 bg-red/10 px-[10px] py-[6px] text-[11px] text-red">
            {error}
          </div>
        )}
      </div>

      {/* Grant footer — requirements + the (collapsed) grant trigger; expands to the form */}
      <div className="border-t border-line px-[18px] py-[14px]">
        {!showForm ? (
          <div className="flex items-center justify-between gap-[12px]">
            <span className="max-w-[520px] text-[11px] leading-[150%] text-t4">
              Grant requires DPA · ZDR · no-training + a confirmation challenge, per provider, with a
              mandatory expiry.
            </span>
            <button
              type="button"
              onClick={() => { resetForm(); setShowForm(true); }}
              className="inline-flex items-center h-[34px] shrink-0 rounded-[9px] px-[14px] border border-line-2 text-[12px] leading-4 font-medium text-t2 hover:text-accent hover:border-accent-line transition-colors"
            >
              + Grant attestation
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-[10px]">
              <div className="rounded-md border border-red/40 bg-red/10 px-[10px] py-[7px] text-[10.5px] text-t2 leading-relaxed">
                Granting an attestation lets <span className="text-red">EXPLICIT MNPI</span> route to this
                provider's cloud lane under the enterprise tier. Confirm the contractual protections are
                actually in force — all three are required.
              </div>

              <div className="flex flex-wrap items-center gap-[10px]">
                <label className="text-[10px] tracking-[0.1em] uppercase text-t3">Provider</label>
                <select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  disabled={submitting}
                  className="rounded-[5px] bg-bg border border-line px-[9px] py-[5px] text-[12px] text-t1 outline-none focus:border-accent-line disabled:opacity-60"
                >
                  {PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
                <label className="text-[10px] tracking-[0.1em] uppercase text-t3 ml-[6px]">Valid for</label>
                <input
                  type="number" min={1} max={400} value={durationDays}
                  onChange={(e) => setDurationDays(Math.max(1, Math.min(400, Number(e.target.value) || 1)))}
                  disabled={submitting}
                  className="w-[64px] rounded-[5px] bg-bg border border-line px-[8px] py-[5px] text-[12px] text-t1 outline-none focus:border-accent-line disabled:opacity-60"
                />
                <span className="text-[10px] text-t4">days (≤ ~13 months)</span>
              </div>

              <div className="flex flex-col gap-[5px] text-[11.5px] text-t2">
                {([
                  ["dpa", dpa, setDpa, "Signed Data Processing Agreement is in force"],
                  ["zdr", zdr, setZdr, "Zero-data-retention is contractually guaranteed"],
                  ["no_training", noTraining, setNoTraining, "Payload is contractually excluded from training"],
                ] as const).map(([key, val, set, label]) => (
                  <label key={key} className="flex items-center gap-[8px] cursor-pointer">
                    <input
                      type="checkbox" checked={val} disabled={submitting}
                      onChange={(e) => set(e.target.checked)}
                      className="accent-[var(--accent,#e8a04c)]"
                    />
                    <span className="text-t3 text-[10px] uppercase tracking-[0.06em] w-[92px]">{key}</span>
                    <span>{label}</span>
                  </label>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-[8px]">
                <label className="text-[10px] tracking-[0.1em] uppercase text-t3">Granted by</label>
                <input
                  type="text" value={grantedBy} placeholder="operator identity (audit)"
                  onChange={(e) => setGrantedBy(e.target.value)}
                  disabled={submitting}
                  className="flex-1 min-w-[180px] rounded-[5px] bg-bg border border-line px-[9px] py-[5px] text-[12px] text-t1 outline-none focus:border-accent-line disabled:opacity-60"
                />
              </div>

              {formError && (
                <div className="rounded-md border border-red/50 bg-red/10 px-[10px] py-[6px] text-[11px] text-red">
                  {formError}
                </div>
              )}

              <div className="flex items-center gap-[6px]">
                <button
                  type="button"
                  onClick={() => { setShowForm(false); resetForm(); }}
                  disabled={submitting}
                  className="rounded-md px-[12px] py-[5px] border border-line text-[11px] tracking-[0.06em] text-t2 hover:text-t1 hover:border-line-2 disabled:opacity-40"
                >
                  CANCEL
                </button>
                <button
                  type="button"
                  onClick={() => void grant()}
                  disabled={!canGrant}
                  title={canGrant ? "Grant the attestation" : "Tick all three protections + name the operator first"}
                  className="rounded-md px-[14px] py-[6px] text-[11px] tracking-[0.1em] uppercase bg-red text-bg hover:brightness-110 disabled:opacity-40 disabled:cursor-default disabled:hover:brightness-100"
                >
                  {submitting ? "GRANTING…" : "GRANT ATTESTATION →"}
                </button>
              </div>
            </div>
          )}
      </div>
    </div>
  );
}
