/**
 * v5 footer bar — 2-column grid (function keys left, system info right).
 *
 * Per the mockup: 10.5px text in t3, key chips in t2, values in t2 bold-ish.
 * No more vertical dividers between keys (cleaner). Compact 8×22px padding.
 *
 * Renamed conceptually to FootBar but the export stays `FnStrip` to avoid
 * breaking App.tsx imports during the v5 rewrite. Phase 2 / 3 may rename
 * the file once everything has settled.
 */
export function FnStrip() {
  return (
    <footer className="grid grid-cols-[1fr_auto] items-center gap-6 border-t border-line bg-bg px-[22px] py-[8px] text-[10.5px] tracking-[0.06em] text-t3">
      <div>
        <span className="mr-[14px]"><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F1</span>help</span>
        <span className="mr-[14px]"><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F2</span>recall</span>
        <span className="mr-[14px]"><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F3</span>jump</span>
        <span className="mr-[14px]"><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F4</span>new-session</span>
        <span className="mr-[14px]"><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F5</span>reindex</span>
        <span><span className="mr-1 text-t2 border border-line px-[5px] py-[1px] text-[10px]">F10</span>quit</span>
      </div>
      <div>
        <span className="ml-[16px] text-t2"><span className="text-t3">routing </span>local · ollama qwen3:14b</span>
        <span className="ml-[16px] text-t2"><span className="text-t3">index </span>4,128 chunks · 3m</span>
        <span className="ml-[16px] text-t2"><span className="text-t3">£ today </span>1.42</span>
      </div>
    </footer>
  );
}
