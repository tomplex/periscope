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
- **A rebuild activates the app.** `recycle_now` restores the window with `restore_state(StateFlags::all())` (and the
  plugin's own `on_window_ready` hook does the same for every window Tauri attaches), which in
  tauri-plugin-window-state 2.4.1 calls `show()` then `set_focus()`; tao's `set_focus` runs
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
clock, so a recycle's `Destroyed` after an earlier `Focused(false)` keeps it running. `focus()` is the one locked
read: the recycle monitor takes both its gate input (`Focus::unfocused_for()`, None while focused or Unknown) and
its "waiting" log text from one snapshot. `is_focused()` serves the banners' quiet-while-focused check.

**D3 — The rebuild loop shares one result channel across attempts.** Every attempt's closure sends its result on
the same channel instead of a per-attempt one. The main thread runs closures in order, so when an attempt times out
but later builds the window, its `Ok` reaches the channel before the "already exists" errors of attempts queued
behind it; the loop takes that `Ok` as the success it is (clearing `RECYCLING`). An "already exists" that arrives
first still means the old window is registered, so the loop keeps retrying as today. No error text is matched. The
loop is `await_rebuild(attempts, timeout, submit, log)`; `recycle_now` passes the real submit step (sleep, then
build on the main thread), so the result handling is tested without a window.

**D4 — A rebuild never activates the app.** The window-state plugin's `on_window_ready` hook restores every window
Tauri attaches, each recycle's rebuild included, and with `StateFlags::all()` its VISIBLE restore calls `show()` then
`set_focus()`, which tao runs as `makeKeyAndOrderFront` + `activateIgnoringOtherApps: YES`. That was the activation.
`main.rs` registers the plugin with `StateFlags::all() - StateFlags::VISIBLE` (tauri.conf.json already shows the
startup window), so the hook restores the rebuilt window's frame and nothing in the rebuild activates the app. wry
still calls `NSApplication::activate` for every webview it builds (macOS 14+), but that cooperative activation is
declined while another app is frontmost. Verified 2026-11-02: two unfocused recycles, one while typing in another
app, left that app frontmost ("focus stayed with pid …" both times), and the unfocused clock kept counting. So there
is no explicit `restore_state`, no `set_focus`, and no focus hand-back; keystrokes stay with the app you're in. A
manual menu recycle runs with Periscope already active, and the builder's window becomes key. `recycle_now` saves
the window state before `destroy()` (the plugin's own save runs on exit only), and a window that was minimized
before `destroy()` is minimized again once rebuilt (the plugin has no minimized flag). The rebuild also records its
time for D1's minimum age.

**D5 — Every check sweeps the renderers the monitor has seen live; the after-recycle reaper goes.** Once per
`CHECK_INTERVAL_S`, the live WebContent pid is read on the main thread and the answer comes back on a channel with a
timeout; the read is None with no `main` window, no selector, or no answer. Every pid read joins a set of renderers
seen live (one static behind a mutex, which also keeps the last pid read for `recycle_now`'s "destroying webview"
line, "unknown" when none). The sweep runs only when this tick's read answered and no recycle is in flight. Its
candidates are every WebContent the app is responsible for: `proc_listallpids`, kept when the executable path
(`proc_pidpath`) contains `WebKit.WebContent` and the responsible pid (`responsibility_get_pid_responsible_for_pid`,
private libSystem, the call `periscope/resources.py` also uses) is this process. It SIGKILLs each candidate in the
seen set other than this tick's live pid (WebKit's process cache can hand a parked renderer back to a new webview),
and drops seen pids that are no longer candidates. Each kill re-checks (recycle flag, rebuild time) immediately
before signalling, so a stale pid after an unannounced renderer restart (wry reloads the page itself when its
WebContent dies) or a recycle that finished mid-sweep is not mistaken for a leftover; what remains is a microsecond
gap against a crash-reload, whose cost is one page reload. Every renderer the sweep killed in the log was one the
monitor had read live (each "killed leftover WebContent N" names the pid of the preceding "destroying webview"), so
the seen set replaces a size floor: WebKit's pre-warmed spare, a service-worker renderer or the Web Inspector is
never a target. This one path replaces `reap_old_webcontent`'s 10 s kill: it covers a normal recycle (within one
check, ≤ 60 s), a rebuild that gave up but left a window, and anything else that parks a renderer the page ran in.
(A give-up that left no window reads no live pid and sweeps nothing; quitting the app clears those renderers.)
Renderers left by an older run of the app die with it, so quitting the current app clears today's 41485.

**D6 — Policy is pure and unit-tested; the OS calls stay thin.** `should_recycle(Inputs) -> bool`,
`sweep_targets(candidates, seen, live_pid) -> Vec<pid>`, `sweep(targets, still_valid, kill)` (the per-kill recheck
and the kill injected), `await_rebuild` (D3) and the knob parser `env_number` hold the decisions and get
`cargo test` cases next to `notifications.rs`'s and `zoom.rs`'s; `focus.rs`'s state transitions get one test.

## Changes

- `src-tauri/src/focus.rs` (new): the focus state and its `focus()` snapshot (D2).
- `src-tauri/src/notifications.rs`: drops `FOCUSED` / `set_focused`, reads `focus::is_focused()`.
- `src-tauri/src/main.rs`: `mod focus`; `on_window_event` calls `focus::set_focused`; the window-state plugin
  restores every flag but VISIBLE (D4).
- `src-tauri/src/recycle.rs`: the gate and its knobs (D1), `await_rebuild`'s shared channel (D3), the rebuild with
  no explicit restore or focus call, re-minimized when it was minimized, and the rebuild timestamp (D4), the answered
  pid read, the seen-live set and the sweep replacing `reap_old_webcontent` (D5), the pure policy and its tests (D6).
  The "waiting" log line names what is holding the recycle back.
- `docs/tauri-shell.md`: the recycling paragraph becomes prose plus bullets: the gate and knobs, the rebuild without
  activation, the shared channel, the sweep of renderers seen live, the two invariants, testing via `open --env`.
- `static/src/notify/persist.js`: its header's "recycle.rs rebuilds the webview once you've been idle a while"
  becomes "…once you've left periscope or been idle a while".

## Testing

- `cargo test` in `src-tauri/`:
  - `should_recycle`: under threshold never; too young never; unfocused ≥ / < gate; focused or unknown with idle
    ≥ / < gate.
  - `sweep_targets`: takes renderers seen live that aren't live now; never the live pid; never an unseen one; drops
    seen pids that are gone.
  - `sweep`: a recheck that fails after the first kill stops the rest.
  - `await_rebuild`: a late `Ok` from a timed-out attempt is a rebuild; an "already exists" first, then `Ok`, is a
    rebuild on attempt 2; ten errors give up.
  - knobs: an unset `PERISCOPE_RECYCLE_GB` is 2 GB, and `-1`, `inf`, `NaN` fall back to it; `env_secs` falls back on
    unset, `inf`, `NaN`, `1e30`, `-1`.
  - `focus`: Unknown → Focused → UnfocusedSince (clock runs, a repeated false keeps it) → Focused.
- Manual, on the rebuilt `.app` (replacing the running app needs the user's go-ahead), launched with
  `open --env PERISCOPE_RECYCLE_GB=0.2 --env …_MIN_AGE_S=60 --env …_UNFOCUSED_S=10` (`launchctl setenv` from the
  agent's shell did not reach the app):
  - switching to another app for 10 s recycles, and you **stay** in that app, typing included (D4; verified
    2026-11-02);
  - no second recycle within the minimum age;
  - the manual menu recycle leaves periscope focused;
  - whether minimizing produces a recycle (assumption below); `shell.log` shows the sweep never killing the live pid.
- After merge: the popover's "webview" line (THI-448) shows whether the renderer stays near or under 2 GB through a
  working day.

## Assumptions

- wry's `NSApplication::activate` is declined while another app is frontmost (macOS 14+ cooperative activation);
  verified on macOS 27 with the plugin's VISIBLE restore gone (D4). An OS that honours it again would put Periscope
  in front on every unfocused recycle.
- A ⌘H-hidden app stays hidden through a rebuild: nothing activates the app, so a window built in a hidden app
  should stay hidden (unverified).
- A minimized window is re-minimized after the rebuild (unverified manually); it is shown for the moment between
  the build and `minimize()`.
- Not exercised by the manual test (no UI scripting available): the manual menu recycle while focused; whether
  minimizing counts as unfocused; a fullscreen Periscope in another Space (the plugin restores fullscreen on the
  rebuilt window).
- A renderer replaced before any check read it as live (two manual recycles within a minute) never joins the seen
  set and isn't swept; quitting the app clears it.
- A seen pid stays in the set until a sweep no longer finds it among the candidates (one or two checks after it
  dies); a new WebContent of this app reusing that pid within that window would be killed. macOS hands out pids in
  increasing order, so that needs a wrap of the pid space within a couple of minutes.
- Bad knob values (negative, NaN, infinite, or too large for a `Duration`) fall back to the default rather than
  disabling recycling or stopping the monitor thread.
- `Focused(false)` fires on app switch and ⌘H (tao's `windowDidResignKey`; verified in source). Whether it fires on
  minimize is untested; if not, the idle gate still covers a minimized periscope.
- `proc_listallpids` takes a byte size and returns a count; it and `responsibility_get_pid_responsible_for_pid` link
  from Rust without a `#[link]` attribute (both verified in a scratch binary).
