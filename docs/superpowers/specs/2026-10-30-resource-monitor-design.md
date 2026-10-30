# Resource monitor

The dashboard shows how much memory and CPU each owner uses (agent sessions, the periscope server, its webview)
so that lag with ~10 agents can be traced to an owner before anything is optimized. THI-448, first slice.

## What the measurements showed (2026-10-30, 11 panes)

| Owner | Footprint | CPU |
|---|---|---|
| Machine (16 GB RAM, 10 cores) | ~48 GB; 16.5 of 17.4 GB swap used | load avg 50–70 |
| Agent sessions, whole pane subtrees | 12.8 GB | 112% |
| ↳ heaviest pane | 6.8 GB, mostly `tsc`/`prettier`/`eslint`; its claude 446 MB | 58% |
| Webview (WebKit `WebContent`) | 2.9 GB, peaked at 7.7 GB the same day | ~40% |
| Periscope server + tmux control clients | 176 MB | ~7% |

`ps` RSS left out compressed and swapped pages: the webview read 276 MB resident against 2,855 MB of footprint, one
claude 111 MB against 460 MB. `proc_pid_rusage` read footprint and CPU time for 6 pids in ~1 ms without a fork, and
its CPU deltas matched `top`.

## Scope

In: measurement, the header pill and popover, the detail-header numbers, the webview through the shell.
Out, each its own later PR: dashboard freshness (`build_state` time, `/ws/state` frame gap), a 1 h history, a
"recycle pending" marker for the webview. THI-449 (recycle gate) and THI-450 (subprocesses per tick) are separate
tickets.

## Decisions

**D1 — Memory is `phys_footprint`, CPU is the change in CPU time; both come from `proc_pid_rusage`.** Footprint is
what Activity Monitor calls Memory and what the shell's recycle already uses; RSS hides exactly the pressure this
feature exists to show. CPU% is per core like `top` (100% = one core), computed over the interval since the previous
sample of the same process. Called through `ctypes` on `/usr/lib/libproc.dylib` with the `RUSAGE_INFO_V0` flavor
(the struct the shell binds in `recycle.rs`); CPU times are mach absolute units, scaled by `mach_timebase_info`.
Stdlib only, so `uv run server.py`'s PEP-723 header doesn't change.

**D2 — Measurement runs inside `build_state`, once per hub tick, and adds no subprocess.** It reuses the process
table (`session_status.proc_table()`) and the tmux pane roots (`_tmux_pane_pids()`) that each tick already forks
for; the pane roots get cached alongside `pane_claude_pids()`'s result so they are read, not re-forked. A tick
costs ~0.2 ms per process for the `proc_pid_rusage` calls (~25 ms at today's ~120 processes in pane subtrees). A
separate sampler thread was rejected: it would need its own process table (another `ps`, or a larger `libproc`
binding) and a thread under `docs/testing.md`'s rules, for steadier CPU windows nobody asked for yet.

**D3 — A process is identified by (pid, start time).** The previous CPU sample is keyed by the pair, using
`ri_proc_start_abstime` from the same struct, so a recycled pid starts a fresh delta instead of reporting the
difference between two unrelated processes. A process seen for the first time has no CPU% yet; it counts as 0 in
sums. Entries for processes absent from a tick are dropped.

**D4 — A session's numbers cover the pane's whole process subtree.** Rooted at the pane's `pane_pid`, walked with
the same `proc_table()` children map `pane_claude_pids()` uses. The claude process alone undercounts a busy session
(446 MB against 6.8 GB above). `sessions` is the total over every tmux pane, including panes that run no agent.

**D5 — The periscope server is its own process plus its children.** That covers the tmux control-mode clients and
whatever `git`/`gh`/`ps` is running at that instant. The tmux server is not counted anywhere: it is shared, and it
measured 74 MB and 0.4%.

**D6 — The webview is measured by the server from a pid the shell writes to a file.** The shell already reads the
`WebContent` pid every 60 s (`refresh_webcontent_pid`); it also writes it to `~/.config/periscope/webcontent.pid`,
next to `shell.log` and `zoom`. The server reads the file each tick and measures that pid itself, but only when the
file is less than 3 minutes old and `proc_pidpath` names a `com.apple.WebKit.WebContent` executable. Otherwise
`webview` is `null`: an older `.app`, a quit app, or a dashboard viewed in a browser tab all just omit the line.
The webview's `WebContent` has launchd as its parent and no sockets of its own (WebKit's networking process holds
them), so the server cannot find it without the shell.

**D7 — The machine line reads the kernel directly, with no fork.** Load average from `os.getloadavg()`; swap used
and total from `sysctlbyname("vm.swapusage")`; free memory percent from `kern.memorystatus_level` (the figure
`memory_pressure` prints).

**D8 — The ↻ cycle hint keeps watching the claude process alone, measured as footprint.** The leak it targets lives
in claude, not in its tool children. `mem_signal` reads footprint instead of RSS, the thresholds are renamed to
`MEM_BAD_FOOTPRINT_KB` / `MEM_WARN_FOOTPRINT_KB` and keep their values (4 GB / 2 GB), and the chip's `rss_gb` becomes
`mem_gb`. Today's claudes sit at 250–550 MB of footprint, so nothing new trips. `_claude_procs()` keeps its `ps`
call for liveness and age; moving that off `ps` belongs to THI-450.

**D9 — Without `libproc` (not macOS), measurement is off.** One warning at first use; `res` and `resources` are
absent from `/api/state`, and the UI renders nothing for them. A pid that exits or refuses access between the
process table and the `proc_pid_rusage` call drops out of the sums; nothing in this path raises into `build_state`.

## Server

`periscope/resources.py` — stdlib only, no periscope imports, like `session_status`:

- `usage(pids)` → `{pid: (footprint_bytes, cpu_ns, start)}` for the pids it could read.
- `sample(pids)` — reads `usage`, diffs against the previous sample per (pid, start), returns `{pid: (footprint_bytes,
  cpu_pct)}`, and updates the stored samples.
- `subtree(root, kids)` → the root and all its descendants.
- `machine()` → `{load1, swap_used_mb, swap_total_mb, mem_free_pct}`.
- `webview_pid()` → the validated pid from D6, or `None`.

`build_state` calls one function in `resources` that samples every pid it needs in a single pass (all pane
subtrees, the server subtree, the webview), then:

- each window row gets `res: {mem_mb, cpu_pct}` for its pane's subtree;
- the blob gets `resources: {server, webview, sessions, machine}`, where `server`, `webview` and `sessions` are
  `{mem_mb, cpu_pct}` (`webview` may be `null`) and `machine` is D7's dict.

`/api/healthz` adds `resources: {server}` with the server's `{mem_mb, cpu_pct, children}`. It samples the server
subtree itself and shares the stored CPU samples with `build_state`.

## Shell

`recycle.rs`: after `refresh_webcontent_pid` stores a pid, write it to `~/.config/periscope/webcontent.pid`
(overwriting, so the mtime is the freshness signal), including right after a recycle rebuilds the webview. Failure
to write is ignored, like `shell_log`. `docs/tauri-shell.md` gains a sentence on the file. The change needs an `.app`
rebuild; until then D6's fallback applies.

## UI

- **Header pill** after `UsagePill`: `▣ 18.1G · 165%`, the sum of server, webview and sessions. Hidden when
  `resources` is absent.
- **Popover** on click: periscope server, webview, then every session sorted by memory (a row selects its pane),
  then the machine line: `load 52 · swap 16.5/17.4G · 27% free`. The CPU column's tooltip says 100% = one core.
- **Detail header**: a `· 6.8G · 58%` part after the context %, for the selected pane.
- Formatting (MB→`0.6G`/`512M`, percent rounding) and sorting go in a pure `static/src/chrome/resources.js`, tested
  with vitest, the way `usageSummary.js` backs `UsagePill`.
- `memHint` reads `mem_gb`.

## Tests

- `tests/test_resources.py`: CPU% on a first sample (0), on a second, after a pid reuse (new start time → fresh
  sample), and for a pid gone from the tick (dropped); subtree sums over a fake children map; `webview_pid` for a
  missing file, a stale file, a pid whose path isn't `WebContent`, and a valid one; the no-`libproc` path. The
  `proc_pid_rusage` call is behind one function the tests replace; one smoke test reads the test process itself.
- `tests/test_window_view.py`: `mem_signal` on footprint at each tier.
- `tests/routes/`: `/api/state` carries `res` and `resources`; `/api/healthz` carries `resources.server`.
- vitest: `resources.js` formatting and ordering; `memHint` with `mem_gb`.

## Assumptions

- Every process measured belongs to the user running periscope; `proc_pid_rusage` on a root-owned pid fails and the
  pid is skipped (D9). Verified for claude, the server and `WebContent`.
- ~120 processes per tick stays cheap enough at ~0.2 ms each; if a tick's measurement grows past ~50 ms, that shows
  up in the freshness slice.
