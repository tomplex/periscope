# Spec — multi-account capacity pooling

## Problem

One Claude subscription, repeatedly exhausted at the weekly limit, with no
higher tier left to buy (account A is already `default_claude_max_20x`). The
only remaining lever is a second subscription — but every Claude Code launch
periscope makes is hardcoded to one credential, and `usage.py` reads exactly one
keychain item, so a second account is invisible to the dashboard even if it
exists.

The naive fix (a second `~/.claude` with the config copied into it) is rejected:
it forks skills, agents, commands, plugins, prompts, memory, and history into
two diverging trees.

## Goal

Run panes on either of two subscription accounts from one periscope, with:

- **No config replication.** One canonical `~/.claude`.
- **No read-path changes.** Transcript view, narrator, `/history`, burn
  attribution and session status keep working for panes on either account.
- Per-account usage visible in the dashboard.
- An assisted (never autonomous) way to move a limit-blocked pane to the other
  account.

## Verified mechanism

All of the following were observed on this machine (2026-07-30), not inferred.
They contradict the prevailing public advice, so they are recorded here rather
than left implicit:

| Claim | Evidence |
|---|---|
| `CLAUDE_CONFIG_DIR` works on macOS | alt dir populated with `.claude.json`, `projects/`, `sessions/`, `backups/` |
| It relocates `.claude.json` too | file appears *inside* the alt dir, not left at `~` |
| Interactive auth works under an alt config dir | `/login` completed; interactive turn ran; transcript written |
| Credentials are isolated per config dir | alt-dir login left the shared keychain item's `mdat` untouched |
| Two *different* accounts coexist | distinct `accountUuid` / `emailAddress` / `organizationUuid`; both authenticate back to back |
| Credentials are keyed to the config dir **path** | `mv` of the dir orphaned the credential → "Not logged in" |
| Symlinked `projects/` + `sessions/` are written **through** | shared tree 1620 → 1621 transcripts; symlinks not replaced |
| `CLAUDE_CODE_HOST_CREDS_FILE` is **not** an account selector | redirecting it to an absent path still authenticated off the keychain |
| `CLAUDE_CODE_OAUTH_TOKEN` does not outrank the keychain | an invalid value still authenticated (ignored, or silent fallback — undetermined) |

Two corollaries drive the whole design: credentials are per-config-dir (so two
accounts never contend), and directory symlinks are traversed for writes (so
data can stay unified while identity is split).

## Shape

### The second config dir

`~/.claude-b` is a **thin shell**. Only identity is real; everything else points
back at the canonical tree:

```
~/.claude-b/
  .claude.json         real   — account B identity (oauthAccount)
  settings.json        real   — see open question below
  projects   -> ~/.claude/projects     symlink  (transcripts stay unified)
  sessions   -> ~/.claude/sessions     symlink
  skills, agents, commands, plugins, prompts, tools, file-history, … -> ~/.claude/*
```

Because `projects/` and `sessions/` resolve to the shared tree, every periscope
read path keeps working untouched — `usage.py:41`, `activity.py:774`,
`turns.py:30`, `session_status.py:27`, `history/backfill.py:17`. **No multi-root
refactor.** This is the single biggest cost this design avoids.

The path is load-bearing: it is baked into the credential binding, so
`~/.claude-b` can never move without a re-login.

### Account registry

Two accounts, in `state.json` (`store.py`). Not N — see "Not doing".

```
accounts: [
  {id: "a", label: "...", config_dir: null},          # null = default ~/.claude
  {id: "b", label: "...", config_dir: "~/.claude-b"},
]
```

`config_dir` is the primary key in practice, since that is what the credential
is bound to.

### One command-builder

Today five sites construct the Claude command independently, and three bypass
the `claude_exec()` accessor (`config.py:52`) by importing the raw `CLAUDE_EXEC`
constant (`config.py:41`):

| Site | Current |
|---|---|
| `worktree_spawn.py:262-268` | `claude_exec()` → `send-keys` |
| `channels.py:616-618` (`spawn_claude`) | raw `CLAUDE_EXEC` |
| `routes/sessions.py:274-278` (`/api/window/new`) | raw `CLAUDE_EXEC`, plus `--resume` variant |
| `channels.py:870-876` (`resume_session`) | raw `CLAUDE_EXEC` |
| `resurrect.py:127-131` | rebuilds a **bare** `claude --resume <uuid>` |

All five route through one builder that takes an account id and emits the
command with a `CLAUDE_CONFIG_DIR=<dir> ` prefix (omitted for the default
account, so account A's command string is byte-identical to today's).

Env cannot be passed any other way: no launch site uses `tmux new-window -e`,
and panes inherit the tmux server's environment rather than periscope's. The
prefix in the `send-keys` string is the only seam.

`store.py:310-317` seeds the command palette from `CLAUDE_EXEC`, and
`LauncherModal.jsx:147-162` sends a **client-supplied** `exec` string — so the
account must be a separate parameter on `/api/window/new`, never baked into the
palette entry, or saved palette entries would pin an account.

### Per-pane account attribute

`pane_status` (`activity.py:92-108`) is keyed by tmux pane id and already has
the guarded-ALTER idiom for adding columns (`activity.py:123-129`). Add
`account` there. `store.py`'s `WindowAnnotation` (`store.py:66-77`) is the
alternative, but it is keyed by `@periscope_id`, which is re-minted on rebind —
an account binding must not silently move panes between subscriptions.

### Restore survives reboot

`resurrect.py:115` matches only `["claude"]` as argv[0] and rebuilds a bare
command (`resurrect.py:127-131`). A prefixed command (`CLAUDE_CONFIG_DIR=… claude`)
therefore (a) fails that match and is left unrewritten, losing `--resume`, or
(b) if the match is widened, is rebuilt *without* the prefix — silently
restoring every account-B pane onto account A after a reboot. The rewriter must
preserve the account prefix the same way it already preserves
`--dangerously-load-development-channels` flags (`resurrect.py:38`).

### Usage

`usage.py:148-163` reads one fixed keychain item (`"Claude Code-credentials"`)
and `usage_samples` (`activity.py:77-83`) has no account column. Per-account
usage needs both widened: a token source per account, and `meter` rows tagged
by account. `UsagePill.jsx` renders one meter set today and grows a second.

### Blocked-pane handoff

Per the chosen UX (assisted, not autonomous): when a pane is limit-blocked and
the other account has headroom, surface it on the card with a "move" action.
The move is a `--resume <uuid>` spawn under the other account's config dir,
reusing `pane_sessions` (`activity.py:45-49`) for the session id. No autonomous
pane-killing on a heuristic.

## Risks

- **Prefix assignment leaking into the pane's shell.** `claude` on this machine
  is a zsh *function*, not a binary. For function calls, POSIX shells may
  persist a `VAR=x cmd` prefix assignment into the calling shell (bash does;
  zsh restores). If it leaks, subsequent manual `claude` invocations in that
  pane silently inherit the account — arguably desirable, but it must be a
  decision, not an accident. Verify under the real launch path.
- **Path immutability.** Credentials bind to the config dir path. Any future
  reorganization of `~/.claude-b` costs a re-login and orphans a keychain item.
- **Unsupported surface.** None of this is documented; a Claude Code release
  could change credential scoping and break account B with no warning. The
  failure mode should be "account B panes fail to authenticate", not "panes
  silently run on account A" — which is exactly what the resurrect gap above
  would cause.
- **Both accounts write one `projects/` tree.** Session files are UUID-named so
  collisions are implausible, but `/history` will index both accounts'
  transcripts into one DB with no account attribution. Acceptable (arguably
  desirable); noted so it is not discovered as a surprise.

## Open questions

- **Where account B's credential physically lives.** Not `.credentials.json`,
  not `.claude.json`, not the shared keychain item — by elimination a
  path-namespaced keychain item whose name was never identified. Blocks the
  per-account usage pill, since `usage.py` needs a token per account. Everything
  else in this spec is unblocked.
- **Does `settings.json` tolerate being a symlink?** Untested. Claude Code
  rewrites it, and atomic write-then-rename replaces a file symlink rather than
  following it. Defaulting to a real copy (the one genuinely duplicated file)
  until tested.
- Whether the `default_claude_ai` tier on account B changes the pooling
  arithmetic enough to matter. Account B is expected to be upgraded.

## Not doing

- **N accounts.** Exactly 2. The registry is a list, so widening later is
  mechanical, but no UI or scheduling is built for N.
- **Autonomous migration** of a blocked pane (kill + respawn under the other
  account). Explicitly rejected in favour of the assisted affordance.
- **Automatic account selection / load balancing.** The user picks; periscope
  surfaces headroom. Scheduling can come later once per-account usage is real.
- **Multi-root read paths.** Made unnecessary by the symlink shape. If the
  symlinks ever have to go, this becomes ~8 single-root globs turning into
  N-root globs — the cost this design exists to avoid.
- **Second OS user / VM isolation.** Rejected: file ownership across repos and
  worktrees, and a second tmux server for periscope to reach.
