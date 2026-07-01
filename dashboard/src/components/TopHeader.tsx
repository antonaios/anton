import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Search } from "lucide-react";
import { ThemeToggle } from "./ThemeToggle";

/** Format a Date as "YYYY-MM-DD · HH:MM" in UTC — v5 spec. */
function formatUtc(d: Date): string {
  const iso = d.toISOString();
  return `${iso.slice(0, 10)} · ${iso.slice(11, 16)}`;
}

/**
 * v5 top bar (Paper node 5BB-0) — off-white --surface header, 62px tall.
 *
 * LEFT: logo + divider + the {workspaceSwitcher} project-pill slot.
 * CENTER: the omnibox button (opens the command modal via onOpenCommand).
 * RIGHT: Live pill + live UTC clock pill + ThemeToggle (as a matching pill).
 */
export function TopHeader({
  workspaceSwitcher,
  onOpenCommand,
}: {
  workspaceSwitcher?: ReactNode;
  onOpenCommand?: () => void;
}) {
  const [utc, setUtc] = useState<string>(() => formatUtc(new Date()));

  // Live UTC clock — aligns first tick to the next minute boundary.
  useEffect(() => {
    const tick = () => setUtc(formatUtc(new Date()));
    const now = new Date();
    const msToNextMinute = 60_000 - (now.getSeconds() * 1000 + now.getMilliseconds());
    let interval: number | undefined;
    const align = window.setTimeout(() => {
      tick();
      interval = window.setInterval(tick, 60_000);
    }, msToNextMinute);
    return () => {
      window.clearTimeout(align);
      if (interval !== undefined) window.clearInterval(interval);
    };
  }, []);

  return (
    <header
      className="flex h-[62px] w-full shrink-0 items-center justify-between gap-[28px] bg-bg-2 px-[22px] antialiased [font-synthesis:none]"
      style={{ boxShadow: "#23211C0A 0px 1px 2px,#23211C1F 0px 6px 18px -10px" }}
    >
      {/* LEFT — logo · divider · the workspace switcher pill (slot) */}
      <div className="flex items-center gap-[15px]">
        <img
          src="/anton-logo.png"
          alt="ANTON"
          className="h-[22px] w-[48px] select-none object-contain"
          draggable={false}
        />
        <div className="h-[26px] w-px shrink-0 bg-line-2" />
        {workspaceSwitcher}
      </div>

      {/* CENTER — omnibox; opens the command modal */}
      <div className="flex justify-center">
        <button
          type="button"
          onClick={onOpenCommand}
          className="flex h-[40px] w-full max-w-[560px] items-center gap-[11px] rounded-[11px] border border-line-2 bg-paper2 pl-[15px] pr-[9px] text-left"
        >
          <Search size={15} className="shrink-0 text-t3" strokeWidth={2} />
          <span className="grow text-[13px] leading-none text-t3">
            Search deals, recall a fact, or run a tool
          </span>
          <span className="shrink-0 rounded-[6px] border border-line-2 bg-bg-2 px-[7px] py-[4px] font-mono text-[11px] font-medium leading-none text-t2">
            Ctrl K
          </span>
        </button>
      </div>

      {/* RIGHT — Live · UTC clock · theme toggle */}
      <div className="flex items-center gap-[8px]">
        <div className="flex h-[36px] shrink-0 items-center gap-[7px] rounded-[9px] border border-line bg-bg px-[12px]">
          <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-green" />
          <span className="shrink-0 text-[12px] font-medium leading-none text-t2">Live</span>
        </div>
        <div className="flex h-[36px] shrink-0 items-center gap-[7px] rounded-[9px] border border-line bg-bg px-[12px]">
          <span className="shrink-0 text-[12px] leading-none text-t2">UTC</span>
          <span className="shrink-0 font-mono text-[12px] font-medium leading-none text-t1">{utc}</span>
        </div>
        <div className="flex h-[36px] w-[36px] shrink-0 items-center justify-center rounded-[9px] border border-line bg-bg">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
