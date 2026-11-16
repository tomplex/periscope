// A select request from another app (POST /api/select), riding the state blob as `select`:
// { pid, at } with `at` in epoch ms. Each tab applies a request once, and only while it is
// fresh, so a tab opened later does not jump to a pane someone asked for minutes ago.
export const SELECT_FRESH_MS = 30_000;

/** `window`: the live window to select now, or null. `lastAt`: the `at` of the request this tab
 * applied last, moved on only when a window is returned, so a request whose pane appears on a later
 * state still applies then. */
export function selectToApply(select, lastAt, nowMs, windows) {
  const fresh = select?.pid && select.at !== lastAt && nowMs - select.at <= SELECT_FRESH_MS;
  const w = fresh ? windows.find((x) => x.pid === select.pid) : null;
  return w ? { window: w, lastAt: select.at } : { window: null, lastAt };
}
