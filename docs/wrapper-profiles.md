# Wrapper profiles (default | full)

`claude` on this machine is a **zsh function**, not the binary
(`~/.claude/bin/claude-wrapper.zsh`). It injects a system prompt and, given
`full`, adds the pipeline direction prompt (`tom-full.md`) and turns on the
pipeline plugin set (superpowers, tom-workflow, codebase-health-check). The
default profile is `tom-core` plus the mode overlay, on whatever plugins
settings.json enables. Periscope spawns are `send-keys` into an interactive
shell, so **every periscope-spawned pane already goes through that wrapper** —
this is why a spawned pane has Tom's system prompt at all.

The launcher's Profile picker (sticky, `prefs.ui.launch_profile`) sends
`profile=full` to `/api/window/new`, which sets `CLAUDE_WRAPPER_PROFILE=full`
on the new tmux window via `tmux -e`. `spawn_claude` and unified open do not
carry a profile; they get the default, which is the point of it being the
default.

**The profile is carried as an env var, never as the `claude full` argv word.**
The wrapper *accepts* that word, but consumes it and execs `command claude
--settings '{...plugins...}'` — so `full` never reaches claude's argv, and
nothing downstream could observe it. Two consumers need to:
`session_status.pane_profiles` (the rail chip) and `resurrect._rewrite_line`
(re-emitting the prefix so a full pane survives a reboot on the full plugin set).
Detecting it from argv instead would mean fingerprinting the wrapper's exact
plugin JSON. Env is the one carrier all three parties read — the same reason
`CLAUDE_CONFIG_DIR` works this way.

Consequences worth knowing:

- **The account and the profile are orthogonal.** Account = which subscription
  bills (`CLAUDE_CONFIG_DIR`); profile = which plugin set runs. Both ride
  `tmux.env_args`, both get scrubbed off the session by `scrub_session_env`,
  both get re-emitted by resurrect. A full pane on account B is normal.
- **`session_status` caches the raw env TAIL, not parsed values**
  (`_pane_claude_envs`). One `ps eww` fork serves every per-pane variable; a
  per-variable cache would fork once per variable on the `/api/state` hot path.
- **Only agent windows carry it** (`profiles.sendsProfile`, mirroring
  `sendsAccount`). A shell window that inherited it would put a hand-typed
  `claude` on the full plugin set invisibly — the chip is derived from a live
  claude process, which a shell window has none of.
- **Editing the wrapper is part of this feature.** Periscope sets the var; the
  wrapper is what honours it. It fails safe: an un-updated wrapper ignores the
  var and yields a normal pane, never a wrong-plugin-set one.

**Model override (`ANTHROPIC_MODEL`) rides the same carrier.** Two surfaces,
shaped like the account pin: the header's **spawn-model pin**
(`settings.spawn_model`, a SERVER setting so MCP `spawn_claude` honours it
too; rides `/api/state` as `spawn_model`) is the standing default for every
spawn path — launcher New Tab, unified open, `spawn_claude` — and the
launcher's Model picker is a per-launch override that seeds from the pin and
is not remembered. `usage.choose_launch(account, model)` is the one choke
point on both axes (docs/account-routing.md): an explicit value wins,
INCLUDING an explicit `"default"` (the launcher always sends one — that is
how a single launch opts out of the pin); `None` falls to the pin, and a pin
of `"auto"` (or none) lets `launch_policy` pick fable, then opus[1m]. The
value is set on the new window via `tmux.env_args` — the third
env binding beside the account and profile, scrubbed off the session by
`scrub_session_env` like the other two; on the unified-open two-window layout
only the agent window carries it. Claude reads it with precedence `--model` >
`ANTHROPIC_MODEL` > `settings.json`. `config.model_env` checks only the
character set (it lands in a `-e` arg and a shell prefix); the alias list is
not validated because it moves. Registry: `static/src/models.js`. Two things
differ from the profile:

- **There is no model chip.** The rail already shows the model parsed off
  Claude's status line (`panes.STATUS_RE` → `w.model`), and that is the truth —
  the env var is the SPAWN-TIME choice, which an in-session `/model` leaves
  untouched. `session_status.pane_models` exists for resurrect only.
- **Resurrect re-emits the prefix ONLY on the session-lost path.** `--resume`
  restores a session's own model unless `ANTHROPIC_MODEL` is set at launch, so
  a prefix on a resumed pane would clobber a later `/model` switch with the
  spawn-time choice. With no session to resume, the spawn-time choice is all
  that is left of the intent, so it is kept there.

**Effort is the one launch choice carried as a flag, not env.** The header's
**spawn-effort pin** (`settings.spawn_effort`; rides `/api/state` as
`spawn_effort`) is `"default"` (unset: no flag, Claude picks per model and
settings.json) or one of `config.EFFORT_LEVELS` (low, medium, high, xhigh,
max). `usage.choose_launch` copies it onto `Launch.effort`, and the three
spawn paths put it on the agent's own command line through
`config.build_agent_command(..., effort=)` as `--effort <level>`. The
launcher's Effort picker is the per-launch override, shaped like its Model
picker (seeds from `launch_default.effort`, always sends a value, `default`
is the opt-out, not remembered). Dashboard resume doesn't apply it.

Why a flag, against this page's env-only rule: Claude's env var
`CLAUDE_CODE_EFFORT_LEVEL` makes it **refuse `/effort` for the whole
session** ("CLAUDE_CODE_EFFORT_LEVEL=… overrides this session — clear it and …
takes over"). The pin shipped on that var first, and it left every
periscope-launched pane unable to change effort, including through the
per-pane control. `--effort` only seeds the session's starting level (it
lands in Claude's initial app state, not in env), so `/effort` keeps working.
What the flag costs, and how each is handled:

- **Attribution reads argv, not env.** `session_status.pane_efforts` parses
  `--effort` off the live claude's command line. It's the LAUNCH level, like
  `pane_models`; `/effort` leaves argv alone.
- **Resurrect treats it like the model.** On the session-lost path the
  captured command keeps the flag. On `--resume` the
  command is rebuilt from scratch and the flag is dropped, because
  re-emitting the launch level after a reboot would undo a later `/effort`.
- **Only the agent window carries it.** It's on the agent's command line, so
  the sibling shell of the two-window layout never sees it. Codex ignores it.
- **`config.effort_level` validates against the closed list**, not a
  character set. The levels don't move the way model aliases do. An unknown
  value (a stale pin) fails open to the default, and the closed list is what
  makes it safe to splice unquoted into `spawn_claude`'s command line.

**Per-pane model/effort (the pane header's pickers).** The header pins and
the launcher only shape NEW panes. A running session is changed from its own
header: `SessionPickers.jsx` → `POST /api/pane/session-setting?pid&setting=
model|effort&value`, which types `/model <x>` or `/effort <x>` into the pane
through `/api/send`'s paste-then-Enter path (`_send_to_target`). Rules:

- **It refuses (409) unless the pane is a Claude that is idle with an empty
  input box**, read from a fresh capture, not the poll. Keystrokes into a
  busy pane land mid-turn, and into a half-typed message they are appended
  to the draft and submitted with it. The session status file decides
  idle-ness when the pane maps to a live session; the scrape decides
  otherwise. The client disables the pickers on the same conditions from
  the poll (`sessionPickers.sessionBlocker`) so they read honestly.
- **Values.** Effort's `default` is typed as `/effort auto` (the model's own
  level). The model takes any alias Claude does, `default` included,
  checked by `config.model_env`'s character set like the pin. `auto` is the
  pin's chooser value and is rejected here.
- **Typing is not taking, so the pane is read back** (`_command_outcome`,
  ~3s): Claude's own line under the echoed command decides. "Set … to" means
  `applied`. "Not applied" / "overrides this session" means Claude refused,
  which returns 409 with Claude's words. "Switch model?" means `confirm`:
  in a session with history Claude asks before switching, because the whole
  conversation is re-read at the new model's cost. periscope never answers
  that for the user; the picker toasts "answer it in the pane". Silence
  means `unverified`.
- **The value is remembered only when applied** (`windows[pid].session_model`
  / `session_effort` = `{value, claude_pid}`), because Claude reports effort
  nowhere periscope can read. `window_view` shows it only while that claude
  pid is still the pane's claude, so a fresh session in the same pane falls
  back to its launch values (`launch_effort`, read off argv). The model
  picker prefers the status line's model whenever it parses.
- **Side effect worth knowing: both commands also save a default.**
  `/effort <level>` writes `modelSettings.<model>.effortLevel` and `/model
  <x>` writes `model` in the account's `settings.json` ("saved as your
  default for new sessions"; verified on Claude Code 2.1.280). periscope
  spawns aren't affected while their pin is set (`--effort` and
  `ANTHROPIC_MODEL` both beat settings), but a hand-launched `claude`, or a
  spawn with the pin on `default`, starts at whatever was last picked in
  any pane. Nothing typed at the prompt selects Claude's session-only
  variant.
