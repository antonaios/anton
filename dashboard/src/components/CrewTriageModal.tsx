import { useEffect, useState } from "react";
import { X, Lock } from "lucide-react";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";

interface Props {
  open: boolean;
  onClose: () => void;
  onRun: (args: { pdf_path: string; entity?: string }) => void;
}

/** The triage crew's always-local 6-role extraction pipeline. */
const ROLES = [
  "Ingestor",
  "RedFlags",
  "Opportunities",
  "KeyMetrics",
  "QuestionsForMgmt",
  "Summariser",
] as const;

/**
 * #front-door · /triage crew launcher.
 *
 * The triage crew (MNPI-locked, always-local 6-role extraction) reads a
 * CIM/teaser PDF. It needs a SERVER-READABLE absolute path: a browser file
 * picker only yields a File object, not a path the bridge can open, and the
 * bridge runs locally — so the operator pastes/types the absolute path here.
 * (A true upload would need a new bridge route — out of scope for v1.)
 *
 * This modal only COLLECTS the args; `onRun` hands them to App.fireCrew, which
 * opens the crew bubble + consumes the SSE channel.
 */
export function CrewTriageModal({ open, onClose, onRun }: Props) {
  const [path, setPath] = useState("");
  const [entity, setEntity] = useState("");

  useEffect(() => { if (open) { setPath(""); setEntity(""); } }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const canRun = path.trim().length > 0;
  const submit = () => {
    if (!canRun) return;
    const e = entity.trim();
    onRun({ pdf_path: path.trim(), ...(e ? { entity: e } : {}) });
  };

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-bg/70 p-6 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="triage-title"
    >
      <div className="flex max-h-[90vh] w-full max-w-[520px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — intake/gated (triage) */}
        <div className="h-[3px] shrink-0 bg-amber" />
        {/* Header — title + crew chip + subtitle (the MNPI/always-local
            fail-loud guard is reinforced by the red lock band below) */}
        <div className="flex items-start justify-between gap-[12px] border-b border-line px-[20px] py-[14px]">
          <div className="min-w-0">
            <div className="flex items-center gap-[10px]">
              <h2 id="triage-title" className="text-[15px] font-semibold tracking-[-0.01em] text-t1">
                Triage crew
              </h2>
              <Chip label="crew · MNPI-safe" variant="accent" className="mono" />
            </div>
            <p className="mt-[5px] text-[12px] leading-[1.5] text-t3">
              Point the triage crew at a CIM PDF — 6 roles, always local.
            </p>
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-[20px] py-[18px]">
          <label className="mono mb-[6px] block text-[10px] uppercase tracking-[0.1em] text-t3">
            CIM / teaser PDF path
          </label>
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            autoFocus
            placeholder="<workspace-root>\…\CIM.pdf"
            className="mb-[6px] w-full rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[8px] text-[13px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line"
          />
          <div className="mb-[18px] text-[11px] leading-relaxed text-t4">
            Absolute path on this machine — the bridge opens it directly (no upload).
          </div>

          <label className="mono mb-[6px] block text-[10px] uppercase tracking-[0.1em] text-t3">
            Entity{" "}
            <span className="text-t4 normal-case tracking-normal">(optional — deal / target name for the record)</span>
          </label>
          <input
            value={entity}
            onChange={(e) => setEntity(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            placeholder="Project Falcon"
            className="w-full rounded-lg border border-line-2 bg-bg-2 px-[12px] py-[8px] text-[13px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line"
          />

          {/* Roles pipeline — the 6-role extraction chain */}
          <div className="mt-[18px]">
            <div className="mono mb-[8px] text-[10px] uppercase tracking-[0.1em] text-t3">
              Roles · local pipeline
            </div>
            <div className="flex flex-wrap items-center gap-x-[6px] gap-y-[6px]">
              {ROLES.map((role, i) => (
                <span key={role} className="flex items-center gap-x-[6px]">
                  <span className="mono rounded-[6px] border border-line-2 bg-bg-2 px-[8px] py-[3px] text-[10px] text-t2">
                    {role}
                  </span>
                  {i < ROLES.length - 1 && (
                    <span className="mono text-[11px] text-t4">→</span>
                  )}
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* MNPI lock band — fail-loud reinforcement of the always-local guard */}
        <div className="border-t border-line px-[20px] pt-[14px]">
          <div className="flex items-start gap-[9px] rounded-[10px] border border-red/40 bg-red/12 px-[13px] py-[11px]">
            <Lock size={16} className="mt-[1px] shrink-0 text-red" />
            <span className="text-[11px] leading-relaxed text-t2">
              Always local · MNPI — the CIM stays on the machine; cloud promotion is blocked.
            </span>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-[12px] px-[20px] py-[13px]">
          <span className="text-[11px] text-t4">6 roles · local Ollama · ~100k tokens</span>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-line-2 px-[14px] py-[7px] text-[12px] text-t2 transition-colors hover:border-accent-line hover:text-t1"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!canRun}
              onClick={submit}
              className="rounded-lg bg-accent px-[16px] py-[7px] text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-default disabled:opacity-40 disabled:hover:opacity-40"
            >
              Run triage →
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
