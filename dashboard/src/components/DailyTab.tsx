import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { CalendarDays, ExternalLink, FileText } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import type { DailyResponse, RecentDailyResponse } from "../types";

/**
 * Lightweight markdown renderer for the daily-note body — no new dependency.
 * Parses `content` line-by-line into the artboard's document-body register:
 *   · "## "/"# " heading  → accent vertical tab-marker + 13.5px semibold heading
 *   · "- [ ]"/"- [x]"     → 16px checkbox square (filled+strike when done) + text
 *   · leading "HH:MM"      → fixed-width mono time column + body text
 *   · indented "[[wiki]]"  → accent-mono "↳ [[name]]"
 *   · "- " bullet          → dot + text
 *   · anything else        → plain 13px body line
 * Unknown lines fall through to plain text so the render never throws.
 */
function renderNoteBody(content: string): ReactNode {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];

  lines.forEach((raw, i) => {
    const key = `ln-${i}`;
    const line = raw.replace(/\s+$/, "");
    const trimmed = line.trim();

    // Blank line → skip; spacing is structural (flex gap + section margins).
    if (trimmed === "") {
      return;
    }

    // (a) Section heading — "# " or "## " (and deeper).
    const heading = /^#{1,6}\s+(.*)$/.exec(trimmed);
    if (heading) {
      // 15px section gap (artboard) above each heading except the first node.
      const top = out.length === 0 ? "" : "mt-[7px] ";
      out.push(
        <div key={key} className={`${top}flex items-center gap-2`}>
          <div className="h-[13px] w-[3px] shrink-0 rounded-[2px] bg-accent" />
          <div className="text-[13.5px] font-semibold leading-4 text-t1">{heading[1]}</div>
        </div>,
      );
      return;
    }

    // (b) Todo rows — "- [ ] " (open) / "- [x] " (done).
    const todo = /^-\s*\[( |x|X)\]\s+(.*)$/.exec(trimmed);
    if (todo) {
      const done = todo[1].toLowerCase() === "x";
      out.push(
        <div key={key} className="flex items-center gap-2.5 pl-[11px]">
          {done ? (
            <div className="flex h-[15px] w-[15px] shrink-0 items-center justify-center rounded-[4px] bg-accent">
              <svg width="9" height="9" viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg">
                <path
                  d="M1.5 5L4 7.5L8.5 2.5"
                  fill="none"
                  stroke="var(--card)"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
          ) : (
            <div className="h-[15px] w-[15px] shrink-0 rounded-[4px] border-[1.5px] border-solid border-line-2" />
          )}
          <div
            className={
              done
                ? "text-[13px] leading-[150%] text-t3 line-through decoration-1"
                : "text-[13px] leading-[150%] text-t1"
            }
          >
            {todo[2]}
          </div>
        </div>,
      );
      return;
    }

    // (d) Indented wikilink → accent-mono "↳ [[name]]".
    const indented = raw.startsWith("  ") || raw.startsWith("\t");
    const wikiOnly = /^(?:↳\s*)?(\[\[[^\]]+\]\])$/.exec(trimmed);
    if (indented && wikiOnly) {
      out.push(
        <div key={key} className="pl-4">
          <div className="mono text-[11px] leading-[15px] text-accent">↳ {wikiOnly[1]}</div>
        </div>,
      );
      return;
    }

    // (d) Bullet — "- " / "* ".
    const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      out.push(
        <div key={key} className="flex gap-2 pl-[11px] text-[13px] leading-[150%] text-t2">
          <span aria-hidden className="select-none text-t3">•</span>
          <span className="min-w-0">{bullet[1]}</span>
        </div>,
      );
      return;
    }

    // (c) Leading "HH:MM" time → fixed-width mono time column + body text.
    const timed = /^(\d{1,2}:\d{2})\s+(.*)$/.exec(trimmed);
    if (timed) {
      out.push(
        <div key={key} className="flex gap-[9px] pl-[11px]">
          <div className="mono w-[42px] shrink-0 text-[12px] leading-[150%] text-t3">{timed[1]}</div>
          <div className="text-[13px] leading-[150%] text-t2">{timed[2]}</div>
        </div>,
      );
      return;
    }

    // (e) Plain line.
    out.push(
      <div key={key} className="pl-[11px] text-[13px] leading-[150%] text-t2">
        {line}
      </div>,
    );
  });

  return <div className="flex flex-col gap-2">{out}</div>;
}

/** "2026-06-23" → "Mon 23 Jun — Daily" (UTC). Falls back to a plain label if
 *  the date can't be parsed, so a stray name never throws. */
function dailyTitleFromDate(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return `Note — ${iso}`;
  const weekday = parsed.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
  const dayMonth = parsed.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  return `${weekday} ${dayMonth} — Daily`;
}

/** Whole-day relative label from an ISO date to today (UTC): today / Nd ago /
 *  Nw ago / Nmo ago. Daily notes are day-granular, so no hour precision. */
function relativeAgo(iso: string): string {
  const then = new Date(`${iso}T00:00:00Z`).getTime();
  if (Number.isNaN(then)) return "";
  const now = new Date();
  const todayUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.round((todayUtc - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "1d ago";
  if (days < 7) return `${days}d ago`;
  if (days < 28) return `${Math.floor(days / 7)}w ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

/**
 * Daily tab — show today's note if it exists, with a deep-link into
 * Obsidian. No editor inside the dashboard — Obsidian is the editor.
 *
 * v5 look: a daily-note document hero (DAILY badge + title + body) sitting
 * beside a slim "recent notes" rail. The dashboard stays a read-only mirror —
 * the note body is rendered verbatim from the vault; edits happen in Obsidian.
 */
export function DailyTab() {
  const [data, setData] = useState<DailyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Recent-notes rail (from /api/daily/recent). Kept separate + best-effort so a
  // failure (e.g. an older bridge without the endpoint) degrades the rail to the
  // today-only fallback below rather than breaking the page.
  const [recent, setRecent] = useState<RecentDailyResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.dailyToday()
      .then((r) => { if (!cancelled) setData(r); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Unknown error"); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.dailyRecent(6)
      .then((r) => { if (!cancelled) setRecent(r); })
      .catch(() => { /* best-effort — the rail falls back to today only */ });
    return () => { cancelled = true; };
  }, []);

  const obsidianHref = data ? `obsidian://open?path=${encodeURIComponent(data.path)}` : "";

  // Friendly date label ("Mon · 23 Jun 2026") derived from the ISO `date`.
  let dateLabel = data?.date ?? "";
  let docTitle = data?.date ? `Note — ${data.date}` : "Daily note";
  if (data?.date) {
    const parsed = new Date(`${data.date}T00:00:00Z`);
    if (!Number.isNaN(parsed.getTime())) {
      const weekday = parsed.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
      const dayMonth = parsed.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
      const year = parsed.toLocaleDateString("en-GB", { year: "numeric", timeZone: "UTC" });
      dateLabel = `${weekday} · ${dayMonth} ${year}`;
      docTitle = `${weekday} ${dayMonth} — Daily`;
    }
  }

  const sizeLabel = data?.size_bytes ? `${(data.size_bytes / 1024).toFixed(1)} KB` : "";

  // Parse the vault note body into the artboard's document register once per load.
  const noteBody = useMemo(() => renderNoteBody(data?.content ?? ""), [data?.content]);

  return (
    <div className="flex h-fit max-w-[1060px] flex-col gap-5 px-7 py-[30px]">
      {/* ── Page header ──────────────────────────────────────────────── */}
      <header className="flex flex-col gap-[7px]">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">
            Notes
          </h2>
          {data?.path && (
            <span className="mono shrink-0 text-[11px] leading-[14px] text-t3">
              {data.path}{sizeLabel ? ` · ${sizeLabel}` : ""}
            </span>
          )}
        </div>
        <p className="text-[13px] leading-[150%] text-t2">
          Your daily and working notes — a read-only mirror of the vault. Edits happen in Obsidian.
        </p>
      </header>

      {/* ── Date row + Obsidian deep-link ────────────────────────────── */}
      <div className="flex items-center justify-between gap-3.5">
        <div className="flex items-center gap-[9px]">
          <CalendarDays size={14} className="shrink-0 text-t3" />
          <span className="shrink-0 text-[12.5px] font-medium leading-4 text-t1">
            {dateLabel || "Today"}
          </span>
          {data?.date && (
            <span className="flex h-5 shrink-0 items-center rounded-[6px] bg-accent-soft px-2 text-[10.5px] font-semibold leading-[14px] tracking-[0.04em] text-accent">
              TODAY
            </span>
          )}
        </div>
        {data && (
          <a
            href={obsidianHref}
            className="flex h-[34px] items-center gap-[7px] rounded-[9px] border border-line-2 bg-bg-1 px-3.5 text-[12.5px] leading-4 text-accent transition-colors hover:bg-bg-2"
          >
            Open in Obsidian <ExternalLink size={13} className="shrink-0" />
          </a>
        )}
      </div>

      {error && (
        <div className="rounded-lg border border-red/40 bg-red/10 px-3.5 py-2.5 text-[12px] text-red">
          Bridge offline — {error}
        </div>
      )}

      {/* ── Document hero + recent-notes rail ────────────────────────── */}
      {data && (
        <div className="flex h-fit items-start gap-[18px]">
          {/* Document card */}
          <div className="flex min-w-0 grow basis-0 flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-card">
            {data.exists ? (
              <>
                <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-[15px]">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <span className="flex h-5 shrink-0 items-center rounded-[5px] bg-accent-soft px-2 text-[9.5px] font-bold leading-3 tracking-[0.08em] text-accent">
                      DAILY
                    </span>
                    <h3 className="truncate text-[14px] font-semibold leading-[18px] text-t1">
                      {docTitle}
                    </h3>
                  </div>
                  <span className="mono w-max shrink-0 text-[11px] leading-[14px] text-t3">
                    {sizeLabel}
                  </span>
                </div>
                <div className="max-h-[58vh] overflow-y-auto px-[22px] py-[18px]">
                  {noteBody}
                </div>
              </>
            ) : (
              <div className="flex flex-col items-start gap-3 px-[22px] py-8">
                <FileText size={20} className="text-t3" />
                <h3 className="text-[14px] font-semibold leading-[18px] text-t1">
                  No daily note for today
                </h3>
                <p className="max-w-prose text-[13px] leading-[150%] text-t2">
                  Today's daily note doesn't exist yet — it would live at{" "}
                  <span className="mono text-t1">{data.path}</span>. Open it in Obsidian to create it.
                </p>
                <a
                  href={obsidianHref}
                  className="mt-1 flex h-[34px] items-center gap-[7px] rounded-[9px] border border-line-2 bg-bg-1 px-3.5 text-[12.5px] leading-4 text-accent transition-colors hover:bg-bg-2"
                >
                  Open in Obsidian <ExternalLink size={13} className="shrink-0" />
                </a>
              </div>
            )}
          </div>

          {/* Recent-notes rail — the last N daily notes from /api/daily/recent
              (metadata only; each row deep-links into Obsidian, today carries a
              badge). Falls back to a today-only entry when the recent endpoint
              is unavailable (older bridge), so it degrades gracefully. */}
          <div className="flex w-[360px] shrink-0 flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-card">
            <div className="flex items-center justify-between border-b border-line px-4 py-3.5">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-semibold leading-[14px] tracking-[0.1em] text-t3">
                  RECENT
                </span>
                <span className="mono text-[11px] leading-[14px] text-t4">
                  {recent && recent.items.length > 0 ? recent.total : data.exists ? 1 : 0}
                </span>
              </div>
              <span className="text-[11px] leading-[14px] text-t3">General</span>
            </div>
            {recent && recent.items.length > 0 ? (
              recent.items.map((it) => {
                const isToday = it.date === data.date;
                return (
                  <a
                    key={it.path}
                    href={`obsidian://open?path=${encodeURIComponent(it.path)}`}
                    className={cn(
                      "flex flex-col gap-[5px] border-b border-line px-4 py-3 transition-colors last:border-b-0",
                      isToday ? "bg-bg-2" : "hover:bg-bg-2",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="line-clamp-1 min-w-0 text-[13px] font-semibold leading-[135%] text-t1">
                        {dailyTitleFromDate(it.date)}
                      </span>
                      {isToday && (
                        <span className="flex h-4 shrink-0 items-center rounded-[4px] bg-accent-soft px-1.5 text-[8px] font-bold leading-none tracking-[0.06em] text-accent">
                          TODAY
                        </span>
                      )}
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="mono line-clamp-1 min-w-0 text-[10px] leading-[13px] text-accent">
                        {it.path}
                      </span>
                      <span className="mono w-max shrink-0 text-[10.5px] leading-[13px] text-t3">
                        {relativeAgo(it.date)}
                      </span>
                    </div>
                  </a>
                );
              })
            ) : data.exists ? (
              <a
                href={obsidianHref}
                className="flex flex-col gap-[5px] px-4 py-3 transition-colors hover:bg-bg-2"
              >
                <div className="line-clamp-1 text-[13px] font-semibold leading-[135%] text-t1">
                  {docTitle}
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="mono line-clamp-1 min-w-0 text-[10px] leading-[13px] text-accent">
                    {data.path}
                  </span>
                  <span className="mono w-max shrink-0 text-[10.5px] leading-[13px] text-t3">
                    today
                  </span>
                </div>
              </a>
            ) : (
              <div className="px-4 py-3 text-[12px] text-t3">No notes mirrored yet.</div>
            )}
            {((recent && recent.total > 0) || data.exists) && (
              <div className="px-4 pt-2 pb-3 text-[10.5px] leading-[150%] text-t3">
                Older notes live in the vault — open Obsidian to browse the full history.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
