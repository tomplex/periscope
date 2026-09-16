// The Claude subscriptions periscope pools across. The registry is server-side
// (store.get_accounts) and rides /api/state as `accounts` into the `accounts`
// signal; nothing on the client hardcodes an account.
//
// Shared so every surface that names an account uses the SAME letter: the
// launcher's account picker and the usage pill's per-account meters. Divergent
// labels ("B" vs "account B" vs "@b") would make it impossible to tell that the
// pane you just launched is the one whose bar is pinned at 100%.
import { accounts, moveTargets } from "./store.js";

// Letter for an account id. Unknown ids (a hand-edited state.json) fall back to
// the raw id rather than being dropped — a meter with no name still beats a
// silently missing subscription.
export function accountLabel(id, registry = accounts.value) {
  return registry.find((a) => a.id === id)?.label || id;
}

// Whether there is more than one account to tell apart. Every account-naming
// surface (pickers, move chip, rail account chip, pill letters) hides without
// it: with a single subscription there is no choice to make and nothing to name.
export function multiAccount(registry = accounts.value) {
  return registry.length > 1;
}

// Where "move this pane to another subscription" sends it, or null when the
// action doesn't apply. The destination is the server's pick (`move_targets`,
// launch_policy.choose_move) so the chip names exactly what the click sends.
// Pure so the rail row only renders the answer.
//
// null when:
//   - a shell pane has no Claude session to resume;
//   - `account: "unknown"` is a CLAUDE_CONFIG_DIR no registered account claims,
//     so we can't say which subscription it's on — and therefore can't pick
//     another. Guessing could move it ONTO the exhausted account, the one
//     outcome this feature exists to avoid;
//   - the server offered no target (a single account, or a poll that predates
//     the target).
//
// A missing `account` is the default account: /api/state stamps the field on
// every window, but a rolling reload can paint rows from a pre-account server.
export function moveAccountTarget(w, registry = accounts.value, targets = moveTargets.value) {
  if (!(w.agent === "claude" || w.is_claude)) return null;
  const current = w.account || "default";
  if (!registry.some((a) => a.id === current)) return null;
  const id = targets?.[current];
  return registry.find((a) => a.id === id && id !== current) || null;
}
