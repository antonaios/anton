import { TABS } from "../data/seed";
import { cn } from "../lib/cn";

// "routing" is a left-nav SETTINGS leaf with its OWN tab body (RoutingTab) — it
// is not a top-tab button (so it is NOT in the TABS seed array), hence unioned in
// here rather than derived from TABS. App.navToTab maps the routing NavKey to it.
export type TabKey = (typeof TABS)[number]["key"] | "routing";

interface Props {
  active: TabKey;
  onSelect: (key: TabKey) => void;
}

export function MainTabs({ active, onSelect }: Props) {
  return (
    <div className="border-b border-line px-[18px]">
      <div className="flex gap-0 pt-2">
        {TABS.map((t) => {
          const isActive = t.key === active;
          const count = "count" in t ? (t as { count: number }).count : undefined;
          return (
            <button
              type="button"
              key={t.key}
              onClick={() => onSelect(t.key)}
              className={cn(
                "relative px-[14px] py-[5px] text-[11px] uppercase tracking-[0.12em] transition-colors",
                isActive ? "text-text-primary font-medium" : "text-text-secondary hover:text-text-primary",
              )}
            >
              {t.label}
              {count !== undefined && (
                <span className="mono ml-[5px] text-[10px] text-text-tertiary">{count}</span>
              )}
              {isActive && (
                <span className="absolute inset-x-3 -bottom-px h-0.5 bg-info" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
