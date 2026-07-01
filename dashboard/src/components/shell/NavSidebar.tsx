import type { LucideIcon } from "lucide-react";
import {
  LayoutGrid, Inbox, History, FileText, FileOutput, Newspaper,
  Activity, Server, Route, Wallet, FolderTree, SlidersHorizontal,
} from "lucide-react";
import { cn } from "../../lib/cn";
import type { LLMBurnSummary } from "../../types";

/**
 * Every selectable leaf destination in the left nav — one entry per row the
 * operator can land on. The four KNOWLEDGE rows (Recall/Notes/Outputs/News),
 * Activity, and the five Settings rows are all flat leaves.
 */
export type NavKey =
  | "desk"
  | "inbox"
  | "recall"      // KNOWLEDGE
  | "notes"       // KNOWLEDGE
  | "outputs"     // KNOWLEDGE
  | "news"        // KNOWLEDGE
  | "activity"    // ACTIVITY
  | "providers"   // SETTINGS
  | "routing"     // SETTINGS
  | "budget"      // SETTINGS
  | "taxonomy"    // SETTINGS
  | "operator";   // SETTINGS

interface NavLeaf {
  kind: "leaf";
  key: NavKey;
  label: string;
  icon: LucideIcon;
}
interface NavSection {
  /** Optional mono-uppercase section header above the items. */
  eyebrow?: string;
  items: NavLeaf[];
}

/**
 * The nav tree, driven from data so the render stays declarative. The KNOWLEDGE
 * rows are flat leaves (no collapsible "Library" group — the group header was a
 * non-navigating toggle, so it was removed and its children pinned permanently
 * under the KNOWLEDGE eyebrow). Order + icons + eyebrows match the Desk reference.
 */
export const NAV: NavSection[] = [
  { items: [
    { kind: "leaf", key: "desk",  label: "Desk",  icon: LayoutGrid },
    { kind: "leaf", key: "inbox", label: "Inbox", icon: Inbox },
  ] },
  { eyebrow: "KNOWLEDGE", items: [
    { kind: "leaf", key: "recall",  label: "Recall",  icon: History },
    { kind: "leaf", key: "notes",   label: "Notes",   icon: FileText },
    { kind: "leaf", key: "outputs", label: "Outputs", icon: FileOutput },
    { kind: "leaf", key: "news",    label: "News",    icon: Newspaper },
  ] },
  { eyebrow: "ACTIVITY", items: [
    { kind: "leaf", key: "activity", label: "Activity", icon: Activity },
  ] },
  { eyebrow: "SETTINGS", items: [
    { kind: "leaf", key: "providers", label: "Providers", icon: Server },
    { kind: "leaf", key: "routing",   label: "Routing",   icon: Route },
    { kind: "leaf", key: "budget",    label: "Budget",    icon: Wallet },
    { kind: "leaf", key: "taxonomy",  label: "Taxonomy",  icon: FolderTree },
    { kind: "leaf", key: "operator",  label: "Operator",  icon: SlidersHorizontal },
  ] },
];

export interface NavSidebarProps {
  /** Currently-active leaf destination (App's single source of truth). */
  active: NavKey;
  /** Fired when a leaf row is clicked. App translates NavKey→TabKey and applies
   *  the OPERATOR dirty-state guard before committing the switch. */
  onSelect: (key: NavKey) => void;
  /** Pending-proposal total → coral count badge on the Inbox row. Undefined or
   *  0 ⇒ no badge. */
  reviewCount?: number;
  /** Today's LLM burn (`/api/telemetry/llm-burn`). When present, an always-on
   *  cost footer fills the gap above SETTINGS so spend is visible on every
   *  screen, not just the Desk rail. Null/undefined while in-flight ⇒ hidden. */
  burn?: LLMBurnSummary | null;
}

/**
 * NavSidebar — the left vertical nav rail (Paper node 56K-0).
 *
 * A SELF-CONTAINED `bg-rail` teal column (172px wide, 16px radius) carrying its
 * own padding, shadow and ground. Renders the {@link NAV} tree as flat leaf rows
 * grouped under mono-uppercase section eyebrows; a flex spacer between ACTIVITY
 * and SETTINGS bottom-aligns the settings group. Purely presentational —
 * selection + routing are owned by the parent.
 */
export function NavSidebar({ active, onSelect, reviewCount, burn }: NavSidebarProps) {
  return (
    <nav
      aria-label="Primary"
      className="flex w-[172px] shrink-0 flex-col gap-[3px] overflow-clip rounded-[16px] bg-rail px-[13px] pb-[22px] pt-[16px] [box-shadow:#23211C0A_0px_1px_2px,#23211C21_0px_10px_26px_-12px]"
    >
      {/* Top section — Desk + Inbox (no eyebrow). */}
      <NavRow icon={LayoutGrid} label="Desk" active={active === "desk"} onClick={() => onSelect("desk")} />
      <NavRow icon={Inbox} label="Inbox" active={active === "inbox"} badge={reviewCount} onClick={() => onSelect("inbox")} />

      {/* KNOWLEDGE — Recall / Notes / Outputs / News, pinned (no collapse). */}
      <Eyebrow label="KNOWLEDGE" />
      {NAV[1].items.map((item) => (
        <NavRow
          key={item.key}
          icon={item.icon}
          label={item.label}
          active={active === item.key}
          onClick={() => onSelect(item.key)}
        />
      ))}

      {/* ACTIVITY. */}
      <Eyebrow label="ACTIVITY" />
      <NavRow icon={Activity} label="Activity" active={active === "activity"} onClick={() => onSelect("activity")} />

      {/* Spacer pushes SETTINGS to the bottom of the rail. */}
      <div className="min-h-[14px] grow" />

      {/* Always-on cost footer — fills the gap so today's burn shows on every
          screen (the Desk rail otherwise owns the only burn readout). */}
      {burn != null && <RailBurnFooter burn={burn} />}

      {/* SETTINGS — no top padding on its eyebrow (the spacer supplies the gap). */}
      <Eyebrow label="SETTINGS" bottomOnly />
      {NAV[3].items.map((item) => (
        <NavRow
          key={item.key}
          icon={item.icon}
          label={item.label}
          active={active === item.key}
          settings
          onClick={() => onSelect(item.key)}
        />
      ))}
    </nav>
  );
}

/**
 * A single flat nav leaf row. Matches the Paper rail row: h-38 (35 for settings),
 * 10px radius, 11px horizontal padding + icon gap, light teal text. The active
 * row fills with a 16% white pill and bumps the label to 600.
 */
function NavRow({
  icon: Icon, label, active, badge, settings, onClick,
}: {
  icon: LucideIcon;
  label: string;
  active: boolean;
  badge?: number;
  settings?: boolean;
  onClick: () => void;
}) {
  const showBadge = badge != null && badge > 0;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex shrink-0 items-center gap-[11px] rounded-[10px] px-[11px] transition-colors",
        "outline-none focus-visible:ring-2 focus-visible:ring-accent",
        settings ? "h-[35px]" : "h-[38px]",
        active ? "bg-[#FFFFFF29]" : "hover:bg-[#FFFFFF1A]",
      )}
    >
      <Icon size={settings ? 16 : 17} strokeWidth={1.8} className="shrink-0 text-white" />
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-left text-[13px] leading-none text-[#F4F8F7]",
          active ? "font-semibold" : "font-medium",
        )}
      >
        {label}
      </span>
      {showBadge && (
        <span className="flex h-[18px] min-w-[20px] shrink-0 items-center justify-center rounded-[9px] bg-red px-[6px]">
          <span className="mono text-[10.5px] font-semibold leading-none text-[#FBFAF7]">
            {badge}
          </span>
        </span>
      )}
    </button>
  );
}

/** Mono-uppercase faint section eyebrow on the rail. */
function Eyebrow({ label, bottomOnly }: { label: string; bottomOnly?: boolean }) {
  return (
    <div className={cn("px-[11px] pb-[7px]", !bottomOnly && "pt-[17px]")}>
      <div className="text-[11.5px] font-semibold uppercase leading-none tracking-[0.11em] text-[#CDE4E2]">
        {label}
      </div>
    </div>
  );
}

/**
 * Always-visible "burn · today" footer pinned above SETTINGS. Reads the same
 * real telemetry the Desk rail uses ({@link LLMBurnSummary.totals}) — today's
 * USD spend + call count, no fabricated cap or delta. Styled with the rail's
 * white-alpha-on-dark idiom so it reads in both the teal and navy themes.
 */
function RailBurnFooter({ burn }: { burn: LLMBurnSummary }) {
  const spent = burn.totals.costUsd;
  const calls = burn.totals.calls;
  return (
    <div className="mb-[12px] flex flex-col gap-[7px] rounded-[11px] border border-white/10 bg-white/[0.03] px-[11px] py-[10px]">
      <div className="flex items-center justify-between">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[#9FB6C2]">Burn · today</span>
        <span className="mono text-[9px] text-[#9FB6C2]">{calls} {calls === 1 ? "call" : "calls"}</span>
      </div>
      <div className="flex items-baseline gap-[5px]">
        <span className="mono text-[17px] font-semibold leading-none text-[#F4F8F7]">{fmtRailUsd(spent)}</span>
        <span className="text-[10px] text-[#9FB6C2]">spent today</span>
      </div>
    </div>
  );
}

/** Compact USD formatter for the rail footer — sub-cent floors to "<$0.01". */
function fmtRailUsd(v: number): string {
  if (v <= 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1) return `$${v.toFixed(3)}`;
  if (v < 100) return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}
