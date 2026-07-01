import { cn } from "../lib/cn";
import { INTEL_FEED } from "../data/seed";
import type { IntelTone } from "../types";

/**
 * Intelligence feed — bottom of the workspace. Three source-attributed cards
 * showing routine outputs, vault changes, and news. Matches the panel box
 * pattern (Command · Anton style) with tabbed filter strip in the header.
 */
export function IntelligenceFeed() {
  return (
    <section className="panel">
      <div className="flex items-center justify-between border-b border-line px-[14px] py-[7px]">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.18em] text-label">
          Intelligence feed
        </h3>
        <div className="flex">
          <FeedTab label="All" active />
          <FeedTab label="Routines" />
          <FeedTab label="Vault" />
          <FeedTab label="News" />
          <FeedTab label="Markets" />
        </div>
      </div>

      <div className="grid grid-cols-3 gap-[10px] px-[14px] py-[12px]">
        {INTEL_FEED.map((item) => (
          <article
            key={item.id}
            className="flex flex-col gap-1 border border-line bg-bg-2 px-[12px] py-[10px]"
          >
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-text-tertiary">
              <span className={cn("h-1 w-1 rounded-full", toneDot(item.sourceTone))} />
              <span className="font-semibold text-label">{item.source}</span>
              <span>· {item.ago}</span>
            </div>
            <div className="text-[12px] font-medium leading-[1.35] text-text-primary">
              {item.title}
            </div>
            <div className="text-[10.5px] leading-[1.45] text-text-secondary">
              {item.description}
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="mono border border-line-strong px-[6px] py-[1px] text-[9.5px] tracking-[0.1em] text-text-secondary">
                {item.pill}
              </span>
              <span className="mono text-[9.5px] tracking-[0.08em] text-text-tertiary hover:text-info cursor-pointer">
                {item.link}
              </span>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function FeedTab({ label, active = false }: { label: string; active?: boolean }) {
  return (
    <span
      className={cn(
        "cursor-pointer border-r border-line px-[10px] py-[3px] text-[10px] uppercase tracking-[0.14em] last:border-r-0",
        active ? "bg-panel-hover text-text-primary" : "text-text-secondary hover:text-text-primary",
      )}
    >
      {label}
    </span>
  );
}

function toneDot(tone: IntelTone): string {
  switch (tone) {
    case "ok":   return "bg-green";
    case "warn": return "bg-amber";
    case "info": return "bg-info";
  }
}
