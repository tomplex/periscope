# Session recovery: resume specific Claudes after reboot

**Status:** design
**Date:** 2026-06-05

## Problem

The machine already restores its tmux workspace after a reboot:
`tmux-resurrect` + `tmux-continuum` are wired up (`@continuum-restore 'on'`,
`@resurrect-capture-pane-contents 'on'`, `@resurrect-processes 'claude'`), so
windows, panes, cwds, and pane contents all come back, and resurrect re-runs the
command each pane was running.

The one thing that does **not** come back is the *conversation*. Each Claude pane
relaunches as a **fresh** `claude` session instead of resuming the session it had
before the reboot. The user wants periscope to make the restore resume each pane's
specific Claude session (`claude --resume <uuid>`), fully automatically on boot.

## Why it's broken today

A post-save-layout hook already exists for this
(`~/.tmux/resurrect-claude-hook.sh`, referenced by
`@resurrect-hook-post-save-layout`). Its job is to rewrite the `claude` entries in
each resurrect save file to include `--resume <uuid>`. It is currently a **complete
no-op**, for two independent reasons, plus a third latent defect:

1. **Broken claude detection.** The hook only rewrites a pane line when its
   `pane_current_command` field equals `claude`:
   ```bash
   if [[ "$line" == pane${d}* && "$line" =~ ${d}claude${d}:claude ]]; then
   ```
   Claude Code now reports `pane_current_command` as its **version string**
   (`2.1.139`, `2.1.150`, …), confirmed against live panes. Save-file lines look
   like `…\t2.1.150\t:claude --system-prompt…`, so `\tclaude\t:claude` never
   matches. Nothing is ever rewritten.

2. **Wrong session map.** The hook resolves UUIDs from
   `/tmp/claude-sessions/pid-<os_pid>` — an orphaned legacy map with no writer
   found anywhere in `~/.claude`, `~/dev/periscope`, or `~/.tmux*`. Periscope's
   own authoritative, `/clear`-surviving map is the **`pane_sessions` table in
   `periscope.db`** (`~/.config/periscope/periscope.db`), keyed by tmux pane id,
   written by `pane_session_hook.py` (which opens its own stdlib `sqlite3`
   connection on Claude's `SessionStart`/`UserPromptSubmit`). The older
   `~/.config/periscope/pane_sessions/<pane_id>` *directory* is itself legacy —
   migrated into the table by `activity.migrate_legacy_pane_sessions()` and now
   stale.

3. **Latent: channel flags dropped.** Even when it matched, the rewrite
   `sed`-replaced everything after `:claude` with `--resume <uuid>`, discarding the
   `--dangerously-load-development-channels server:periscope` (and `server:lgtm`)
   flags — so a resumed Claude would come back disconnected from periscope's
   channel.

## Why rewrite-at-save-time (not server-driven resume)

The natural alternative — have the periscope server `send-keys`
`claude --resume …` into restored panes after boot — founders on one hard fact:
**tmux pane ids do not survive a reboot.** The tmux server restarts and reassigns
pane ids from `%0`, so every `pane_sessions/%56` file points at a dead id, and
periscope cannot tell which *new* pane should resume which session. The only
identifier stable across a reboot is the pane's **`session:window.pane` position**
— which resurrect itself relies on (its restored pane-content files are named
`pane-<session>:<window>.<pane>`).

The resurrect save file already pairs each pane with its position **and** the
command it was running. That makes the save file the one artifact that already
contains everything needed, surviving the reboot intact. Correcting the command
string inside it is strictly less work than reproducing that snapshot in periscope
and reimplementing resurrect's restore-launch path via keystrokes. We keep
resurrect's clean, ordered, content-restoring relaunch and only fix *what command*
it relaunches.

The rewrite must run at **save** time (while panes are live and the
pane→session map is valid), not restore time (panes are dead, the map is gone).
The existing post-save-layout hook is exactly that timing, and running after
continuum's own write avoids racing it.

## Design

### Trigger (no new tmux config)

The existing line in `~/.tmux.conf`:

```
set -g @resurrect-hook-post-save-layout '~/.tmux/resurrect-claude-hook.sh'
```

repoints to the periscope subcommand:

```
set -g @resurrect-hook-post-save-layout '<periscope-checkout>/bin/periscope resurrect-rewrite'
```

`@resurrect-processes 'claude'` stays on — resurrect still relaunches the command
at restore; we only fix the command text. No other tmux config changes. Nothing is
written into any watched project repo: code lives in the periscope repo, the
trigger is in `~/.tmux.conf`, runtime state stays under `~/.config/periscope/` and
resurrect's own save dir.

### `periscope/resurrect.py`

**Import discipline.** Stdlib-only at runtime, so the hook runs as
`python3 -m periscope.resurrect <savefile>` with no `uv`/dependency-resolution
cost on continuum's ~10-minute cadence. It may import **only**
`periscope.config` (a stdlib-only leaf module — `os`, `pathlib`) plus the stdlib
(`sqlite3`, `re`, `subprocess`, `os`, `pathlib`, `tempfile`). It must **not**
import `periscope.activity` (whose own imports pull in `git_pr`, `panes`, and
`rename_ai` → the Anthropic SDK and other non-stdlib deps), which would break the
plain-`python3` invocation. The module therefore opens its **own** read
connection to the session DB — the same out-of-process pattern
`pane_session_hook.py` already uses as the writer.

Public surface:

```python
def rewrite_save_file(save_path: Path) -> int:
    """Rewrite claude pane lines in a resurrect save file to resume their
    specific session. Returns the number of lines rewritten."""
```

with a `__main__` that takes the save-file path as argv[1], calls
`rewrite_save_file`, and exits 0 unconditionally (a hook must never fail a
continuum save).

**Parse.** Each pane line is tab-delimited. The resurrect pane format places:
field 0 = `pane`, field 1 = session, field 2 = window index, field 5 = pane index,
field 9 = `pane_current_command`, field 10 = `:<full command>`. Lines that are not
pane lines (e.g. `window`, `state`) and panes whose full command does not start
with `claude` pass through unchanged.

**Detect claude by the full-command field** (`claude` as the first token of field
10, after stripping its leading `:`), *not* `pane_current_command`. This is what
fixes defect 1 and is robust to future `pane_current_command` changes.

**Resolve the session uuid.**
- Build the key `<session>:<window_index>.<pane_index>` from the save-file fields.
- Map that key to the live tmux pane id via
  `tmux list-panes -a -F '#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}'`.
  (Pane indices are 1-based on this host — `pane-base-index 1` — matching the save
  file; the lookup keys off whatever the save file recorded, so no assumption is
  baked in.)
- Read the uuid from the `pane_sessions` table in `config.ACTIVITY_DB`
  (`~/.config/periscope/periscope.db`) via a stdlib `sqlite3` read:
  `SELECT session_id FROM pane_sessions WHERE pane_id=?`. The connection is opened
  read-only and closed per invocation (the hook is short-lived). This mirrors
  `pane_session_hook.py`'s own direct-connection pattern rather than importing the
  server's `activity` module.

**Reconstruct, preserving channels.** From the captured full command, regex out
every `--dangerously-load-development-channels <value>` flag (in original order),
discard the stale `--system-prompt …` value and any pre-existing `--resume …`, and
emit:

```
claude --resume <uuid> --dangerously-load-development-channels server:periscope [--dangerously-load-development-channels server:lgtm] …
```

Extracting only the channel flags sidesteps parsing the multi-kilobyte,
`\012`-escaped system-prompt value entirely. Replace field 10 with
`:` + the reconstructed command; rejoin the line with tabs.

**Skip safely.** If the pane has no resolvable uuid (no live pane id for the
position, or no `pane_sessions` row for that pane id), leave the line
**unchanged** — resurrect restores it as a fresh `claude`, never a broken pane.
Count it as skipped.

**Write atomically** (tmpfile + `os.replace`), matching the existing hook.

**Idempotent.** Re-running on an already-rewritten file strips the existing
`--resume` and re-injects the current uuid, so repeated saves converge.

### `bin/periscope resurrect-rewrite <savefile>`

A thin subcommand alongside the existing ones (`install-hook`, etc.) that execs
`python3 -m periscope.resurrect "$1"` from the checkout. No new dependencies.

## Testing

`tests/test_resurrect.py`, following the package's one-test-per-module mirror.
Monkeypatch the `tmux list-panes` call and point `config.ACTIVITY_DB` at a temp
SQLite DB seeded with a `pane_sessions` table (`pane_id → session_id` rows).
Fixture save-file lines:

- A claude pane with `--system-prompt` + both `server:periscope` and `server:lgtm`
  channels → rewritten to `claude --resume <uuid>` with **both** channel flags
  preserved in order and the system-prompt dropped.
- An `nvim` pane line and a `zsh` pane line → untouched.
- A claude pane whose position has no `pane_sessions` entry → untouched.
- A `window`/`state` (non-pane) line → untouched.
- Idempotency: feeding the rewritten output back in produces the same result.

This is the regression guard for the next time Claude's TUI/process format shifts
— the same class of breakage that just silently disabled the old hook.

## Out of scope / follow-ups

- **Orphaned `/tmp/claude-sessions` writer.** No producer was found in the obvious
  locations, yet files are still appearing with recent mtimes. Tracking down and
  removing that writer (and the `/tmp/claude-sessions` dir) is a separate cleanup.
- **Deleting `~/.tmux/resurrect-claude-hook.sh`.** Superseded once the subcommand
  is verified working; remove after a confirmed reboot-resume.
- **`bin/periscope install` managing the tmux.conf line.** The repoint is a
  one-line manual edit for now; folding it into `install`/`install-hook` is
  optional and deferred.
