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
- **A latent bug in the one process table we keep:** `process_snapshot` runs
  `ps -axo pid=,ppid=,lstart=,state=,comm=,args=`, and macOS cuts `comm` to 16 characters when it is not the last
  column — a claude launched as `/Users/tom/.local/bin/claude` reads `/Users/tom/.loca`, a codex launched as
  `/usr/local/bin/codex` reads `/usr/local/bin/c`. Its `lstart` also prints in the user's locale (`mer. 30 sept.`
  under fr_FR), which the row regex can't read, emptying the whole table.

## Scope

In: A (measure), B (fixed forks 6 → 2), C (git off the request path), D (`gh` without keychain reads), and the
process table's executable and locale fix that B depends on. That fix also resolves THI-453's main case (a codex
launched by absolute path never matched `_is_codex_executable`).
Out, its own ticket: `capture-pane` through the existing control-mode client instead of a fork per pane (touches the
mirror code and `docs/invariants.md` #2–#7); batching the per-branch `gh pr list --head` calls into one query per
repo (decide from A's numbers).

## Decisions

**D1 — Forks are counted by a Python audit hook.** `sys.addaudithook` on the `subprocess.Popen` event (which
`subprocess.run`, `check_output`, `Popen` and asyncio's subprocesses all raise) counts every fork the server makes,
by command (`argv[0]` basename plus the first word-like argument, skipping the values of `-C`/`-L`/`-S`/`-c`: `git
status`, `tmux list-panes`, `ps`, `ps eww`), in a rolling 60 s window. No call site changes, and background threads
count too. Installed once in the app's lifespan; the hook returns at once for every other audit event, and its whole
body is guarded — an exception escaping an audit hook aborts the Popen it audits. The hook can't see the processes
the server's children spawn (gh's `security` and `send-telemetry`); those are measured by sampling the process tree.

**D2 — Each refresh reports its cost.** `build_state` records its duration; the state hub records the gap between
broadcasts (what a dashboard actually sees) and forgets it when it goes idle, so the first push after nobody watched
doesn't report the idle stretch. `/api/state` carries `refresh: {build_s, build_max_s, gap_s, forks_per_min,
top_forks}` (last build, max over 60 s, last gap or null, total and the five largest commands over 60 s);
`/api/healthz` carries the full per-command `forks` map. The resource popover (THI-448) shows one line:
`refresh 0.4 s · every 1.1 s · 140 forks/min`, with the slowest build and the top commands in its tooltip — a
`refresh` signal in `store.js`, set by `poll.js`, read by `ResourcePill.jsx`, formatted by `resources.js`, styled in
`static/styles.css`. A build slower than 3 s logs a warning naming its top forkers, at most once a minute.

**D3 — One tmux call per refresh.** `tmux.list_panes()` runs one `tmux list-panes -a -F <window fields + pane fields
+ pane_active>` through `_tmux_argv` (so it honours `PERISCOPE_TMUX_SOCKET`), fields separated by `\x1f` (a tab in a
window name or path no longer shifts fields), and stores its rows as a snapshot; `tmux.recent_panes()` serves that
snapshot while it is under 1 s old, under a lock, else calls `list_panes`. The snapshot lives in `tmux.py` so
`session_status` keeps importing only leaf modules.
`list_windows` always calls `list_panes` — never the snapshot: `open_ops` reads a window it just created straight
back, and `pids` stamps `@periscope_id` with a bare `tmux set-option`, so a cached answer would show a just-stamped
window unstamped and re-mint it (#11, #13). It keeps its exact row shape, built from each window's active pane.
`all_pane_ids` and `session_status._tmux_pane_pids` read `recent_panes`, so a refresh forks tmux once, at its
`list_windows`. Invariants kept: focus from `#{window_active}`, never `#{window_activity}` (#1); `#{window_id}` and
`#{@periscope_id}` carried for stamping by window id (#11); the full window roster reaches
`_attach_git_then_resolve_pids` (#13, #19); `all_pane_ids` still includes a split window's background panes, and its
caller still skips GC on an empty result.

**D4 — One process table per refresh.** `agent_processes.process_snapshot()` (pid, ppid, start time, state, argv;
locked, 1 s) becomes the only `ps` table. It runs `ps -axo pid=,ppid=,lstart=,state=,args=` with `LC_ALL=C`, and
`ProcessInfo.exe` is argv[0] — the executable as launched — replacing the truncated `comm`, so every caller
(`_is_codex_executable`, `_claude_procs`) sees a full path. `session_status.proc_table` and `_claude_procs` read it
instead of forking their own (`ps -axo pid=,ppid=,command=` and `ps -axo pid=,etime=,comm=`); a process's age comes
from its start time, and `parse_etime` goes. The 15 s `ps eww` environment scan stays: process environment has no
other source. Both snapshots are locked, so the fan-out can no longer stampede them.

**D5 — Git: two commands per refresh, refreshed in the background.** `git_state_for` runs
`git --no-optional-locks status --porcelain=v2 --branch --untracked-files=normal` (branch, detached sha, ahead count
from `# branch.ab`, untracked entries) and `git --no-optional-locks diff-index -M --shortstat HEAD` (line counts)
instead of seven commands. `status` takes no lock under `--no-optional-locks`; the line counts come from
`diff-index`, which never writes the index (plain `diff HEAD` does, flag or not) — `-M` so a staged rename reads as
one, not a full delete+add. The status exit code is the repo check — `rev-parse --git-dir` goes. When status says the cwd has
no work tree (a bare-container layout whose `.git` file says `gitdir: ./.bare`, a cwd inside `.git`, a bare repo),
one `git rev-parse --abbrev-ref HEAD` gives the branch and the state reads `clean`. A repo with no commits reads its
branch from `# branch.head` (today's read returned `rev-parse`'s error text as the branch). The common dir
(`resolve_repo`) and remote URL (`github_slug`) are cached per cwd for 10 minutes in `git_pr` (gitutil's functions
stay live for their other callers). The output shape (`branch`, `git`, `repo_slug`, `repo_key`, `repo_label`) is
unchanged.

`cached_git_state` becomes stale-while-revalidate: it returns the cached entry and, when it is stale, starts a
deduplicated background refresh through `_bg`; each entry's next refresh is 15 s ± 5 s (jittered, so cwds warmed
together don't expire together). The background read gets a 10 s git timeout (it is off the request path; the
synchronous read keeps 3 s). A background read that comes back empty for a cwd that is still a directory keeps the
previous entry and retries at the next expiry — a timed-out status says nothing new. A path whose refresh can't start
or crashes is released, never left marked in flight. A cwd seen for the first time is still filled synchronously:
the pid resolver matches panes on `(branch, cwd)` after a tmux restart (`_attach_git_then_resolve_pids`), so `branch`
must not be missing on first sight. The cache gets a lock.

**D6 — `gh` reads its token once, not per call.** `gh.run(*args, cwd, timeout)` runs every `gh` periscope runs
(`git_pr`, `reviews`, `cleanup`, `projects`) with `GH_TOKEN` from `gh auth token`, `GH_TELEMETRY=false` and
`GH_NO_UPDATE_NOTIFIER=1`. gh reads `GH_TOKEN` before its stored credentials, so no call execs `security`, and none
spawns `gh send-telemetry`. The token is cached for 10 minutes (so `gh auth switch` takes effect soon); a failed
`gh auth token` is cached as "no token" for 60 s, and the calls run without `GH_TOKEN` as today. A 401 — `HTTP 401:
Bad credentials`, `failed to get runs: HTTP 401: …`, or the search API's `non-200 OK status code: 401 Unauthorized`
— re-reads the token, at most once per 60 s (a `*.ghe.com` remote, where `GH_TOKEN` also applies, would otherwise
re-read on every call). `tmux._run` takes an `env`.

## Changes

- `periscope/forks.py` (new): the audit hook, the rolling counter, build and gap timings, the `refresh` stats (D1,
  D2).
- `periscope/app.py`: installs the hook in the lifespan.
- `periscope/routes/state.py`, `periscope/state_hub.py`: build duration, broadcast gap reset on idle, the `refresh`
  block (D2). `periscope/routes/healthz.py`: the full `forks` map.
- `periscope/tmux.py`: `list_panes` / `recent_panes` and the `\x1f`-separated format (D3); `_run(env=)` (D6).
- `periscope/panes.py`: `list_windows` and `all_pane_ids` read them (D3).
- `periscope/agent_processes.py`: `args`-only `ps` under `LC_ALL=C`; `ProcessInfo.exe` from argv[0] (D4).
- `periscope/session_status.py`: `_tmux_pane_pids`, `proc_table`, `_claude_procs` read the shared snapshots;
  `parse_etime` deleted (D3, D4). `periscope/window_view.py`: a comment.
- `periscope/git_pr.py`: the two-command read, the work-tree-less fallback, the 10-minute identity cache, background
  refresh, jitter, lock, keep-on-failure (D5); `gh.run` at its call sites (D6).
- `periscope/gh.py` (new): the `gh` environment and token (D6); `reviews.py`, `cleanup.py`, `projects.py` call it.
- `static/src/store.js`, `static/src/poll.js`, `static/src/chrome/resources.js`,
  `static/src/chrome/ResourcePill.jsx`, `static/styles.css`, rebuilt `static/dist/`: the refresh line (D2).
- `tests/conftest.py`: seeds `tmux._panes_snapshot`, `agent_processes._snapshot_cache` and `gh._token_cache` with
  unexpiring empty entries (replacing the `_tmux_pane_pids` stub) and clears git_pr's per-path caches, so no test
  forks real tmux, ps or gh or starts a git refresh thread; tests of those paths reset the seed.
- `docs/invariants.md`: a line under #1 / #11 that the window rows come from one `list-panes`, and that only
  `all_pane_ids` and the pane → pid map read the shared snapshot. `docs/testing.md`: the conftest seeds. `CLAUDE.md`:
  `forks.py` and `gh.py` in the module table.

## Testing

- `tests/test_forks.py`: command keys; the hook counts real `subprocess.run` / `Popen`; other audit events are
  ignored; a failing body never raises; the window rolls; `refresh_stats`; the slow-build warning and its once-a-minute
  limit.
- `tests/test_state_hub.py`: gaps recorded between broadcasts, none across an idle stretch. `tests/routes/test_state.py`,
  `tests/routes/test_healthz.py`: the `refresh` block and the `forks` map.
- `tests/test_agent_processes.py`: fixtures rebuilt as real `ps … args=` rows; argv[0] as the executable (a codex by
  absolute path is found); `ps` runs with `LC_ALL=C`; a French `lstart` reads nothing; a live `ps` run on a process
  whose argv[0] is `/Users/tom/.local/bin/claude`, with `LC_ALL=fr_FR.UTF-8` in the environment, reads the full path.
- `tests/test_session_status.py`: `proc_table` and `_claude_procs` built from a `process_snapshot` fixture match
  today's outputs (age from start time; a `node …/.claude/plugins/…` process is not a claude); one `ps` serves the
  refresh; `_tmux_pane_pids` reads the snapshot and honours the socket.
- `tests/test_tmux.py`, `tests/test_panes.py`: windows derived from a list-panes fixture match today's
  `list_windows` rows exactly; a live tmux server checked against today's `list-windows` parser; `all_pane_ids`
  includes background panes; an empty tmux answer; one `list-panes` per refresh; `list_windows` never serves the
  snapshot; a tab in a name is kept; a newline-split row is skipped and logged once.
- `tests/test_git_pr.py`: porcelain-v2 fixtures for clean, dirty, untracked-only, detached, ahead, no-upstream and
  non-repo, each compared on a real repo with the seven-command read; a repo subdirectory (whole-repo untracked count,
  on purpose); unborn; bare container, cwd inside `.git`, plain bare repo; `--no-optional-locks` on both; two forks
  once identity is cached; stale-while-revalidate (stale returns cached and refreshes once; first sight fills
  synchronously; jittered expiry; longer background timeout; a failed background read keeps the entry; a vanished cwd
  clears; a refresh that can't start or raises releases the path).
- `tests/test_gh.py`: the environment carries `GH_TOKEN`/`GH_TELEMETRY`/`GH_NO_UPDATE_NOTIFIER`; the token is reused
  for 10 minutes; a failed read is retried after 60 s; each 401 shape re-reads, at most once per 60 s; other failures
  keep the token; no token → calls still run; no `["gh", …]` argv outside `gh.py`.
- vitest: the refresh line's formatting and its render.
- Before/after: interleaved runs (before, after, before, after) of a dev server on :17375 against the same panes —
  `refresh` and the full `forks` map from the server, plus a process-tree sampler for gh's children (`security`,
  `send-telemetry`), recorded in the PR. A dev server (`is_prod()` false) skips the narrator, the session poke and the
  MCP listener, so its absolute numbers are below prod's; the comparison is between the two runs.

## Assumptions

- `git status --porcelain=v2 --untracked-files=normal` counts untracked entries the same way as `ls-files -o
  --exclude-standard --directory --no-empty-directory` run from the repo root (a wholly-untracked directory is one
  entry; ignored-only and empty directories are none). The tests compare both on fixture repos.
- From a repo subdirectory, the old `ls-files -o` counted only that subdirectory's untracked entries; `status` counts
  the whole repo's (`?2` → `?3`, and a deeper cwd `clean` → `?3`). Kept: `diff HEAD`'s line counts were already
  whole-repo, so the chip now describes one thing.
- The short sha for a detached HEAD is the first 7 characters of `# branch.oid` (today: `rev-parse --short`, whose
  default abbreviation grows with the object count — this repo, ~17k objects, gets 8 — so a detached pane's
  `@<sha>` branch changes once on deploy).
- With `--no-optional-locks` on both commands nothing writes a refreshed index back, so racily-clean files are
  re-hashed on each refresh until the user's own git command rewrites the index. Accepted; in a repo with a clean
  filter (e.g. git-crypt) that's one filter process per stat-dirty file per refresh, until then.
- A failed identity read (`resolve_repo` falls back to the cwd, `github_slug` to `None`, e.g. on its 3 s timeout) is
  cached for the full 10-minute identity TTL, so in that rare case an untagged pane sits in a cwd-keyed repo track
  and PR links lack a slug for up to 10 minutes; the identity reads don't take the background refresh's longer
  timeout.
- A cwd with no work tree reads `clean` with the HEAD branch (`?` when detached); a plain bare repo now reads that way
  too (today's read gave its branch and a garbage `?1` from `ls-files`' error text).
- A background read that returns nothing for a cwd that is still a directory keeps the last state, so a checkout
  that stops being a repo (`.git` deleted in place) keeps showing its last branch until the pane leaves it.
- A cwd's branch shown on the dashboard can lag a checkout by up to ~20 s (the stale entry is served while the
  background refresh runs); pid rebinding uses the same, possibly stale, branch.
- argv[0] names the executable for claude and codex (they don't rewrite their process title). A process whose argv[0]
  is a path with a space reads only up to the space — none of the processes periscope classifies.
- A newline or `\x1f` in a window name given with `new-window -n`, a path or an `@`-option (tmux escapes renames and
  session names) still breaks its pane's `list-panes` row; that row is skipped (logged once), so
  for as long as the name holds the newline the pane is missing from `all_pane_ids` (its alerts are GC'd) and its
  window from the full roster (its `pane_tracks` row can be pruned, #19). Before this change a tab or newline there
  crashed the whole `list_windows`.
- `all_pane_ids` and the pane-to-claude map read a list-panes snapshot up to 1 s old (the claude map, with its own
  1 s cache, up to ~2 s): a pane created in that window reads as "no claude yet" and fails open (#14, #16).
- `gh auth token` prints the token gh would use for github.com; a token revoked within the 10 minutes fails until the
  401 re-read (at most a minute).
- The audit hook's per-event cost (a string compare) is negligible against periscope's file I/O.
