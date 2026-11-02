# Fewer forks per refresh

`build_state` — the body of every dashboard refresh, run by the state hub about once a second and by any REST
caller — measures its own cost, forks two processes per refresh instead of six, keeps git off the request path, and
stops paying a keychain read for every `gh` call. THI-450.

## What was measured (2026-11-02, prod, 11 Claude panes, load avg ~10)

- **~200 forks in 30 s** sampled from the server's process tree (a lower bound; short-lived ones slip between
  samples): `ps` ~73, `git` ~60, `tmux` ~27, `security` 12, `gh` ~8, plus `ssh -G`, `claude agents --json`, the
  tmux-continuum save.
- **Every refresh, caches warm:** 6 fixed forks — `tmux list-windows`, `tmux list-panes -a -F '#{pane_id}'`
  (`panes.all_pane_ids`), `tmux list-panes -a -F '#{pane_id}\t#{pane_pid}'` (`session_status._tmux_pane_pids`, bare
  `tmux` that ignores `PERISCOPE_TMUX_SOCKET`), and three full process tables in three formats
  (`session_status.proc_table`, `session_status._claude_procs`, `agent_processes.process_snapshot`) — plus one
  `capture-pane` per working or freshly-output pane. About 9 per refresh, up to 17.
- **Every ~15 s:** `git_pr.cached_git_state` runs 7 git commands per pane cwd, serially on the request path, and
  every cwd was warmed in the same refresh so they all expire together: ~77 serial git forks in one refresh.
- **Under load:** `session_status`'s 1 s caches have no lock. A refresh slower than 1 s lets each of the fan-out's
  up-to-32 threads re-fork `tmux list-panes` and two `ps` in parallel.
- **`gh`:** 11 calls in 20 s (per-branch `gh pr list --head`, `gh pr view` for linked PRs, the reviews queue's
  `gh pr list --state open`). gh's keyring on macOS execs `/usr/bin/security` to read its token on every call, and gh
  spawns `gh send-telemetry`. The plan-usage keychain reads (`usage._read_oauth_token`) are correctly rate-limited
  (per account per 300 s) and are not the anomaly.

## Scope

In: A (measure), B (fixed forks 6 → 2), C (git off the request path), D (`gh` without keychain reads).
Out, its own ticket: `capture-pane` through the existing control-mode client instead of a fork per pane (touches the
mirror code and `docs/invariants.md` #2–#7); batching the per-branch `gh pr list --head` calls into one query per
repo (decide from A's numbers).

## Decisions

**D1 — Forks are counted by a Python audit hook.** `sys.addaudithook` on the `subprocess.Popen` event (which
`subprocess.run`, `check_output` and `Popen` all raise) counts every fork the server makes, by command (`argv[0]`
basename plus the first subcommand, e.g. `git status`, `tmux list-panes`), in a rolling 60 s window. No call site
changes, and background threads count too. Installed once in the app's lifespan; the hook returns at once for every
other audit event.

**D2 — Each refresh reports its cost.** `build_state` records its duration; the state hub records the gap between
broadcasts (what a dashboard actually sees). `/api/state` carries `refresh: {build_s, build_max_s, gap_s,
forks_per_min, top_forks}` (last build, max over 60 s, last gap, total and the five largest commands over 60 s). The
resource popover (THI-448) shows one line: `refresh 0.4 s · every 1.1 s · 140 forks/min`, with the top commands in
its tooltip. A build slower than 3 s logs a warning naming its top forkers.

**D3 — One tmux call per refresh.** A single `tmux list-panes -a -F <window fields + pane fields + pane_active>`
through `_tmux_argv` (so it honours `PERISCOPE_TMUX_SOCKET`), cached for 1 s behind a lock. From it:
`list_windows` keeps its exact row shape, built from each window's active pane; `all_pane_ids` and
`session_status._tmux_pane_pids` read every pane. Invariants kept: focus from `#{window_active}`, never
`#{window_activity}` (#1); `#{window_id}` and `#{@periscope_id}` carried for stamping by window id (#11); the full
window roster reaches `_attach_git_then_resolve_pids` (#13, #19); `all_pane_ids` still includes a split window's
background panes, and its caller still skips GC on an empty result.

**D4 — One process table per refresh.** `agent_processes.process_snapshot()` (pid, ppid, start time, state, comm,
args; locked, 1 s) becomes the only `ps` table. `session_status.proc_table` and `_claude_procs` read it instead of
forking their own (`ps -axo pid=,ppid=,command=` and `ps -axo pid=,etime=,comm=`); a process's age comes from its
start time. The 15 s `ps eww` environment scan stays: process environment has no other source. Both snapshots are
locked, so the fan-out can no longer stampede them.

**D5 — Git: two commands per refresh, refreshed in the background.** `git_state_for` runs
`git --no-optional-locks status --porcelain=v2 --branch --untracked-files=normal` (branch, detached sha, ahead count
from `# branch.ab`, untracked entries) and `git diff HEAD --shortstat` (line counts) instead of seven commands.
`--no-optional-locks` keeps the status call from taking the index lock under the user's own git commands. The git
dir, common dir (`resolve_repo`) and remote URL (`github_slug`) are cached per cwd for 10 minutes. The output shape
(`branch`, `git`, `repo_slug`, `repo_key`, `repo_label`) is unchanged.

`cached_git_state` becomes stale-while-revalidate: it returns the cached entry and, when it is stale, starts a
deduplicated background refresh through `_bg`; each entry's next refresh is 15 s ± 5 s (jittered, so cwds warmed
together don't expire together). A cwd seen for the first time is still filled synchronously: the pid resolver
matches panes on `(branch, cwd)` after a tmux restart (`_attach_git_then_resolve_pids`), so `branch` must not be
missing on first sight. The cache gets a lock.

**D6 — `gh` reads its token once, not per call.** One helper builds the environment for every `gh` periscope runs
(`git_pr`, `reviews`, `cleanup`, `projects`): `GH_TOKEN` from `gh auth token` (read once, cached for an hour,
re-read after an auth failure), `GH_TELEMETRY=false` and `GH_NO_UPDATE_NOTIFIER=1`. gh reads `GH_TOKEN` before its
stored credentials, so no call execs `security`, and none spawns `gh send-telemetry`. If `gh auth token` fails, the
calls run without `GH_TOKEN` as today.

## Changes

- `periscope/forks.py` (new): the audit hook, the rolling counter, and `refresh` stats (D1, D2).
- `periscope/app.py`: installs the hook in the lifespan.
- `periscope/routes/state.py`, `periscope/state_hub.py`: build duration, broadcast gap, the `refresh` block (D2).
- `periscope/panes.py` (+ `tmux.py` if the format lives there): the one list-panes snapshot; `list_windows` and
  `all_pane_ids` read it (D3).
- `periscope/session_status.py`: `_tmux_pane_pids`, `proc_table`, `_claude_procs` read the shared snapshots (D3, D4).
- `periscope/git_pr.py`, `periscope/gitutil.py`: the two-command refresh, background refresh, jitter, lock, the
  10-minute repo-identity cache (D5).
- `periscope/gh.py` (new, or a function in `gitutil.py` if that reads better): the `gh` environment (D6); every `gh`
  call site uses it.
- `static/src/chrome/ResourcePill.jsx`, `static/src/chrome/resources.js`: the refresh line (D2).
- `docs/invariants.md`: a line under #1 / #11 that the window rows now come from one `list-panes` snapshot.

## Testing

- `tests/test_forks.py`: the hook counts `subprocess.run` / `Popen` by command; the window rolls; other audit events
  are ignored.
- `tests/test_panes.py`: windows derived from a list-panes fixture match today's `list_windows` rows exactly (fields,
  order, active-pane selection, split windows); `all_pane_ids` includes background panes; an empty tmux answer.
- `tests/test_session_status.py`: `proc_table` and `_claude_procs` built from a `process_snapshot` fixture match
  today's outputs (age from start time).
- `tests/test_git_pr.py`: the porcelain-v2 parse against fixtures for clean, dirty, untracked-only, detached, ahead,
  no-upstream and non-repo; stale-while-revalidate (stale returns cached and refreshes once; first sight fills
  synchronously; jittered expiry); one fork pair per refresh.
- `tests/test_gh.py` (or alongside): the environment carries `GH_TOKEN`/`GH_TELEMETRY`/`GH_NO_UPDATE_NOTIFIER`; the
  token is reused within the hour and re-read after an auth failure; no token → calls still run.
- vitest: the refresh line's formatting.
- Before/after on prod-like load: the `refresh` block's `forks_per_min`, `build_s` and `gap_s` from a dev server on
  :17375 against the same panes, recorded in the PR.

## Assumptions

- `git status --porcelain=v2 --untracked-files=normal` counts untracked entries the same way as `ls-files -o
  --exclude-standard --directory --no-empty-directory` (a wholly-untracked directory is one entry). The tests compare
  both on fixture repos.
- The short sha for a detached HEAD is the first 7 characters of `# branch.oid` (today: `rev-parse --short`, which
  can be longer when 7 is ambiguous).
- A cwd's branch shown on the dashboard can lag a checkout by up to ~20 s (the stale entry is served while the
  background refresh runs); pid rebinding uses the same, possibly stale, branch.
- `gh auth token` prints the token gh would use for github.com; a token revoked within the hour fails one call, which
  triggers the re-read.
- The audit hook's per-event cost (a string compare) is negligible against periscope's file I/O.
