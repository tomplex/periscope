# Spec — multi-account capacity pooling

## Problem

One Claude subscription, repeatedly exhausted at the weekly limit, with no
higher tier left to buy (account A is `default_claude_max_20x`). The only
remaining lever is a second subscription — but every Claude launch periscope
makes resolves to one credential, and `usage.py:148` reads exactly one keychain
item, so a second account is invisible to the dashboard even when it exists.

The naive fix (a second `~/.claude` with the config copied in) is rejected: it
forks skills, agents, commands, plugins, prompts, memory and history into two
diverging trees.

## Goal

Run panes on either of two subscription accounts from one periscope, with:

- **No config replication.** One canonical `~/.claude`.
- **No read-path changes.** Transcript view, narrator, `/history`, burn
  attribution and session status work for panes on either account.
- Per-account usage visible in the dashboard.
- Account survives a reboot — a pane must never silently resume on the wrong
  subscription.

## Verified mechanism

Observed on this machine (2026-07-30), not inferred. Recorded because they
contradict the prevailing public advice:

| Claim | Evidence |
|---|---|
| `CLAUDE_CONFIG_DIR` works on macOS | alt dir populated with `.claude.json`, `projects/`, `sessions/`, `backups/` |
| It relocates `.claude.json` too | file appears inside the alt dir, not left at `~` |
| Interactive auth works under an alt config dir | `/login` completed; interactive turn ran; transcript written |
| Credentials are isolated per config dir | alt-dir login left the shared keychain item's `mdat` untouched |
| Two different accounts coexist | distinct `accountUuid`/`emailAddress`/`organizationUuid`; both authenticate back to back |
| Credentials are keyed to the config dir **path** | `mv` of the dir orphaned the credential → "Not logged in" |
| Symlinked `projects/` + `sessions/` are written **through** | shared tree 1620 → 1621 transcripts; symlinks not replaced |
| `CLAUDE_CODE_HOST_CREDS_FILE` is not an account selector | redirected to an absent path, still authenticated off the keychain |
| `CLAUDE_CODE_OAUTH_TOKEN` does not outrank the keychain | an invalid value still authenticated |
| A zsh `VAR=x fn` prefix does not leak into the caller | `zsh -c 'f(){}; FOO=bar f; echo ${FOO:-unset}'` → `unset` |

Two corollaries drive the design: credentials are per-config-dir (so two
accounts never contend), and directory symlinks are traversed for writes (so
data stays unified while identity splits).

## Shape

### The second config dir

`~/.claude-b` is a **thin shell**. Only identity and settings are real:

```
~/.claude-b/
  .claude.json         real   — account B identity
  settings.json        real   — MUST carry the hook registrations (see below)
  projects   -> ~/.claude/projects     symlink
  sessions   -> ~/.claude/sessions     symlink
  skills, agents, commands, plugins, prompts, tools, … -> ~/.claude/*
```

Because `projects/`+`sessions/` resolve to the shared tree, every read path
works untouched — `usage.py:41`, `activity.py:789`, `turns.py:30`,
`session_status.py:27`, `history/backfill.py:17`. No read path calls
`.resolve()`/`realpath`/`st_ino` on a Claude path, so the alt dir is never even
observed. **No multi-root refactor.**

The path is load-bearing: the credential binds to it, so `~/.claude-b` can never
move without a re-login.

### settings.json is not merely a copy — it is the hook producer

`bin/periscope` (`install-claude-hook` / `uninstall-claude-hook`) registers
`pane_session_hook.py` on `SessionStart`/`UserPromptSubmit`. It does **not**
register anything on `SessionEnd` — the `history hook` entry present in this
machine's settings was added by another route (`history/README.md`), so a second
config dir does not inherit it and `/history` will not index account-B sessions
until it is added there by hand. Under `CLAUDE_CONFIG_DIR=~/.claude-b`, Claude
reads `~/.claude-b/settings.json`.

A copy that omits those hooks writes no `pane_sessions` row for any B pane, and
everything downstream goes dark: the transcript view (`turns.py`), the narrator
(which has **no cwd fallback** by design), per-pane burn, and the resurrect
`--resume` rewrite. Both installers therefore fan out over every account config
dir, skipping ones absent on this machine. A hand-copied settings.json would
otherwise silently drift on the next `bin/periscope install`.

### Account binding: key on the Claude session id

Not on tmux pane id. `activity.py:605` `prune_pane_status` filters by the live
pane set, and tmux reassigns `%0, %1, …` from zero after a server restart — so a
restored pane landing on a recycled id **inherits the previous pane's row**,
including its account. Pane ids are *reused*, which is worse than lost: it
silently binds a pane to the wrong subscription.

`@periscope_id` (`pids.py`) is the safer of the two — `uuid4().hex[:8]` is never
reused, and a failed rebind yields a fresh unknown pid (fail-open to the default
account) rather than a stale foreign one. But the durable key is the Claude
**session id** (`pane_sessions`, `activity.py:45-49`): never reused, and already
what `--resume` restores by. Pane id remains the live index into it.

### One command-builder

`config.py:63` `build_agent_command(agent, *, cwd, resume_id)` already exists as
a typed argv builder and already handles a second agent (`codex`, for which the
account concept is N/A). **Extend it with an account parameter** rather than
introducing a parallel builder.

| Site | Current | Notes |
|---|---|---|
| `worktree_spawn.py:270-271` | `build_agent_command` | already routed |
| `routes/sessions.py:182` `_window_new_plain` | `build_agent_command` | primary `+ New tab` path |
| `routes/sessions.py:95` `_window_new_resume` | rebuilds from `CLAUDE_EXEC` | **discards its own `exec_cmd` arg** on the create-session branch, silently overriding `channels.py`'s fully-formed command |
| `channels.py` `spawn_claude` | raw `CLAUDE_EXEC` (`config.py:43`) | |
| `channels.py` `resume_session` | raw `CLAUDE_EXEC` | feeds `_window_new_resume` above |
| `bg_commander.py:149` | `claude_bin()` + `_dispatch_env` (`:157`) | headless `--bg -p`; `_dispatch_env` strips `ANTHROPIC_*` **so it bills the subscription** — its account must be chosen explicitly |

`bg_commander` is the easiest site (a `subprocess` with an explicit env dict) and
the only one that is not a shell seam.

### Passing the account: `-e`, not a string prefix

`tmux new-window -e VAR=val` (tmux 3.6a) sets the pane's process environment, so
the binding survives the user manually re-running `claude` in that pane — a
string prefix does not. No launch site uses `-e` today; all of them
`send-keys` a command string.

The string prefix (`CLAUDE_CONFIG_DIR=… claude …`) remains valid where a command
string is the only artifact: the command palette, and the resurrect save file.

### Restore survives reboot — implemented

resurrect captures each pane's command from **ps argv**
(`tmux-resurrect/scripts/save.sh` → `save_command_strategies/ps.sh`), and a
shell-level `VAR=x cmd` prefix is consumed by the shell and never reaches argv.
The config dir is therefore absent from every captured line, and without
intervention every account-B pane restores onto the default account.

`resurrect.py` re-**emits** the prefix (generation, not preservation), reading
each live pane's actual `CLAUDE_CONFIG_DIR` from its running process
environment — correct only at save time, which is when the module runs. Two
non-obvious constraints:

- `ps eww` concatenates command and environment with no delimiter. Searching the
  whole string matches any process whose *command* merely mentions the variable
  (observed: a grep pattern was read as a value). Only the tail after the
  command — obtained without `e` for the same pid — is searched.
- The prefix must be emitted even when no `pane_sessions` row resolves. Losing
  `--resume` is acceptable; restoring onto the wrong subscription is not.

This requires `@resurrect-processes '~claude'` (substring) in tmux.conf — a bare
`'claude'` anchors at the start of the command, so a prefixed line matches
nothing and the pane restores as a bare shell. The option must also be live in
the running server (`tmux source-file`), not merely in the file.

### Usage

`usage.py:148` runs `security find-generic-password -s "Claude Code-credentials" -w`
— one service name, no account filter, returning a single arbitrary match. With
a second path-namespaced item present, the pill can report **the wrong
account's** meters. `usage_samples` (`activity.py:77`, PK `(meter, at)`) has no
account column, so both accounts interleave into one series with no retroactive
way to separate them.

Add the `account` column **before** any second account is metered, or the
history is poisoned. `UsagePill.jsx` grows a second meter set.

The JSONL fallback (`usage.py:44`) sums `glob("*/*.jsonl")` across the now-shared
tree and therefore aggregates both accounts into one number. It is the
dashboard's fallback whenever the OAuth fetch fails.

## Risks

- **Path immutability.** Credentials bind to the config dir path; any future
  reorganization costs a re-login and orphans a keychain item.
- **Unsupported surface.** Undocumented; a Claude Code release could change
  credential scoping. The failure mode must be "B panes fail to authenticate",
  never "panes silently run on A".
- **Shared-cwd transcript mis-attribution.** `activity.py:817`
  `live_transcript_for` returns newest-mtime-in-encoded-cwd, the documented
  fallback when a pane has no `pane_sessions` row. With both accounts writing one
  tree, an A pane and a B pane in the same directory are indistinguishable. If
  the hook registration above is missed, this fallback becomes the *normal* path
  for B panes rather than the exception.
- **`/history` has no account attribution.** Both accounts index into one DB.
  Acceptable, arguably desirable; noted so it is not a surprise.

## Open questions

- **Where account B's credential physically lives.** Not `.credentials.json`,
  not `.claude.json`, not the shared keychain item — by elimination a
  path-namespaced keychain item whose name is unidentified. Blocks per-account
  usage, and until resolved the pill may silently show the wrong account.
- **Does `settings.json` tolerate being a symlink?** Untested; Claude Code
  rewrites it, and atomic write-then-rename replaces a file symlink. Real copy
  until tested — with the hook-drift caveat above.

## Not doing

- **N accounts.** Exactly 2. The registry is a list, so widening is mechanical.
- **The blocked-pane handoff** (surface "pane blocked → move it"). It is the only
  feature needing *both* accounts' usage to be real, which the keychain question
  blocks. `resume_session` already covers the manual path. Revisit once
  per-account usage works.
- **Autonomous migration** of a blocked pane. Explicitly rejected.
- **Automatic account selection / load balancing.** The user picks.
- **Multi-root read paths.** Made unnecessary by the symlink shape.
- **Second OS user / VM isolation.** Rejected: file ownership across repos and
  worktrees, and a second tmux server to reach.
