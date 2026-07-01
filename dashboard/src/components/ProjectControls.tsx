import { ChevronDown, MessageSquare, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";

/**
 * Project selector strip. Sits in the row directly beneath the SparkTicker,
 * next to MainTabs. Restyled to match the v4 aesthetic: square corners,
 * `border-line-strong` for the dropdown (still the "brighter border" per the
 * original brief), brand-red primary, panel-secondary chrome.
 *
 * On mount, hydrates from `/api/workspaces?type=project`; falls back to
 * "Project Falcon" when the bridge is offline.
 */
export function ProjectControls() {
  const [projects, setProjects] = useState<string[]>(["Project Falcon"]);
  const [selected, setSelected] = useState("Project Falcon");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .listWorkspaces("project")
      .then((res) => {
        if (cancelled) return;
        const names = res.workspaces.map((w) => w.name);
        if (names.length > 0) {
          const falcon = names.find((p) => p.toLowerCase().includes("falcon"));
          setProjects(names);
          setSelected(falcon ?? names[0]);
        }
      })
      .catch(() => {
        // Bridge offline — silent fallback.
      });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="flex h-full items-center gap-2.5 px-[18px] py-[6px]">
      <span className="mono text-[10px] uppercase tracking-[0.16em] text-text-secondary">
        Project
      </span>

      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className={cn(
            "flex h-7 min-w-[200px] items-center justify-between gap-3 px-2.5",
            "border border-line-strong bg-bg-2 text-[12px] font-medium text-text-primary",
            "transition-colors hover:border-text-secondary",
          )}
        >
          <span>{selected}</span>
          <ChevronDown className="h-3.5 w-3.5 text-text-tertiary" />
        </button>

        {open && projects.length > 1 && (
          <div className="absolute top-full z-10 mt-px min-w-[200px] border border-line-strong bg-panel shadow-xl">
            {projects.map((p) => (
              <button
                type="button"
                key={p}
                onClick={() => {
                  setSelected(p);
                  setOpen(false);
                }}
                className="block w-full px-2.5 py-1.5 text-left text-[11.5px] text-text-secondary transition-colors hover:bg-panel-hover hover:text-text-primary"
              >
                {p}
              </button>
            ))}
          </div>
        )}
      </div>

      <button
        type="button"
        className={cn(
          "inline-flex h-7 items-center gap-1.5 px-2.5",
          "bg-brand-red text-[11px] font-medium uppercase tracking-[0.06em] text-white",
          "transition-colors hover:bg-[#e83730]",
        )}
      >
        <Plus className="h-3.5 w-3.5" />
        New project
      </button>

      <button
        type="button"
        className={cn(
          "inline-flex h-7 items-center gap-1.5 px-2.5",
          "border border-line bg-bg-2 text-[11px] uppercase tracking-[0.06em] text-text-secondary",
          "transition-colors hover:border-line-strong hover:text-text-primary",
        )}
      >
        <MessageSquare className="h-3.5 w-3.5" />
        General chat
      </button>
    </div>
  );
}
