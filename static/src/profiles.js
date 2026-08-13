// The `claude` wrapper profiles periscope can spawn into. A profile selects
// which plugin set and system prompt a pane runs under; the registry is
// server-side in config.py (CLAUDE_PROFILES) and is NOT exposed over HTTP —
// hardcoded here because there are exactly two. Read it from an endpoint if
// that ever grows.
//
// Orthogonal to the ACCOUNT (accounts.js): the account picks which subscription
// a pane bills, the profile picks what it can do. A lab pane on account B is a
// normal combination, not a conflict.
export const PROFILES = [
  { id: "default", label: "normal" },
  { id: "lab", label: "lab" },
];

// Whether a launch target should carry the profile at all.
//
// Same guard, and the same reason, as `sendsAccount`: the server sets it as a
// tmux `-e` var, so every process in that window inherits it. A SHELL window
// that carried it would run a hand-typed `claude` on the lab plugin set
// silently — and invisibly, because the rail's profile chip is derived from a
// live claude process, which a shell window has none of. Codex doesn't go
// through the claude wrapper at all.
// Pure: exported for unit tests.
export function sendsProfile(t) {
  return t?.mode === "agent" && (t.agent || "claude") === "claude";
}

// profile id → the `profile` query param, or null to omit it entirely.
// The server fails OPEN to the default on an unknown id (config.profile_env),
// so a param is the risky direction: omitting it keeps the default launch
// byte-identical to the pre-profiles URL.
// Pure: exported for unit tests.
export function profileQuery(p) {
  return !p || p === "default" ? null : p;
}

// Label for a profile id. Unknown ids (a pane whose CLAUDE_WRAPPER_PROFILE
// periscope doesn't recognize) fall back to the raw id rather than being
// dropped — an unnamed chip still beats silently reading as a normal pane,
// which is the one wrong answer.
export function profileLabel(id) {
  return PROFILES.find((p) => p.id === id)?.label || id;
}
