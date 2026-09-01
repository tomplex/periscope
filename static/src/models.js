// Model overrides a Claude pane can be spawned with. "default" means no
// override — the pane runs whatever the account's settings.json says; any
// other id is set as ANTHROPIC_MODEL on the pane's tmux window.
//
// Two surfaces share this list: the header's spawn-model pin (a server
// setting, the standing default for EVERY spawn path incl. MCP spawn_claude)
// and the launcher's per-launch picker, which seeds from the pin and sends its
// value explicitly — "default" included — so one launch can opt out of the pin.
//
// Aliases, not full ids: Claude resolves 'fable' / 'opus' / 'sonnet' to the
// latest of each family, so the list doesn't rot when a model version ships.
// The server accepts any model-id-shaped string (config.model_env), so a full
// id typed into prefs by hand also works — the picker just doesn't offer one.
//
// The `[1m]` suffix is Claude's extended-context variant, and it composes with
// the alias — `opus[1m]` is the latest Opus at a 1M window, still version-free.
// It is not the default: a bare `opus` runs the standard window.
//
// Orthogonal to the account (which subscription bills) and the profile (which
// plugin set runs). Whether a target carries the model at all is the same
// guard as the profile — `sendsProfile` in profiles.js: only a Claude agent
// window, never a shell (a hand-typed `claude` there would silently run on the
// override) and never Codex.
export const MODELS = [
  { id: "default", label: "default" },
  { id: "fable", label: "fable" },
  { id: "opus[1m]", label: "opus 1m" },
  { id: "sonnet", label: "sonnet" },
];
