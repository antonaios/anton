import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { CrewPromotionModal } from "./CrewPromotionModal";
import { MnpiAttestationsPanel } from "./MnpiAttestationsPanel";
import type { CrewProviderRow } from "../types";

/**
 * #llm-routing-postjune15 Mission B — the ACTIONABLE routing surface kept on the
 * Providers tab. The read-only routing-posture readouts (cloud fallback ladders,
 * task-class tiering grid, provider ceilings) now live only on the dedicated
 * Routing tab (RoutingTab); this panel keeps the two operator-actionable
 * sections Paper places on Providers:
 *
 *   · Crews — per-role cloud-promotion (#crew-cloud-promotion)
 *   · MNPI cloud-attestations — the P5 grant gate (MnpiAttestationsPanel)
 *
 * Self-fetches the crew matrix (focus + 60s); planTier feeds the MNPI gate.
 */

const POLL_MS = 60_000;

// ── Crews — per-role cloud-promotable (#crew-cloud-promotion) ────────────────
// Crews run in a sandboxed local subprocess with NO cloud keys. An operator can
// PROMOTE a public/internal crew (or just its generation roles) to a frontier
// cloud model: promoted roles route their LLM calls BACK through the gated
// loopback /api/crew/_llm (the subprocess stays credential-free; the bridge
// re-derives sensitivity and force-locals confidential/MNPI). MNPI/confidential-
// locked crews are NOT promotable here — no cloud option is shown.

// Lock icon for the "locked" right-action on a non-promotable crew row (matches
// the Paper 14×14 padlock glyph — node 9Z2-0 triage row).
function LockGlyph() {
  return (
    <svg width="11" height="11" viewBox="0 0 14 14" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
      <rect x="2.5" y="6" width="9" height="6" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M4.5 6V4.5a2.5 2.5 0 0 1 5 0V6" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

function CrewRow({ c, last, onPromote }: { c: CrewProviderRow; last: boolean; onPromote: (c: CrewProviderRow) => void }) {
  const promoted = Object.entries(c.promotedRoles ?? {});
  const localCount = Math.max(0, c.roles.length - promoted.length);
  const lock = c.sensitivityLock;

  return (
    <div className={`flex items-center gap-[14px] px-[18px] py-[13px] ${last ? "" : "border-b border-line"}`}>
      {/* Slot 1 — verb (fixed width) */}
      <div className="w-[120px] shrink-0">
        <span className="mono text-[11.5px] leading-4 text-t1" title={`${c.description}\n${c.roles.length} roles`}>
          {c.verb}
        </span>
      </div>

      {/* Slot 2 — lane pill(s) + "+N local" (grows) */}
      <div className="grow min-w-0 flex flex-wrap items-center gap-[7px]">
        {promoted.length > 0 ? (
          <>
            {promoted.map(([role, lane]) => (
              <span
                key={role}
                className="inline-flex items-center h-[22px] rounded-md px-[9px] gap-[6px] bg-accent-soft whitespace-nowrap"
                title={`${role} promoted to the ${lane} cloud lane — routed through the gated /api/crew/_llm (subprocess holds no keys).`}
              >
                <span className="w-[5px] h-[5px] rounded-[3px] shrink-0 bg-accent" />
                <span className="text-[11px] leading-[14px] text-accent">{role} → {lane}</span>
              </span>
            ))}
            {localCount > 0 && (
              <span className="text-[11px] leading-[14px] text-t3">+ {localCount} local</span>
            )}
          </>
        ) : c.promotable ? (
          <span className="text-[11.5px] leading-[14px] text-t3" title="All roles run locally on Ollama. Promotable to a frontier cloud lane.">
            local Ollama · all roles
          </span>
        ) : (
          <span
            className="text-[11.5px] leading-[14px] text-t3"
            title="Runs locally on Ollama, fail-closed — the crew subprocess holds no cloud keys."
          >
            local Ollama · {lock === "MNPI" ? "MNPI-locked" : lock === "confidential" ? "confidential-locked" : "locked"}
          </span>
        )}
      </div>

      {/* Slot 3 — sensitivity (dot+word, or a bordered tier chip for locked) */}
      <div className="shrink-0">
        {lock === "MNPI" ? (
          <span
            className="inline-flex items-center h-5 rounded-[5px] px-2 border border-red whitespace-nowrap"
            title="Manifest sensitivity_override = MNPI (CIM inputs are MNPI). Cloud-promotable ONLY under the enterprise tier + an active per-provider P5 attestation."
          >
            <span className="text-[10px] font-semibold tracking-[0.04em] leading-[13px] text-red">MNPI · ATTESTATION</span>
          </span>
        ) : lock === "confidential" ? (
          <span
            className="inline-flex items-center h-5 rounded-[5px] px-2 border border-amber whitespace-nowrap"
            title="Confidential-locked: cloud-promotable only on the Claude lane under the enterprise tier."
          >
            <span className="text-[10px] font-semibold tracking-[0.04em] leading-[13px] text-amber">CONFID · ENTERPRISE</span>
          </span>
        ) : (
          <span className="inline-flex items-center gap-[5px]" title="Public / internal crew — promotable to a frontier cloud lane.">
            <span className="size-[6px] rounded-[3px] shrink-0 bg-green" />
            <span className="text-[11px] leading-[14px] text-t2">promotable</span>
          </span>
        )}
      </div>

      {/* Slot 4 — right action (plain text; locked shows a padlock + "locked") */}
      <div className="w-[80px] shrink-0 flex justify-end items-center gap-[5px]">
        {c.promotable ? (
          <button
            type="button"
            onClick={() => onPromote(c)}
            className="text-[11.5px] leading-[14px] text-accent hover:underline whitespace-nowrap"
            title="Promote a role (or the whole crew) to a frontier cloud model"
          >
            {promoted.length > 0 ? "Manage" : "Promote"}
          </button>
        ) : (
          <span className="inline-flex items-center gap-[5px] text-t4" title="Locked local — promotion needs the enterprise tier (and a P5 attestation for MNPI).">
            <LockGlyph />
            <span className="text-[11.5px] leading-[14px]">locked</span>
          </span>
        )}
      </div>
    </div>
  );
}

function CrewsSection({ crews, sidecarPath, onSaved }: { crews: CrewProviderRow[]; sidecarPath?: string; onSaved: () => void }) {
  const [openCrew, setOpenCrew] = useState<CrewProviderRow | null>(null);
  if (!crews.length) return null;
  return (
    <div className="flex flex-col overflow-clip rounded-[14px] border border-line bg-bg-1 shadow-card">
      {/* Header strip — title + inline caption, with the operator sidecar path right-aligned */}
      <div className="flex items-center justify-between gap-[10px] border-b border-line px-[18px] py-[13px]">
        <div className="flex items-center gap-[9px]">
          <span
            className="text-[11px] font-semibold uppercase tracking-[0.1em] leading-[14px] text-t3"
            title="Crews run on the local Ollama lane by default — the subprocess has no cloud credentials. Promotion routes a role's calls back through the gated /api/crew/_llm."
          >
            CREWS · CLOUD PROMOTION
          </span>
          <span className="mono text-[10.5px] leading-[14px] text-t4" title="The extraction roles always stay on the local lane; only generation roles can be promoted.">
            extraction stays local
          </span>
        </div>
        {sidecarPath && (
          <span className="mono text-[10px] leading-[14px] text-t4" title={sidecarPath}>
            crew_overrides.yaml
          </span>
        )}
      </div>
      <div className="flex flex-col">
        {crews.map((c, i) => (
          <CrewRow key={c.verb} c={c} last={i === crews.length - 1} onPromote={setOpenCrew} />
        ))}
      </div>
      <CrewPromotionModal
        row={openCrew}
        sidecarPath={sidecarPath}
        onClose={() => setOpenCrew(null)}
        onSaved={onSaved}
      />
    </div>
  );
}

// ── Panel ───────────────────────────────────────────────────────────────────

export function RoutingPosturePanel() {
  const [crews, setCrews] = useState<CrewProviderRow[] | null>(null);
  const [crewSidecar, setCrewSidecar] = useState<string | undefined>(undefined);
  const [planTier, setPlanTier] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const load = async () => {
      abortRef.current?.abort();             // dedupe overlapping polls + unmount
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const r = await api.crewProviders(ac.signal);   // crews + plan tier
        if (ac.signal.aborted) return;
        setCrews(r.crews);
        setCrewSidecar(r.sidecarPath);
        setPlanTier(r.planTier);
        setError(null);
      } catch (e) {
        if (ac.signal.aborted) return;
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      } finally {
        if (!ac.signal.aborted) setLoading(false);
      }
    };
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    const onFocus = () => void load();        // refresh on focus, like the parent tab
    window.addEventListener("focus", onFocus);
    return () => {
      abortRef.current?.abort();
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  // Refetch just the crews after a promotion PATCH (the modal's onSaved).
  const reloadCrews = async () => {
    try {
      const r = await api.crewProviders();
      setCrews(r.crews);
      setCrewSidecar(r.sidecarPath);
      setPlanTier(r.planTier);
    } catch { /* keep the prior snapshot */ }
  };

  return (
    <div className="flex flex-col gap-[20px]">
      {loading && !crews ? (
        <div className="text-[11px] italic text-t3">Loading crews…</div>
      ) : error && !crews ? (
        <div className="text-[11px] italic text-t3">Crews unavailable — {error}</div>
      ) : (
        <>
          {/* Crews — structurally local-only subprocess lane */}
          {crews && <CrewsSection crews={crews} sidecarPath={crewSidecar} onSaved={() => void reloadCrews()} />}

          {/* MNPI cloud-attestations — the P5 gate that makes MNPI crews/chat
              liftable. Self-fetches; renders nothing on a pre-P5 bridge (404). */}
          <MnpiAttestationsPanel planTier={planTier} />

          {error && crews && (
            <div className="text-[10px] text-t4">last refresh failed ({error}) — showing the previous snapshot.</div>
          )}
        </>
      )}
    </div>
  );
}
