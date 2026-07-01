import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { ActivityItem } from "../types";
import { Card } from "./ui/Card";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * Vault tab — recent vault activity + a pointer to the omnibox for recall.
 * Mirrors what the Streamlit MVP showed under "Vault Pulse".
 *
 * Restyled to the v5 Activity feed look (Activity · light@2x): recent
 * vault-change rows, each a VAULT badge + the file path (mono) + the change
 * metric (new / updated) and a status dot. Token-only so it flips between the
 * LIGHT teal and DARK navy+gold themes. Behaviour is byte-for-byte the same as
 * before — only the presentation changed.
 */
export function VaultTab() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [hours, setHours] = useState(24);
  const [limit, setLimit] = useState(25);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.vaultPulse(hours, limit);
      setItems(res.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { void refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  return (
    <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-[20px] px-[24px] py-[28px] text-t1">

      {/* No per-view H2 — the single "Activity" header lives in ActivityTab.
          This view is a bare vault-pulse feed card under the toggle. */}

      {/* Vault pulse feed — Activity v5 feed look */}
      <Card padded={false}>
        {/* Feed head: title + count, then the window / limit / refresh controls */}
        <div className="flex flex-wrap items-center justify-between gap-[12px] border-b border-line px-[18px] py-[13px]">
          <div className="flex items-baseline gap-[8px]">
            <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-t3">Vault pulse</span>
            <span className="tabular text-[11px] text-t4">{items.length} items</span>
          </div>
          <div className="flex items-center gap-[8px]">
            <label className="text-[10px] uppercase tracking-[0.12em] text-t4">Window</label>
            <select
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
              className="mono rounded-lg border border-line-2 bg-bg-2 px-[8px] py-[4px] text-[11px] text-t1 outline-none transition-colors focus:border-accent-line"
            >
              <option value={1}>1h</option>
              <option value={6}>6h</option>
              <option value={24}>24h</option>
              <option value={72}>3d</option>
              <option value={168}>1w</option>
            </select>
            <label className="ml-[4px] text-[10px] uppercase tracking-[0.12em] text-t4">Limit</label>
            <input
              type="number"
              value={limit}
              min={1}
              max={50}
              onChange={(e) => setLimit(Math.max(1, Math.min(50, Number(e.target.value) || 25)))}
              className="mono w-[52px] rounded-lg border border-line-2 bg-bg-2 px-[8px] py-[4px] text-[11px] text-t1 outline-none transition-colors focus:border-accent-line"
            />
            <button
              type="button"
              onClick={() => void refresh()}
              className="ml-[4px] flex h-[28px] items-center rounded-lg border border-line-2 px-[12px] text-[11.5px] text-t2 transition-colors hover:border-accent-line hover:text-t1"
            >
              {busy ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {error ? (
          <div className="px-[18px] py-[14px] text-[12.5px] text-red">Bridge offline — {error}</div>
        ) : items.length === 0 ? (
          <div className="px-[18px] py-[14px] text-[12.5px] text-t3">
            No vault activity in the last {hours}h.
          </div>
        ) : (
          <ul className="flex flex-col">
            {items.map((it) => {
              const created = it.kind === "CREATED";
              return (
                <li
                  key={it.path}
                  className="flex items-center gap-[16px] border-b border-line px-[18px] py-[13px] last:border-b-0"
                >
                  {/* Leading slot: timestamp + VAULT badge */}
                  <span className="tabular w-[44px] shrink-0 text-[11px] text-t3">{it.ago}</span>
                  <span className="shrink-0 rounded-[5px] bg-paper2 px-[8px] text-[9px] font-bold uppercase tracking-[0.06em] text-green">VAULT</span>

                  {/* Path (primary, mono) + change context (secondary) */}
                  <div className="flex min-w-0 flex-1 flex-col gap-[3px]">
                    <span className="mono truncate text-[12.5px] text-t1" title={it.path}>{it.path}</span>
                    <span className="mono text-[11px] text-t3">{created ? "created" : "updated"}</span>
                  </div>

                  {/* Trailing slot: change metric + status dot */}
                  <span className="flex shrink-0 items-center gap-[8px]">
                    <span className={created ? "text-[11.5px] text-green" : "text-[11.5px] text-t3"}>
                      {created ? "new" : "updated"}
                    </span>
                    <StatusBadge status="ok" />
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
