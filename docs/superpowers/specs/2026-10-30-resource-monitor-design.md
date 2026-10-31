# Resource monitor

The dashboard shows how much memory and CPU each owner uses (the panes, the periscope server, its webview, and
everything else) so that lag with ~10 agents can be traced to an owner before anything is optimized. THI-448, first
slice.

## What the measurements showed (2026-10-30, 11 panes)

| Owner | Footprint | CPU |
|---|---|---|
| Machine (16 GB RAM, 10 cores) | ~48 GB; 16.5 of 17.4 GB swap used | load avg 50–70 |
| Panes, whole process subtrees | 12.8 GB | 112% |
| ↳ heaviest pane | 6.8 GB, mostly `tsc`/`prettier`/`eslint`; its claude 446 MB | 58% |
| Webview: `periscope-app` and its WebKit helpers | live WebContent 2–3 GB (7.7 GB peak), a parked WebContent never reaped 1.3 GB, GPU 79 MB | ~40% WebContent, GPU most CPU of the app |
| Periscope server + tmux control clients | 176 MB | ~7% own, ~38% more in reaped children |
| Elsewhere, e.g. an orphaned `lgtm` node server | 4.1 GB for that one process | ~0 |

`ps` RSS left out compressed and swapped pages: the webview read 276 MB resident against 2,855 MB of footprint, one
claude 111 MB against 460 MB.

## Scope

In: measurement, the header pill and popover, the detail-header numbers.
Out, each its own later PR: dashboard freshness (`build_state` time, `/ws/state` frame gap), a 1 h history.
THI-449 (recycle gate, unreaped WebContent) and THI-450 (subprocesses per tick) are separate tickets.

## Decisions

**D1 — Memory is `phys_footprint`; CPU is the change in CPU time; both come from `proc_pid_rusage`.** Footprint is
what Activity Monitor calls Memory and what the shell's recycle already uses. Read through `ctypes` on
`/usr/lib/libproc.dylib` with the `RUSAGE_INFO_V1` flavor, which adds the CPU time of a process's reaped children
to the V0 fields `recycle.rs` binds. CPU times are mach absolute units, scaled by `mach_timebase_info` (125/3 on
this machine). Stdlib only, so `uv run server.py`'s PEP-723 header doesn't change. CPU% is per core like `top` and
Activity Monitor (100% = one core). Measured cost: ~3 µs per process, ~2 ms for every process on the machine.

**D2 — Measurement runs inside `build_state`, once per tick, on the tick's existing process snapshot.** It reads
`agent_processes.process_snapshot()`, which the fan-out already takes every tick (`codex_process_for_pane`), so it
adds no subprocess. When the snapshot is `None` (a `ps` failure), `res` and `resources` are `null` for that tick
rather than computed from an empty tree, which would collapse every pane to its shell. A separate sampler thread
was rejected: it would need its own process table and a thread under `docs/testing.md`'s rules.

**D3 — Every process on the machine lands in exactly one group.**
- **Each window row's pane:** the row's `pane_pid` and all its descendants in the snapshot. The claude process alone
  undercounts a busy session (446 MB against 6.8 GB above). `panes` is the sum over rows, so the rows add up to it.
- **Server:** periscope's own pid and its descendants (the tmux control clients, whatever `git`/`gh`/`ps` is running).
- **Webview:** the `periscope-app` process (matched by the kernel's process name, `proc_name`: `ps` cuts the
  `comm` column to 16 characters) and every process whose responsible pid is that app, found through
  libSystem's `responsibility_get_pid_responsible_for_pid`. That covers every WebKit helper: the live WebContent,
  any parked WebContent the recycle didn't reap, GPU and Networking. The WebKit helpers have launchd as their parent,
  so the process tree can't find them. With no `periscope-app` running (dashboard in a browser tab), `webview` is
  `null`.
- **Elsewhere:** every other process readable by the user. That includes the tmux server, non-active panes of split
  windows, the `periscope-input` pane, daemons a session left running, and every other app. No attempt is made to
  attribute these: the orphaned `lgtm` server's responsible process is Ghostty, the same as Ghostty's own processes.
  Processes `proc_pid_rusage` can't read (root-owned, or gone since the snapshot) are skipped.

**D4 — A process is identified by (pid, start time), and a group's CPU accounts for exits.** Samples are keyed by
`(pid, ri_proc_start_abstime)`, so a recycled pid never diffs against an unrelated process. A process's CPU total is
its own time plus its reaped children's. A group's CPU over the interval is:
- for each member present in both samples, the change in its total;
- plus the whole total of a member that started after the previous sample;
- minus the last total of each member that vanished since the previous sample, because when it is reaped its whole
  CPU time reappears in its parent's children time.
The result is clamped at 0: a vanished process reaped outside the group would otherwise subtract time the group
never gains back. This catches the short-lived `git`/`gh`/`tsc` runs that live less than a tick, which own-time
sampling would count as 0.

**D5 — Samples are taken under a lock, at most every 0.5 s, and never against stale history.** `build_state` can
run concurrently from the state hub, the REST fallback poll and other tabs. One lock covers read-diff-write. A
caller within 0.5 s of the previous sample gets the previous measurement back instead of a new delta (5 ms windows measured 0%
for a server averaging 10%). A previous sample older than 10 s (a hub that idled with no subscribers) counts as
missing. With no usable previous sample, `cpu_pct` is `null`, never 0, so a zero always means "measured idle".

**D6 — The machine line reads the kernel directly, with no fork.** Load average from `os.getloadavg()`; swap used
and total from `sysctlbyname("vm.swapusage")`; free memory percent from `kern.memorystatus_level`, the figure
`memory_pressure` prints.

**D7 — The ↻ cycle hint keeps watching the claude process alone, measured as footprint.** The leak it targets lives
in claude, not in its tool children. The thresholds become `MEM_WARN_MB` / `MEM_BAD_MB` at the same values (2 GB /
4 GB); today's claudes sit at 250–550 MB, so nothing new trips. `claude_proc_for` reads the claude's footprint
directly (`resources.footprint_mb`, one ~3 µs read), because the fan-out builds each row before `measure` runs; the chip sends `{tier, gb, age_s, trigger}` where `trigger` is `"mem"` or `"age"`, so
`memHint` labels from it instead of repeating the 2 GB threshold in JS. `_claude_procs()` keeps its `ps` call for
liveness and age but drops `rss=`; moving liveness off `ps` belongs to THI-450.

## Server

`periscope/resources.py`, stdlib plus `agent_processes` (itself stdlib-only) for the snapshot and `descendants`:

- `measure(panes)` → `Measurement | None`, given `{pane_id: pane_pid}`. It reads every process once, assigns each
  to a group (D3), computes footprint and CPU per group (D4, D5), and returns `panes: {pane_id: {mem_mb, cpu_pct}}`
  and `totals: {server, webview, panes, elsewhere, machine}`. Each group is `{mem_mb, cpu_pct}`; `webview` may be
  `null`; `machine` is D6's `{load1, swap_used_mb, swap_total_mb, mem_free_pct}`. `None` when the snapshot failed.
- `footprint_mb(pid)` — one process's footprint, for the ↻ hint (D7).

`build_state` calls `measure` after the fan-out and before the narrator merge. Each row gets `res`; the blob gets
`resources`.

`/api/healthz` is unchanged: its only programmatic caller (`bin/periscope`) reads `.version`, and a narrow sample
there would disturb the CPU history of every other group.

## UI

- **Header pill** after `UsagePill`: `▣ 18.1G`, the memory of server, webview and panes together. CPU stays out of
  the pill. Hidden when `resources` is absent.
- **Popover** on click, one line each with memory and CPU: periscope server, webview, then every pane sorted by
  memory (a line selects its pane), then elsewhere, then the machine line: `load 52 · swap 16.5/17.4G · 27% free`.
  The CPU column's tooltip says 100% = one core. A `null` CPU shows `–`.
- **Detail header**: `· 6.8G mem · 58% cpu` after the context %, for the selected pane.
- Formatting (MB → `0.6G` / `512M`, CPU rounding, `null`) and the sort go in a pure `static/src/chrome/resources.js`,
  tested with vitest, the way `usageSummary.js` backs `UsagePill`.
- `memHint` reads `gb` and `trigger`.

## Tests

- `tests/conftest.py`: an autouse fixture stubs `resources.measure`'s system calls, so no test reads the real
  machine's processes. Tests of `resources` itself replace the same seams with fakes.
- `tests/test_resources.py`, over a fake snapshot and fake rusage:
  - group assignment, including a WebKit helper found by responsible pid and a process outside every group;
  - CPU: first sample (`null`), steady second sample, pid reuse, a child started after the previous sample, a child
    that exits and is reaped by a member, a member reaped outside the group (clamped);
  - the 0.5 s and 10 s rules, and two threads sampling at once;
  - a `None` snapshot;
  - one smoke test that reads the test process itself through the real `proc_pid_rusage`.
- `tests/test_window_view.py`: `mem_signal` on footprint, each tier and trigger (updating today's RSS cases).
- `tests/routes/`: `/api/state` carries `res` and `resources`, and omits both when the snapshot is `None`.
- vitest: `resources.js` formatting, `null` CPU and ordering; `memHint` with `gb`/`trigger` (updating
  `memHint.test.js` and `agent.test.js`).

## Assumptions

- Periscope runs only on macOS (`ci.yml`: "periscope only runs on macOS hosts"), so `libproc` is always present.
- `responsibility_get_pid_responsible_for_pid` is a private libSystem function, the same kind of private API the shell
  already relies on (`_webProcessIdentifier`). It worked on this machine (macOS 27) for every WebKit helper.
- Summing `phys_footprint` across the webview's processes doesn't double count; WebKit hands IOSurface ownership to
  WebContent. Not proven for the GPU process.
- A webview open on JS older than the server shows a malformed ↻ label until it reloads.
