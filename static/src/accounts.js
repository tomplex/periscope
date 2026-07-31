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
