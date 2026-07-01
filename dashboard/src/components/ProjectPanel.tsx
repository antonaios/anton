import { cn } from "../lib/cn";
import type {
  ProjectOverview, ProjectKeyDate, ClientSide, ProjectStatus, Sensitivity,
  WorkspaceType,
} from "../types";

interface Props {
  name: string;             // "DemoTarget" (workspace.name passthrough)
  /** Workspace type — drives the non-project placeholder state (#12b). */
  workspaceType?: WorkspaceType;
  onOpenVault?: () => void;
  // ── Live mode (Phase 2 part 4) ──────────────────────────────────────────
  /** Loaded ProjectOverview. When present, replaces the milestones-only render. */
  overview?: ProjectOverview | null;
  /** Hydration in flight — render skeleton over header + body. */
  loading?: boolean;
  /** Fetch error — render inline chip in header; keep stage + name visible. */
  error?: string | null;
}

/**
 * v5 right-rail Project panel.
 *
 *   1. **Overview mode** (`workspaceType === "project"` + `overview` set) —
 *      renders the rich shape with status / side / sensitivity badges, sector
 *      line, tldr, target / counterparty / client rows, opened / closed
 *      dates, and the keyDates list with status dots. Live data from
 *      /api/projects/{name}/overview.
 *   2. **Loading state** — skeleton bars while the overview fetch is mid-flight.
 *   3. **Non-project placeholder** (`workspaceType !== "project"`) — explicit
 *      message that the panel is project-scoped. No demo data leaks through
 *      (#12b — Session F polish).
 */
export function ProjectPanel({
  name, workspaceType, onOpenVault, overview, loading, error,
}: Props) {
  const isOverview = overview != null;
  const isProjectWorkspace = workspaceType === "project";
  const headerStage = (overview?.stage ?? "—").toString().toUpperCase();

  return (
    <div>
      {/* Section head */}
      <div className="mb-[12px] flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-t3">Project</span>
        <div className="flex items-center gap-[10px] text-[11px]">
          {error && (
            <span className="inline-flex items-center gap-[4px] text-red" title={error}>
              <span className="h-[5px] w-[5px] rounded-full bg-red" />
              error
            </span>
          )}
          {loading && (
            <span className="italic text-t3">loading…</span>
          )}
          <span
            className="cursor-pointer text-t3 transition-colors hover:text-t1"
            onClick={onOpenVault}
          >Vault →</span>
        </div>
      </div>

      {/* Title + badges row */}
      <div className="mb-[10px] flex flex-wrap items-center gap-[8px]">
        <h2 className="text-[15px] font-semibold tracking-[-0.01em] text-t1">{name}</h2>
        {overview?.stage && (
          <span className="text-[10px] uppercase tracking-[0.14em] text-accent">{headerStage}</span>
        )}
        {overview?.status      && <StatusBadge value={overview.status} />}
        {overview?.clientSide  && <SideBadge value={overview.clientSide} />}
        {overview?.sensitivity && <SensitivityBadge value={overview.sensitivity} />}
      </div>

      {/* Sector · subsector · industry line */}
      {isOverview && (overview!.sector || overview!.subsector || overview!.industry) && (
        <div className="mb-[6px] text-[11.5px] text-t3">
          {[overview!.sector, overview!.subsector, overview!.industry].filter(Boolean).join(" · ")}
        </div>
      )}

      {/* TLDR */}
      {overview?.tldr && (
        <p className="mb-[12px] mt-[8px] text-[12.5px] leading-[160%] text-t2">
          {overview.tldr}
        </p>
      )}

      {/* Target / counterparty / client / opened / closed */}
      {isOverview && (
        <dl className="mt-[12px] grid grid-cols-[auto_1fr] gap-x-[14px] gap-y-[8px] rounded-xl border border-line bg-bg-1 px-[14px] py-[13px] text-[11px]">
          {overview!.target       && <Row label="Target"        value={overview!.target} />}
          {overview!.counterparty && <Row label="Counterparty"  value={overview!.counterparty} />}
          {overview!.client       && <Row label="Client"        value={overview!.client} />}
          {overview!.owner        && <Row label="Owner"         value={overview!.owner} />}
          {overview!.opened       && <Row label="Opened"        value={overview!.opened} />}
          {overview!.closed       && <Row label="Closed"        value={overview!.closed} />}
        </dl>
      )}

      {/* Key dates list (overview mode only) */}
      {isOverview && overview!.keyDates.length > 0 && (
        <div className="mt-[16px]">
          <div className="mb-[8px] text-[10px] uppercase tracking-[0.14em] text-t3">Key dates</div>
          <div className="flex flex-col gap-[7px]">
            {overview!.keyDates.map((kd) => <KeyDateRow key={kd.label} kd={kd} />)}
          </div>
        </div>
      )}

      {/* Skeleton when loading + nothing else to show yet */}
      {loading && !isOverview && (
        <div className="mt-[14px] flex flex-col gap-[8px]">
          {[68, 92, 54].map((w, i) => (
            <div key={i} className="h-[12px] rounded-md bg-bg-2 border border-line"
              style={{ width: `${w}%`, opacity: 0.5, animation: "pulse 1.8s ease-in-out infinite", animationDelay: `${i * 0.15}s` }} />
          ))}
        </div>
      )}

      {/* Non-project workspace placeholder (#12b) — explicit, no demo leak.
          Shows for BD / General workspaces; project workspaces with a 404
          surface via the header error chip instead. */}
      {!isOverview && !loading && !isProjectWorkspace && (
        <div className="mt-[14px] rounded-xl border border-line bg-bg-1 px-[14px] py-[14px] text-[11px] leading-[1.5] text-t3">
          <div className="mb-[4px] text-t2">No project brief.</div>
          Right-rail data is project-scoped.{" "}
          {workspaceType === "bd" && "BD workspaces don't have a brief.md (yet)."}
          {workspaceType === "general" && "General workspaces are ad-hoc — no brief expected."}
          {!workspaceType && "Switch to a project workspace to see live brief data."}
        </div>
      )}

      {/* Project workspace, fetch finished, no overview returned, no error. */}
      {!isOverview && !loading && isProjectWorkspace && !error && (
        <p className="mt-[10px] text-[11px] italic text-t3">
          Project brief not found in vault. Drop a <span className="mono">00 Brief.md</span> in <span className="mono">Projects/{name}/</span>.
        </p>
      )}

      {/* Last touched footer (overview only) */}
      {overview?.lastTouched && (
        <div className="mt-[14px] text-[10px] tracking-[0.06em] text-t4">
          brief touched {formatLastTouched(overview.lastTouched)}
        </div>
      )}
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-[10px] uppercase tracking-[0.1em] text-t3">{label}</dt>
      <dd className="truncate text-[12px] text-t2" title={value}>{value}</dd>
    </>
  );
}

function KeyDateRow({ kd }: { kd: ProjectKeyDate }) {
  return (
    <div className={cn(
      "flex items-baseline justify-between text-[12px]",
      kd.state === "next" ? "text-accent" : "text-t2",
    )}>
      <span className="flex items-baseline gap-[8px]">
        <span className={cn(
          "inline-block h-[6px] w-[6px] -translate-y-[1px] rounded-full",
          kd.state === "done"   && "bg-ok",
          kd.state === "next"   && "bg-accent",
          kd.state === "future" && "bg-t4",
        )} />
        {kd.label}
      </span>
      <span className={cn("tabular text-[11px]", kd.state === "next" ? "text-accent" : "text-t3")}>
        {kd.date ?? "—"}
      </span>
    </div>
  );
}

function StatusBadge({ value }: { value: ProjectStatus }) {
  // live = bright; paused = quiet; won = accent (good); lost = accent dim; archived = quiet
  const cls = (
    value === "live"     ? "border-green/40 bg-green/15 text-green"   :
    value === "won"      ? "border-green/40 bg-green/15 text-green"   :
    value === "paused"   ? "border-line-2   bg-bg-2     text-t3"      :
    value === "lost"     ? "border-accent-line bg-accent-soft text-accent"  :
    /* archived */         "border-line-2   bg-bg-2     text-t3 opacity-70"
  );
  return <Badge label={value} className={cls} />;
}

function SideBadge({ value }: { value: ClientSide }) {
  return <Badge label={value === "advisory" ? "ADV" : value.toUpperCase()} className="border-line-2 bg-bg-2 text-t2" />;
}

function SensitivityBadge({ value }: { value: Sensitivity }) {
  // Sensitivity map: public=t3, internal=t2, confidential=amber, MNPI=red.
  const cls = (
    value === "MNPI"         ? "border-red/40    bg-red/15    text-red" :
    value === "confidential" ? "border-amber/40  bg-amber/15  text-amber" :
    value === "internal"     ? "border-line-2    bg-bg-2      text-t2" :
    /* public */               "border-line-2    bg-bg-2      text-t3"
  );
  const short = value === "MNPI" ? "MNPI" : value.slice(0, 3).toUpperCase();
  return <Badge label={short} className={cls} title={value} />;
}

function Badge({ label, className, title }: { label: string; className: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-block rounded-md border px-[7px] py-[2px] text-[9px] font-semibold uppercase tracking-[0.08em]",
        className,
      )}
    >{label}</span>
  );
}

function formatLastTouched(iso: string): string {
  try {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return iso;
    const now = new Date();
    const sec = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
    if (sec < 60)     return `${sec}s ago`;
    const min = Math.round(sec / 60);
    if (min < 60)     return `${min}m ago`;
    const hr  = Math.round(min / 60);
    if (hr < 24)      return `${hr}h ago`;
    const day = Math.round(hr / 24);
    if (day === 1)    return "yesterday";
    if (day < 7)      return `${day}d ago`;
    return d.toISOString().slice(0, 10);
  } catch { return iso; }
}
