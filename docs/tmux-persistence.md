# tmux persistence (`periscope/resurrect.py`)

Session survival across reboots is tmux-resurrect + tmux-continuum, with two
periscope hooks into it.

**Save-file rewrite (`python -m periscope.resurrect <file>`).** Registered as
`@resurrect-hook-post-save-layout` via `bin/periscope resurrect-rewrite`.
Resurrect re-runs each Claude pane's captured command, but that command starts
a *fresh* session — no `--resume <uuid>`. This rewrites each Claude pane's
command in the just-written save file. It must run at SAVE time while panes are
live: the pane→session map is keyed by tmux pane id, and pane ids are reassigned
when the server restarts, so at restore time there is nothing left to map.
Import discipline: stdlib + `periscope.config` only — it runs under plain
`python3`, and `periscope.activity` would drag in the Anthropic SDK.

**A captured `--session-id` restores as `--resume`.** A Claude that periscope
starts with a prompt (`channels.spawn_claude_with_prompt`) gets
`--session-id <uuid>`, and Claude refuses to start twice with one ID ("Session
ID … is already in use"). A pane with no `pane_sessions` row keeps its
captured command, so without this it would come back with no Claude. The
rewrite reads the flag as `--resume <uuid>` everywhere it reads a captured
`--resume`: the duplicate grouping, the row-less line and the demoted line.

**One `--resume` per session per save.** If several pane lines would resume
the same session (by `pane_sessions` row, or captured argv — a restored
zombie's argv already says `--resume`), only one keeps it: the pane whose live
claude reports that session now, then the one that touched its session file
last (the same file `live_session_id_for_pane` reads), then the active pane,
then the first line. Keepers are picked BEFORE rendering, and the rest render
once in the session-lost shape — env prefixes including the spawn-time
`ANTHROPIC_MODEL`, the captured command including `--effort`, its own
`--resume` stripped. (Stripping `--resume` off an already-rendered resume line
instead lost the model and effort, and counted the line twice.)
Two Claudes on one session both write its transcript; this stops a
duplicated layout (see the plugin rule below) from putting several Claudes on
one session. It removes no pane: every pane line is still restored, so if the
racing restores come back the window still gains a pane every restart (each
now a fresh Claude). Only the declared-once rule, with a single TPM run line,
prevents the layout growth.

**Periscope drives the periodic save (`resurrect.save_now()`).**
tmux-continuum has NO timer: its save fires purely as a side effect of
`status-right` being *expanded*, which only happens when a status line is drawn
for a client. Every client periscope attaches is control-mode (the pane mirror
and the input client) and control-mode clients render no status line — so on a
host driven entirely through the dashboard, continuum silently never saves.
Observed: a 24-day gap between saves, after which a reboot restored a 24-day-old
layout. The activity worker now calls `save_now()` each tick; the script
self-gates on `@continuum-save-interval` and takes its own lock, so calling it
every 30s only writes when an interval has actually elapsed. Prod-only, and it
degrades silently when continuum isn't installed.

**`bin/periscope install` provisions the tmux side too (`install-tmux`,
`periscope/tmux_persist.py`).** It clones TPM if absent, runs TPM's
non-interactive `bin/install_plugins` for resurrect + continuum, writes a
periscope-owned `~/.config/periscope/tmux.conf` (an `@plugin` line for each of
the three not declared elsewhere — see below — `@continuum-restore on`,
`@resurrect-processes '~claude'`, and `@resurrect-hook-post-save-layout`
pointing at THIS checkout), and adds one `source-file` line to the user conf
TPM reads (the XDG one when it exists, else `~/.tmux.conf` — see below),
directly above the line that runs TPM (its `run ... tpm/tpm`, or a
`source-file` that leads to one), else at the end — followed by TPM's run line
only when nothing tmux executes runs TPM (see the run rule below). The owned
file is regenerated wholesale on every `install`/`update` (never merged);
`uninstall-tmux` removes the line (from every user conf, see below) and the
file and leaves the plugins.

The verb plans its conf edits in memory and counts their result before writing
any: when tmux would still run TPM more than once, or the count can't be
finished (a `$VAR` nothing here can expand, below), it writes nothing (no
file, no line), says what it would have changed, and exits 1 — nothing it adds
brings a doubled run back to one, and its source line would switch continuum
on, to run twice. (Checked only after writing, a symlinked pair got the run
line, TPM went from 0 to 2 runs, and install exited 1 having done it.) Past
that check it prints each change as it lands — every line it removed or added
— and fails loudly, exit 1, on any fault: TPM reports a failed clone only as
text, so it checks the three plugin directories itself (a silently missing
plugin is a silently missing save); a step that fails once the confs are
rewritten (the TPM clone, `install_plugins`, the re-source) is reported with
the tool's own stderr, where it used to be a bare traceback with the edits
unprinted; and it re-counts: tmux must run TPM exactly once and TPM must see
each plugin declared once. Every conf shape install misjudged before that
check ended at 0 or 2 runs while it exited 0; it never removes a line of the
user's to pass. A running server is re-sourced so the hook path is live
immediately (a failed re-source still gets the counts reported);
continuum's auto-restore is gated on server start time, so that never restores
over it.

**Every plugin is declared exactly once across what TPM reads.** TPM does not
dedupe `@plugin` lines: a user who already declares tpm/resurrect/continuum in
`~/.tmux.conf` and also sources the owned file gets each plugin's `*.tmux` run
twice — and continuum, run twice, starts TWO background restores at server
start. They race through the same save file; one that sees "pane missing,
window present" while the other is mid-create splits the window again, leaving
an extra shell pane. Resume a Claude in it and the next save records it, so
the window gains a pane every restart (observed 2026-10: one window at three
panes, all `--resume`-ing one session, two of them zombie Claudes). So
`install` omits from the owned file any plugin declared elsewhere, and healthz
reports `plugin_declarations` (each must be 1). "What TPM reads" is resolved
exactly as TPM resolves it (`scripts/helpers/plugin_functions.sh`), because a
file read differently is a declaration miscounted: the user conf is
`${XDG_CONFIG_HOME:-~/.config}/tmux/tmux.conf` when it exists (the `~/.config`
fallback is the launchd job's case: it has no `XDG_CONFIG_HOME`), else
`~/.tmux.conf` (TPM reads one, never both — so that is also where the source
line goes, and an `@plugin` added to `~/.tmux.conf` next to an XDG conf is
never installed or run); a
`source-file` line's paths are word-split, a leading `~` or `$HOME` expanded,
and each word globbed. A literal-path read missed `source-file $HOME/…` and
`…/*.conf`, so install declared continuum a second time; whether a conf
already sources the owned file is resolved the same way (a first-word match
missed a glob covering it, and install added a second source line), while
uninstall removes only periscope's own single-path line. The omission is a
snapshot of the other files at install, so it drifts when they are edited:
drop a declaration the owned file deferred to and the plugin is declared
nowhere (no auto-restore after the next reboot, silently); add one and it is
declared twice. The prod worker therefore calls `tmux_persist.heal` each tick,
which regenerates the owned file whenever a count is off 1 (never creating it,
never re-sourcing — plugins load at server start). Each omitted plugin leaves a
comment naming the file that declares it. When a count is off 1, install's
error lists each declaring `file:line` and the source line that brought that
file in (or, at 0, the files TPM reads). Check by hand:
`bash -c 'source ~/.tmux/plugins/tpm/scripts/helpers/plugin_functions.sh; tpm_plugins_list_helper' | sort | uniq -c`.

**TPM runs exactly once at server start.** Each `run ... tpm/tpm` sources
every plugin, so two run continuum twice exactly as a doubled `@plugin` does —
with every declaration still at 1 — and with none, no plugin loads and nothing
restores. tmux is not TPM: it EXECUTES every user conf that exists, and follows
`source-file` to any depth. So `tpm_runs` counts over what tmux executes, never
through TPM's reader:

- The files tmux loads at start: the live server's `#{config_files}` when one
  answers — INSTEAD of the default list, not added to it (a `-f` server ran
  only its file) — else tmux's default list: `/etc/tmux.conf`, `~/.tmux.conf`,
  `$XDG_CONFIG_HOME/tmux/tmux.conf` (when set), `~/.config/tmux/tmux.conf`. tmux 3.6a
  drops only a repeat of the same path STRING, so `~/.tmux.conf` symlinked to
  the XDG conf runs twice (a realpath dedupe counted it once and healthz said
  ok). `#{config_files}` lists just these top-level files, unresolved and
  missing ones included (Homebrew's system conf is
  `/opt/homebrew/etc/tmux.conf`, not TPM's `/etc`) — never what they source.
- Each `source-file` followed where tmux runs it, to tmux's nesting limit (a
  conf that sources itself runs 51 times): `-n` only parses; `-F` expands
  `#{current_file}` / `#{d:current_file}`; `~` and `$VAR` expand except in
  single quotes (`source-file '~/x'` runs nothing, though TPM's reader expands
  it); each path is globbed, one matching nothing skipped. A one-level read
  missed a run line two sources deep or behind `-F '#{d:current_file}/…'`:
  install appended a second one, and healthz counted that one alone.
- `$VAR` is what tmux will expand it to: periscope's own environment when it
  has the variable (install run from a shell), else the running server's
  global one (`tmux show-environment -g VAR`), which tmux expands from. A
  dashboard update runs `install-tmux` under launchd, whose environment has no
  `XDG_CONFIG_HOME`: `source-file $XDG_CONFIG_HOME/…` named no file, the run
  line behind it went uncounted, and every update appended a second one
  (exit 0, healthz 1, TPM run twice). Neither has it (no server, or one
  without it): the file is unknown and the count only a floor — install
  appends no run line, writes nothing, and names the line; healthz reports
  `tpm_runs: null`, `ok` false.

Install's run line follows from that count, not from the conf it edits: it
appends `run '~/.tmux/plugins/tpm/tpm'` only when nothing tmux executes runs
TPM — asked even when the conf TPM reads already sources the owned file. Every
install before XDG support appended its source line plus that run line to
`~/.tmux.conf`; for a user with an XDG conf, tmux ran TPM from both files. So
from every user conf but TPM's, install removes exactly two kinds of line:
periscope's own source line (the owned file by its path, alone on the line),
and a line reading verbatim `run '~/.tmux/plugins/tpm/tpm'` directly under
one. It can't tell whose that run line is, and it is often the user's: the
same installs put the source line directly ABOVE a TPM user's existing run
line, so a user whose `~/.tmux.conf` ended in TPM's canonical line sees it
reported as removed. Nothing else there is touched — a run line spelled
otherwise (`run -b …`), or not directly under periscope's source line, stays
and counts. `uninstall-tmux` strips only the source line, from all of them.
Moving the pair can take the only run line away — the XDG conf of a user who
had sourced the owned file there by hand then ran TPM nowhere (no plugins, no
restore, and re-running install didn't repair it) — so the count, taken after
the move, puts the run line at the end of the conf TPM reads: a removed run
line that was the only one is moved there, not lost. A run line that stays
counts, so install adds none (asked of the XDG conf alone, it used to append a
second one beside a user's `run -b`). Run from
`~/.tmux.conf` before the XDG conf sources the owned file, it still works —
continuum read `@continuum-restore on` in 10 of 10 real server starts with TPM
run first, blocking or `-b`, its restore being a background script that reads
the option after tmux has carried on through the conf. One file under two
paths (a symlinked pair) is the conf TPM reads, never moved out of itself;
tmux runs any run line in it twice, so install writes nothing there and fails
naming both paths (delete the link: tmux ≥ 3.1 reads the XDG path itself).
Every run-count message names each run line once with each way tmux reached
it — `~/.tmux.conf:2 ×2 (loaded at start; sourced by
~/.config/tmux/tmux.conf:2)` — so the source line that reaches a file a second
time is named, and the symlink clause is said only of one file run under two
path strings (said of any line run twice, it sent a user whose XDG conf
sources `~/.tmux.conf` hunting for a link that wasn't there).

`GET /api/healthz` carries a `resurrect` block built from the LIVE tmux
server's options (not the conf file — the file being right and the server
having sourced it are different facts): plugin dirs, `conf_sourced`,
`restore_on`, `processes_ok`, `hook_current` (live hook == this checkout), and
`last_save_at` / `last_save_stale` (older than 2× the live save interval; a
missing save is not stale, a fresh install hasn't had one yet), and
`plugin_declarations` and `tpm_runs` (both above: declarations over what TPM
reads, runs over what tmux executes). `ok` is the conjunction. `last_save_at`
is the newer of `last`'s target and the resurrect directory's mtime: a save
that matches `last` is deleted (`save.sh` `files_differ`), so `last` alone
dates the last layout CHANGE, and an idle night read as a stalled save while
continuum saved every interval. Kept or deleted, the save changed the
directory.
`python3 -m periscope.tmux_persist status <repo>` prints the same without a
server round-trip.

Diagnosing a stale restore: `ls -lt ~/.local/share/tmux/resurrect/` — gaps in
that timeline are the story, unless the layout didn't change (resurrect keeps
no copy of a save identical to `last`; `ls -ld` on the directory and
`tmux show -gv @continuum-save-last-timestamp` date the last save that ran).
`tmux list-clients -F "#{client_flags}"` tells you whether anything is drawing
a status line.

Reproducing a conf shape against real tmux: a temp `HOME`, a fake
`~/.tmux/plugins/tpm/tpm` that logs each run, and the server on its own `-L`
socket. Point `TMUX_TMPDIR` at a directory that EXISTS — tmux 3.6a falls back
to `/tmp/tmux-$UID/default`, the user's own server, when it names a missing
one, and `install`'s re-source then executes the temp confs there. Unset
`TMUX` and `TMUX_PLUGIN_MANAGER_PATH` too: a shell in a pane inherits both, and
TPM loads whatever plugin dir the latter names.
