# Webview recycle gate

The Tauri shell recycles the leaking webview when you leave periscope (not only when you leave the machine), never
in a loop and never by pulling focus, and kills displaced renderers instead of leaking one when a rebuild stumbles.
THI-449.

## What the logs showed (`~/.config/periscope/shell.log`, 2026-08-07 → 11-01, 440 recycles)

- **The gate is too strict for a working day.** A recycle needs the renderer over `PERISCOPE_RECYCLE_GB` (1 GB)
  **and** no input to any app for `PERISCOPE_RECYCLE_IDLE_S` (300 s). Recycles do run when you step away, but while
  you work the footprint grows unchecked: 730 one-minute checks over threshold and blocked on idle in 36 hours
  (10-29 → 10-30), peaking at 7.7 GB. The leak comes from input inside the webview (~2.5 graphics regions per click,
  `docs/tauri-shell.md`), so leaving periscope for another app is the natural moment to recycle.
- **1 GB can't tell a leaked renderer from a fresh one.** A freshly reloaded dashboard measures 1.1–1.6 GB within a
  minute (e.g. 10-28 16:12 at 1409 MB, 10-15 13:56 at 1121 MB). So the idle gate already looped: 62 recycles came
  less than 90 s after the previous one, 20 of them on 10-09 12:07–12:27, each new renderer 1.4–1.6 GB at destroy.
- **A stumbling rebuild leaks the old renderer.** On 10-28 20:51 the first three rebuild attempts timed out (5 s
  each, main thread busy), but one of them built the window: the page registered its notification categories at
  20:51:48. Attempts 4–10 then failed with "a webview with label `main` already exists", the loop gave up, and the
  reaper, which only runs on the success path, never killed WebContent 41485 (still alive at 1.1 GB three days later).
  The same error also means the opposite: in 22 recycles attempt 1 failed with it 250 ms after `destroy()`, because
  the OLD window was still registered, and attempt 2 then succeeded.
- **A rebuild activates the app.** `recycle_now` restores the window with `restore_state(StateFlags::all())`, which
  in tauri-plugin-window-state 2.4.1 calls `show()` then `set_focus()`; tao's `set_focus` runs
  `activateIgnoringOtherApps: YES`. Harmless while you were away; under a gate that fires while you work in another
  app, it would pull focus out from under you.

## Decisions

**D1 — Recycle when periscope has been unfocused for 30 s, or after the existing system idle, but never within 30
minutes of the last rebuild; the threshold rises to 2 GB.** A recycle needs all of:
- footprint ≥ `PERISCOPE_RECYCLE_GB`, default raised from 1.0 to **2.0** (above every fresh renderer in the log,
  well below the leaked peaks);
- the renderer has lived ≥ `PERISCOPE_RECYCLE_MIN_AGE_S`, default 1800 s, since the last rebuild (or the app's
  start), so no gate can loop;
- either periscope has been unfocused ≥ `PERISCOPE_RECYCLE_UNFOCUSED_S` (default 30 s), or system input has been
  idle ≥ `PERISCOPE_RECYCLE_IDLE_S` (default 300 s; covers periscope left frontmost while you're away).

The manual View → Recycle Webview item stays ungated. Rejected: recycling only when hidden or occluded (periscope
often stays visible on a second screen, so it would rarely fire), and a hard ceiling that recycles under you while
focused (the unfocused gate makes that interruption unnecessary).

**D2 — One focus signal, owned by `focus.rs`.** Today `notifications.rs` keeps a bare `FOCUSED` boolean. It moves
to `focus.rs`, which holds one state behind a mutex — `Unknown` (from launch until the first focus event),
`Focused`, or `UnfocusedSince(Instant)` — so a reader never sees "unfocused" paired with a stale time. `set_focused`
is called from `main.rs`'s `on_window_event` (`Focused(f)` → f, `Destroyed` → false); only a real change moves the
clock, so a recycle's `Destroyed` after an earlier `Focused(false)` keeps it running. `is_focused()` serves the
banners' quiet-while-focused check; `unfocused_for() -> Option<Duration>` is None while focused or Unknown.

**D3 — The rebuild loop shares one result channel across attempts.** Every attempt's closure sends its result on
the same channel instead of a per-attempt one. The main thread runs closures in order, so when an attempt times out
but later builds the window, its `Ok` reaches the channel before the "already exists" errors of attempts queued
behind it; the loop takes that `Ok` as the success it is (clearing `RECYCLING`). An "already exists" that arrives
first still means the old window is registered, so the loop keeps retrying as today. No error text is matched.

**D4 — A rebuild doesn't force focus unless periscope had it.** `recycle_now` notes `focus::is_focused()`
before `destroy()`. The rebuild restores every saved state except visibility
(`StateFlags::all() - StateFlags::VISIBLE`), which removes the plugin's forced `activateIgnoringOtherApps: YES`, and
calls `set_focus()` only when periscope had focus — so the manual menu recycle keeps the window focused. wry still
calls `NSApplication::activate` for every webview it creates (macOS 14+), and the manual test showed macOS honours
it while another app is frontmost (a recycle with Claude frontmost left Periscope frontmost 0.5 s later). So a
recycle that started unfocused notes the frontmost app's pid (`NSWorkspace.frontmostApplication`) before
`destroy()`, and 0.5 s after the rebuild lands activates it again (`NSRunningApplication activateWithOptions:`);
verified: Claude stayed frontmost through two recycles. It also records the rebuild time for D1's minimum age.

**D5 — Every check sweeps leftover renderers, but only with a fresh live pid; the after-recycle reaper goes.** Once
per `CHECK_INTERVAL_S`, the live WebContent pid is read on the main thread and the answer comes back on a channel
with a timeout; the sweep runs only when this tick's read answered with a pid and no recycle is in flight, and each
kill re-checks both (recycle flag, rebuild time) immediately before signalling, so a stale pid after an unannounced
renderer restart (wry reloads the page itself when its WebContent dies) or a recycle that finished mid-sweep is not
mistaken for a leftover; what remains is a microsecond gap against a crash-reload, whose cost is one page reload.
With no `main` window the pid is reset to 0. The sweep SIGKILLs every other WebContent the app is responsible for at
or above 256 MB: candidates come from `proc_listallpids`, kept when their executable path (`proc_pidpath`) contains
`WebKit.WebContent` and their responsible pid (`responsibility_get_pid_responsible_for_pid`, private libSystem, the
call `periscope/resources.py` also uses) is this process. The floor spares WebKit's ~7 MB pre-warmed spare; the
smallest displaced renderer the reaper ever killed was 522 MB. This one path replaces `reap_old_webcontent`'s 10 s
kill: it covers a normal recycle (within one check, ≤ 60 s), a rebuild that gave up but left a window, and anything
else that leaves one parked. (A give-up that left no window reads a live pid of 0 and sweeps nothing; quitting the
app clears those renderers.) Renderers left by an older run of the app die with it, so quitting the current app
clears today's 41485.

**D6 — Policy is pure and unit-tested; the OS calls stay thin.** `should_recycle(Inputs) -> bool` and
`sweep_targets(candidates, live_pid) -> Vec<pid>` hold the decisions and get `cargo test` cases next to
`notifications.rs`'s and `zoom.rs`'s; `focus.rs`'s state transitions get one test.

## Changes

- `src-tauri/src/focus.rs` (new): the focus state (D2).
- `src-tauri/src/notifications.rs`: drops `FOCUSED` / `set_focused`, reads `focus::is_focused()`.
- `src-tauri/src/main.rs`: `mod focus`; `on_window_event` calls `focus::set_focused`.
- `src-tauri/src/recycle.rs`: the gate and its knobs (D1), the shared channel (D3), the focus-preserving rebuild and
  the rebuild timestamp (D4), the answered pid refresh and the sweep replacing `reap_old_webcontent` (D5), the pure
  policy and its tests (D6). The "waiting" log line names what is holding the recycle back.
- `docs/tauri-shell.md`: the recycling paragraph describes the new gate, knobs, rebuild and sweep.
- `static/src/notify/persist.js`: its header's "recycle.rs rebuilds the webview once you've been idle a while"
  becomes "…once you've left periscope or been idle a while".

## Testing

- `cargo test` in `src-tauri/`:
  - `should_recycle`: under threshold never; too young never; unfocused ≥ / < gate; focused or unknown with idle
    ≥ / < gate.
  - `sweep_targets`: skips the live pid, skips < 256 MB, takes nothing when the live pid is unknown (0), keeps
    ≥ 256 MB others.
  - `focus`: Unknown → Focused → UnfocusedSince (clock runs, a repeated false keeps it) → Focused.
- Manual, on the rebuilt `.app` (replacing the running app needs the user's go-ahead), launched with
  `open --env PERISCOPE_RECYCLE_GB=0.2 --env …_MIN_AGE_S=60 --env …_UNFOCUSED_S=10` (`launchctl setenv` from the
  agent's shell did not reach the app):
  - switching to another app for 10 s recycles, and you **stay** in that app (D4);
  - no second recycle within the minimum age;
  - the manual menu recycle leaves periscope focused;
  - whether minimizing produces a recycle (assumption below); `shell.log` shows the sweep never killing the live pid.
- After merge: the popover's "webview" line (THI-448) shows whether the renderer stays near or under 2 GB through a
  working day.

## Assumptions

- Handing focus back 0.5 s after the rebuild lands comes after wry's activation; verified on macOS 27 (Claude stayed
  frontmost at every sample from 0.25 s to 5 s). A slower machine could show Periscope briefly before the hand-back.
- After a hand-back the unfocused clock restarts (wry's activation emits `Focused(true)`, the hand-back
  `Focused(false)`); verified: the next "waiting" line read "unfocused 59s". With the 30 min minimum age this only
  delays the next recycle by the 30 s gate.
- Not exercised by the manual test (no UI scripting available): the manual menu recycle while focused, and whether
  minimizing counts as unfocused.
- The app has exactly one legitimate WebContent ≥ 256 MB. A service-worker renderer (`main.jsx` registers `/sw.js`)
  or a future WebKit site-isolation process would break that; today's listing shows none.
- Bad knob values (negative, NaN, infinite) fall back to the default rather than stopping the monitor thread.

- `Focused(false)` fires on app switch and ⌘H (tao's `windowDidResignKey`; verified in source). Whether it fires on
  minimize is untested; if not, the idle gate still covers a minimized periscope.
- The rebuilt window, shown while the app is inactive, doesn't cover the frontmost app's window. Verify in the manual
  test.
- `proc_listallpids` takes a byte size and returns a count; it and `responsibility_get_pid_responsible_for_pid` link
  from Rust without a `#[link]` attribute (both verified in a scratch binary).
- A displaced renderer is ≥ 256 MB by the time a check sees it; a smaller one is left until it grows or exits.
