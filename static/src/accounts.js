// The Claude subscriptions periscope pools across. The registry is server-side
// in store.py (_DEFAULT_ACCOUNTS) and is NOT exposed over HTTP — hardcoded here
// because there are exactly two. Read it from an endpoint if that ever grows.
//
// Shared so every surface that names an account uses the SAME letter: the
// launcher's account picker and the usage pill's per-account meters. Divergent
// labels ("B" vs "account B" vs "@b") would make it impossible to tell that the
// pane you just launched is the one whose bar is pinned at 100%.
export const ACCOUNTS = [
  { id: "default", label: "A" },
  { id: "b", label: "B" },
];

// Letter for an account id. Unknown ids (a hand-edited state.json) fall back to
// the raw id rather than being dropped — a meter with no name still beats a
// silently missing subscription.
export function accountLabel(id) {
  return ACCOUNTS.find((a) => a.id === id)?.label || id;
}

// Where "move this pane to the other subscription" sends it, or null when the
// action doesn't apply. Pure so the rail row only renders the answer.
//
// null in two cases, both deliberate:
//   - a shell pane has no Claude session to resume;
//   - `account: "unknown"` is a CLAUDE_CONFIG_DIR no registered account claims,
//     so we can't say which subscription it's on — and therefore can't say
//     which one is "the other". Guessing could move it ONTO the exhausted
//     account, the one outcome this feature exists to avoid.
//
// A missing `account` is the default account: /api/state stamps the field on
// every window, but a rolling reload can paint rows from a pre-account server.
export function moveAccountTarget(w, accounts = ACCOUNTS) {
  if (!(w.agent === "claude" || w.is_claude)) return null;
  const current = w.account || "default";
  const i = accounts.findIndex((a) => a.id === current);
  if (i < 0) return null;
  return accounts[(i + 1) % accounts.length];
}
