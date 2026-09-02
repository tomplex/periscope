# Wrapper profiles (normal | lab)

`claude` on this machine is a **zsh function**, not the binary
(`~/.claude/bin/claude-wrapper.zsh`). It injects a system prompt and, given
`lab`, swaps the plugin set. Periscope spawns are `send-keys` into an
interactive shell, so **every periscope-spawned pane already goes through that
wrapper** — this is why a spawned pane has Tom's system prompt at all.

The launcher's Profile picker (sticky, `prefs.ui.launch_profile`) sends
`profile=lab` to `/api/window/new`, which sets `CLAUDE_WRAPPER_PROFILE=lab` on
the new tmux window via `tmux -e`.

**The profile is carried as an env var, never as the `claude lab` argv word.**
The wrapper *accepts* that word, but consumes it and execs `command claude
--settings '{...plugins...}'` — so `lab` never reaches claude's argv, and
nothing downstream could observe it. Two consumers need to:
`session_status.pane_profiles` (the rail chip) and `resurrect._rewrite_line`
(re-emitting the prefix so a lab pane survives a reboot on the lab plugin set).
Detecting it from argv instead would mean fingerprinting the wrapper's exact
plugin JSON. Env is the one carrier all three parties read — the same reason
`CLAUDE_CONFIG_DIR` works this way.

Consequences worth knowing:

- **The account and the profile are orthogonal.** Account = which subscription
  bills (`CLAUDE_CONFIG_DIR`); profile = which plugin set runs. Both ride
  `tmux.env_args`, both get scrubbed off the session by `scrub_session_env`,
  both get re-emitted by resurrect. A lab pane on account B is normal.
- **`session_status` caches the raw env TAIL, not parsed values**
  (`_pane_claude_envs`). One `ps eww` fork serves every per-pane variable; a
  per-variable cache would fork once per variable on the `/api/state` hot path.
- **Only agent windows carry it** (`profiles.sendsProfile`, mirroring
  `sendsAccount`). A shell window that inherited it would put a hand-typed
  `claude` on the lab plugin set invisibly — the chip is derived from a live
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
is not remembered. `store.spawn_model_env(explicit)` is the one choke point:
an explicit value wins, INCLUDING an explicit `"default"` (the launcher always
sends one — that is how a single launch opts out of the pin); `None` falls to
the pin. The value is set on the new window via `tmux.env_args` — the third
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
