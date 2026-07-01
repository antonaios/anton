import { cn } from "../lib/cn";

interface DayEvent { text: string; flag?: boolean; }
interface Day { initial: string; date: number; today?: boolean; events: DayEvent[]; }

interface Props {
  range: string;          // right-side day range, e.g. "Fri – Sun"
  days: Day[];
}

/**
 * "Quiet desk" right-rail Week panel — Outlook calendar stub (Paper D1-0).
 *
 * Header "THIS WEEK" label + the day range; one row per day — a stacked
 * date block (initial over the number) beside that day's event list. Today's
 * block lights up in accent and its event dots warm to amber; the rest sit
 * muted. Rows are separated by a dashed hairline. Real wire-up is MS Graph
 * OAuth2 (gated on credentials); until then the operator clicks the range to
 * open Outlook wherever they check it.
 */
export function WeekPanel({ range, days }: Props) {
  return (
    <div className="flex flex-col gap-[14px]">
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-t3">This week</span>
        <span className="tabular cursor-pointer text-[11px] text-t3 transition-colors hover:text-t1">
          {range} →
        </span>
      </div>

      <div className="flex flex-col">
        {days.map((d, di) => (
          <div
            key={d.initial + d.date}
            className={cn(
              "flex items-start gap-[14px] py-[11px]",
              di > 0 && "border-t border-dashed border-line",
            )}
          >
            {/* Stacked date block — accent for today, muted otherwise */}
            <div className="flex w-[34px] shrink-0 flex-col items-start leading-none">
              <span
                className={cn(
                  "text-[10px] font-semibold uppercase tracking-[0.1em]",
                  d.today ? "text-accent" : "text-t3",
                )}
              >
                {d.initial}
              </span>
              <span
                className={cn(
                  "tabular mt-[3px] text-[19px] font-semibold tracking-[-0.02em]",
                  d.today ? "text-t1" : "text-t2",
                )}
              >
                {d.date}
              </span>
            </div>

            {/* Event rows — a status dot + the event label (time is inline) */}
            <div className="flex min-w-0 grow flex-col gap-[7px] pt-[1px]">
              {d.events.map((ev, i) => (
                <div key={i} className="flex items-baseline gap-[8px]">
                  <span
                    className={cn(
                      "mt-[5px] h-[5px] w-[5px] shrink-0 rounded-full",
                      d.today ? "bg-accent" : "bg-line-2",
                    )}
                  />
                  <span
                    className={cn(
                      "min-w-0 grow text-[12px] leading-[1.3]",
                      d.today ? "text-t1" : "text-t2",
                    )}
                  >
                    {ev.text}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
