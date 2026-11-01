# Webview recycle gate

The Tauri shell recycles the leaking WKWebView when you leave periscope, not only when you leave the machine, and
kills every displaced WebContent instead of leaking one when a rebuild stumbles. THI-449.

## What the logs showed (`~/.config/periscope/shell.log`, 2026-10-26 → 11-01)

- **The gate is too strict for a working day.** A recycle needs the renderer over `PERISCOPE_RECYCLE_GB` (1 GB)
  **and** no input to any app for `PERISCOPE_RECYCLE_IDLE_S` (300 s). Recycles do run when you step away (8 on
  10-28, 11 on 10-30/31), but while you work the footprint grows unchecked: 730 one-minute checks over threshold and
  blocked on idle in 36 hours, peaking at 7.7 GB. The leak comes from input inside the webview (~2.5 graphics regions
  per click, `docs/tauri-shell.md`), so leaving periscope for another app is the natural moment to recycle.
- **A stumbling rebuild leaks the old renderer.** On 10-28 20:51 the rebuild's first three attempts timed out (5 s
  each; the main thread was busy) but one of them did build the window: the page registered its notification
  categories at 20:51:48. Attempts 4–10 then failed with "a webview with label `main` already exists", the loop gave
  up, and the reaper — which only runs on the success path — never killed WebContent 41485. It is still alive at
  1.1 GB three days later. A second leftover, 37895 (7 MB, started the moment a reap ran on 10-26), is WebKit's
  pre-warmed spare and harmless.

## Decisions

**D1 — Recycle when the periscope window has been unfocused for 30 s, or after the existing system idle.** Over
threshold AND (`unfocused_for ≥ PERISCOPE_RECYCLE_UNFOCUSED_S`, default 30 s, OR system input idle ≥
`PERISCOPE_RECYCLE_IDLE_S`, default 300 s). The idle branch stays for periscope left frontmost while you're away.
Switching to the terminal or editor for 30 s while over 1 GB costs a ~2 s blank and page reload you are not looking
at; if periscope is visible on another screen you may glimpse it. Rejected: recycling only when the window is
hidden or occluded (periscope often stays visible on a second screen, so it would rarely fire), and a hard ceiling
that recycles under you while focused (an interruption the unfocused gate makes unnecessary).

**D2 — One focus signal, owned by a small `focus.rs`.** Today `notifications.rs` keeps a bare `FOCUSED` boolean set
from `on_window_event`. It moves to `focus.rs`, which records the state and when it last changed:
`set_focused(bool)` (called from the same hook, `Focused(f)` → `f`, `Destroyed` → false), `is_focused()` (the
banners' quiet-while-focused check keeps using it), and `unfocused_for() -> Option<Duration>` (None while focused).
The app starts unfocused-since-launch until the first `Focused(true)`. A recycle destroys the window, which counts as
unfocused, so the clock runs through the rebuild.

**D3 — "Already exists" means an earlier attempt built the window.** In the rebuild loop, an attempt that fails
with "a webview with label `main` already exists" is the success of an attempt that timed out: the loop logs
"webview rebuilt (an earlier attempt landed)", clears `RECYCLING`, and returns, like a normal success. The timed-out
attempt's closure already restored the window state and zoom, because it ran to completion on the main thread.

**D4 — Every check sweeps leftover WebContents; the after-recycle reaper goes.** Once per `CHECK_INTERVAL_S`, after
the live WebContent pid is refreshed, every other WebContent process the app is responsible for with a footprint of
at least 256 MB is SIGKILLed and logged. Found by listing all pids (`proc_listallpids`), keeping those whose
executable path (`proc_pidpath`) contains `WebKit.WebContent` and whose responsible pid
(`responsibility_get_pid_responsible_for_pid`, private libSystem — the same call periscope's server uses to find the
webview, `periscope/resources.py`) is this process. Never runs while the live pid is unknown (0), so the live
renderer is never a candidate. The 256 MB floor leaves WebKit's pre-warmed spare (~7 MB) alone; every displaced
renderer seen in the log was ≥ 700 MB. This one path replaces `reap_old_webcontent`'s 10 s after-recycle kill:
it covers a normal recycle (within one check, ≤ 60 s), the give-up path, and anything else that leaves a renderer
parked. The first check after the new `.app` starts kills today's 41485.

**D5 — Policy is pure and unit-tested; the OS calls stay thin.** `should_recycle(footprint, threshold,
unfocused_for, idle, unfocused_gate, idle_gate) -> bool`, `rebuilt_by_earlier_attempt(err: &str) -> bool` and
`sweep_targets(candidates: &[(pid, footprint)], live_pid) -> Vec<pid>` hold every decision and get `cargo test`
cases next to `notifications.rs`'s and `zoom.rs`'s. The monitor loop gathers inputs and acts on their answers.

## Changes

- `src-tauri/src/focus.rs` (new): the focus state and its clock (D2).
- `src-tauri/src/notifications.rs`: drops `FOCUSED` / `set_focused`, reads `focus::is_focused()`.
- `src-tauri/src/main.rs`: `on_window_event` calls `focus::set_focused`; `mod focus`.
- `src-tauri/src/recycle.rs`: the unfocused gate and its env knob (D1), the "already exists" success (D3), the sweep
  replacing `reap_old_webcontent` (D4), the pure policy functions and their tests (D5). The "waiting" log line names
  both timers: `over threshold (N MB) but focused/unfocused Xs, input Ys ago — waiting`.
- `docs/tauri-shell.md`: the recycling paragraph describes the new gate, its knobs and the sweep.

## Testing

- `cargo test` in `src-tauri/`: `should_recycle` (under threshold never; over threshold with unfocused ≥ / < gate;
  focused with idle ≥ / < gate), `rebuilt_by_earlier_attempt` (the exact error text; other errors), `sweep_targets`
  (skips the live pid, skips < 256 MB, skips everything when live is 0, keeps ≥ 256 MB others).
- Manual, on the rebuilt `.app` (replacing the running app needs the user's go-ahead): lower the gates with
  `launchctl setenv PERISCOPE_RECYCLE_UNFOCUSED_S 10` (GUI apps don't inherit shell env), switch away, and watch
  `shell.log` for the recycle; confirm the first check kills 41485 if it is still alive; confirm the popover's
  "webview" line (THI-448) stays near or under 1 GB through a working day.

## Assumptions

- `Focused(false)` fires when another app becomes frontmost and when periscope is minimized or hidden (tao's
  `windowDidResignKey`). Verify in the manual test; if minimizing doesn't fire it, the idle branch still covers it.
- `proc_listallpids` and `responsibility_get_pid_responsible_for_pid` behave from the shell as they do from Python
  (verified there, macOS 27).
- A displaced renderer is always ≥ 256 MB by the time a check sees it; a smaller one is left until it grows or exits.
