# Setting up a second Claude account for periscope

**Audience: Claude, executing on the coworker's machine.** macOS only. Steps are
in dependency order — don't reorder. Two steps need the human (a browser login,
and a shell-rc edit); everything else you can run.

## What you're building

A second Claude Code subscription that periscope can launch panes on, with
**one canonical config tree**. `~/.claude-b` is a thin shell: only identity and
settings are real files, everything else symlinks back to `~/.claude`.

```
~/.claude-b/
  .claude.json          real     — account B identity + user-level MCP servers
  settings.json         real     — MUST carry the periscope hooks
  settings.local.json   real
  projects  -> ~/.claude/projects     symlink   (shared transcripts)
  sessions  -> ~/.claude/sessions     symlink   (shared session status)
  skills, agents, commands, plugins, prompts, hooks, tools, CLAUDE.md, … -> ~/.claude/*
```

Four mechanisms this rests on, all verified on Tom's machine (2026-07-30), not
inferred from docs — they contradict most public advice, so don't "fix" them:

| Fact | Consequence |
|---|---|
| `CLAUDE_CONFIG_DIR` relocates `.claude.json` **into** the alt dir | identity splits cleanly; nothing is left at `~` |
| Credentials are isolated per config dir, in a keychain item namespaced by `sha256(path)[:8]` | the two accounts never contend; usage is readable per account with no discovery step |
| Directory symlinks are written **through** (not replaced) | skills/plugins/history stay unified while identity splits |
| The credential binds to the config dir **path** | `~/.claude-b` can never move without a re-login |

## Preconditions

- periscope installed and running (`bin/periscope status`) from a git checkout.
- A second Claude subscription — its own email/account — and the human available
  to complete an OAuth browser flow with it.
- tmux new enough for `new-window -e VAR=val` (verified on 3.6a). This is how the
  account binds to a pane's process env, so it survives the user re-running
  `claude` in that pane.

## Step 1 — build the shell

**The path must be exactly `~/.claude-b`.** It is hardcoded in
`periscope/store.py` (`_DEFAULT_ACCOUNTS`) and in `bin/periscope`
(`install-claude-hook`). A different path means periscope cannot see the account
at all.

```sh
set -euo pipefail
A="$HOME/.claude"; B="$HOME/.claude-b"
mkdir -p "$B"
# Per-account real files; everything else in ~/.claude gets symlinked.
KEEP_REAL=".claude.json settings.json settings.local.json mcp-needs-auth-cache.json"
for entry in "$A"/*; do
  name=$(basename "$entry")
  case " $KEEP_REAL " in *" $name "*) continue ;; esac
  case "$name" in *.bak*|*.tmp) continue ;; esac
  [ -e "$B/$name" ] && continue
  ln -s "$entry" "$B/$name"
done
ls -la "$B"
```

Idempotent — re-run it after adding a new top-level dir to `~/.claude`, or that
dir will be missing (not broken, just absent) for account B.

`projects/` and `sessions/` being symlinks is load-bearing, not convenience:
every periscope read path (`turns.py`, `session_status.py`, `usage.py`,
`activity.py`, `history/backfill.py`) resolves them without ever calling
`realpath`, so nothing needs a multi-root refactor. Don't exclude them.

## Step 2 — copy settings, don't symlink them

```sh
cp ~/.claude/settings.json ~/.claude-b/settings.json
[ -f ~/.claude/settings.local.json ] && cp ~/.claude/settings.local.json ~/.claude-b/settings.local.json
```

Real copies because Claude Code rewrites `settings.json` with an atomic
write-then-rename, which **replaces** a file symlink. (Whether it tolerates being
a symlink is untested — don't find out on a working setup.)

The copy is also what carries the hooks and plugin toggles. Consequence worth
telling the human: `enabledPlugins`, `model`, and `permissions` are now
per-account. Enabling a plugin on A does not enable it on B. Tom uses this
deliberately (different default model per account); it is still drift.

## Step 3 — log in as the second account (human)

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude-b" claude
```

Then `/login` and complete the browser flow **with the second account's
credentials**. Verify the identity actually split:

```sh
python3 -c "
import json
for p in ['$HOME/.claude.json','$HOME/.claude-b/.claude.json']:
    print(p, json.load(open(p))['oauthAccount']['emailAddress'])
"
```

Two different emails, or stop — everything downstream assumes the split.

## Step 4 — re-add the user-level MCP servers

`.claude.json` holds the account identity *and* `mcpServers`, so it cannot be
symlinked — a fresh account B starts with **no** user-level MCP servers, silently
(the tools just aren't there, with no error). Plugin-provided servers are
unaffected; `plugins/` is symlinked.

```sh
python3 - <<'PY'
import json, os
a = os.path.expanduser("~/.claude.json"); b = os.path.expanduser("~/.claude-b/.claude.json")
da, db = json.load(open(a)), json.load(open(b))
db["mcpServers"] = da.get("mcpServers", {})
json.dump(db, open(b, "w"), indent=2)
print("copied:", list(db["mcpServers"]))
PY
```

This **re-diverges** every time a server is added to either account — on Tom's
machine the two already disagree about how periscope's own shim is invoked
(`python3 channel_shim.py` vs `uv run --script channel_shim.py`). Re-run this
after adding any user-level MCP server.

## Step 5 — install periscope's hooks into both dirs

Run **after** step 1 — the installer skips config dirs that don't exist yet.

```sh
cd <periscope checkout> && bin/periscope install-hook
```

Claude reads hooks from whichever `CLAUDE_CONFIG_DIR` it launched under, so a
B pane whose `settings.json` lacks `pane_session_hook.py` writes no
`pane_sessions` row — and the transcript view, the narrator (which has **no cwd
fallback**, by design), per-pane burn, and resurrect's `--resume` rewrite all go
dark for that pane, with no error anywhere. That is why the installer fans out
over every account dir instead of just `~/.claude`.

If the human uses periscope's `/history` index, its `SessionEnd` hook is
registered by `history/README.md`, not by periscope's installer — the step-2 copy
carries it over. Confirm it survived:

```sh
python3 -c "
import json
d = json.load(open('$HOME/.claude-b/settings.json'))
for ev, gs in (d.get('hooks') or {}).items():
    for g in gs:
        for h in g.get('hooks', []): print(ev, '->', h['command'])
"
```

Expect `pane_session_hook.py` on both `SessionStart` and `UserPromptSubmit`.

## Step 6 — a shell entry point (human edits their rc)

```zsh
claude-b() { CLAUDE_CONFIG_DIR="$HOME/.claude-b" claude "$@"; }
```

A zsh `VAR=x fn` prefix binds exactly that invocation and does not leak into the
caller (`zsh -c 'f(){}; FOO=bar f; echo ${FOO:-unset}'` → `unset`), so this is
safe to define globally. It also composes with an existing `claude` wrapper
function — the prefix exports to children.

Because `projects/` is shared, this is also how you **move an existing session
onto account B**: `claude-b --resume <uuid>`, or `claude-b -c` for the most
recent session in the current directory. Account A's transcripts are visible and
resumable from B.

## Step 7 — make the binding survive a reboot

tmux-resurrect captures each pane's command from `ps` argv, and a shell-level
`VAR=x cmd` prefix is consumed by the shell and never reaches argv — so without
this, **every account-B pane restores onto account A**. `periscope/resurrect.py`
re-emits the prefix at save time by reading each live pane's real process env,
which requires:

```sh
tmux show -gv @resurrect-processes    # want: ~claude
```

The leading `~` means substring match. A bare `'claude'` anchors at the start of
the command, so a prefixed line matches nothing and the pane restores as a bare
shell. `bin/periscope install` writes this into the periscope-owned tmux.conf;
if the value is wrong or empty, run `bin/periscope install` and re-check — the
option must be live in the **running** server, not merely correct in the file.

## Verify

| Check | Command | Expect |
|---|---|---|
| Alt dir authenticates | `CLAUDE_CONFIG_DIR=~/.claude-b claude -p "reply OK"` | `OK` |
| Distinct accounts | the step-3 python | two different emails |
| Credential isolated | `security find-generic-password -s "Claude Code-credentials-$(printf %s "$HOME/.claude-b" \| shasum -a 256 \| cut -c1-8)" -w >/dev/null && echo found` | `found` |
| Config shared | `readlink ~/.claude-b/skills ~/.claude-b/projects` | paths under `~/.claude` |
| Hooks registered | the step-5 python | `pane_session_hook.py` ×2 |
| periscope sees both | dashboard header usage pill | two meter sets, A and B |
| Launch works | launcher → Account picker → **B** → New tab | rail row chips the pane as B |
| Reads work for B | that pane's Transcript tab | renders its conversation |

In the dashboard: the header's **spawn-account pin** sets the standing default
for every spawn path (launcher, unified open, MCP `spawn_claude`); the launcher's
Account picker overrides one launch; a rail row's move-account action resumes a
pane onto the other subscription.

## Gotchas to hand the human

- **`~/.claude-b` is immutable.** The credential binds to the path. Renaming or
  moving it orphans the keychain item and yields "Not logged in".
- **Two files drift by construction**: `.claude.json` (MCP servers) and
  `settings.json` (plugins, model, permissions). Re-sync deliberately; nothing
  warns you.
- **`/history` has no account attribution** — both accounts index into one DB.
  Arguably desirable; just not a surprise.
- **This is an unsupported surface.** A Claude Code release could change
  credential scoping. The acceptable failure mode is "B panes fail to
  authenticate"; the unacceptable one is "panes silently run on A". If auth
  breaks after an upgrade, check the keychain item name before anything else.
