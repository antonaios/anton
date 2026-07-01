// #59-harness — every state-mutating call mints ONE UUID per logical action
// and reuses it on retries. Pairs with the X-ANTON-Run-Id middleware shipped
// in routines `b1c043a` (#59) — the backend's per-session coalescing lock
// uses run_id to discriminate same-id retries (409 same_run_id_retry: true,
// "poll the in-flight response") from genuinely-concurrent different-id
// contention (409 same_run_id_retry: false, "back off + retry fresh").
//
// Until the client mints stable ids per logical action, every retry looks
// like a fresh run_id to the backend and only the different-id 409 branch
// fires — which still kills the double-click duplicate, but skips the
// cleaner same-id polling semantic. This helper closes that gap.

export function newRunId(): string {
  return crypto.randomUUID();
}
