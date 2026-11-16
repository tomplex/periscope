// A select request from another app (POST /api/select), riding the state blob as `select`:
// { pid, at } with `at` in epoch ms. Each tab applies a request once, and only while it is
// fresh, so a tab opened later does not jump to a pane someone asked for minutes ago.
export const SELECT_FRESH_MS = 30_000;

/** The live window to select now, or null. `lastAt` is the `at` of the request this tab applied last. */
export function selectToApply(select, lastAt, nowMs, windows) {
  if (!select?.pid || select.at === lastAt) return null;
  if (nowMs - select.at > SELECT_FRESH_MS) return null;
  return windows.find((w) => w.pid === select.pid) ?? null;
}
