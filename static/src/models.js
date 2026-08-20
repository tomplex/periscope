// Model overrides the launcher can spawn a Claude pane with. "default" sends
// nothing and the pane runs whatever the account's settings.json says; any
// other id is set as ANTHROPIC_MODEL on the pane's tmux window.
//
// Aliases, not full ids: Claude resolves 'fable' / 'opus' / 'sonnet' to the
// latest of each family, so the list doesn't rot when a model version ships.
// The server accepts any model-id-shaped string (config.model_env), so a full
// id typed into prefs by hand also works — the picker just doesn't offer one.
//
// Orthogonal to the account (which subscription bills) and the profile (which
// plugin set runs). Whether a target carries the model at all is the same
// guard as the profile — `sendsProfile` in profiles.js: only a Claude agent
// window, never a shell (a hand-typed `claude` there would silently run on the
// override) and never Codex.
export const MODELS = [
  { id: "default", label: "default" },
  { id: "fable", label: "fable" },
  { id: "opus", label: "opus" },
  { id: "sonnet", label: "sonnet" },
];

// model id → the `model` query param, or null to omit it entirely. Omitting
// keeps the default launch byte-identical to the pre-models URL.
// Pure: exported for unit tests.
export function modelQuery(m) {
  return !m || m === "default" ? null : m;
}
