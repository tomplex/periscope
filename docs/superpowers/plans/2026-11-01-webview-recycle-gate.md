# Webview Recycle Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Tauri shell recycles the leaking webview once periscope has been unfocused for 30 s (or the machine is idle), never within 30 min of the last rebuild and only above 2 GB, without pulling focus; rebuild attempts share one result channel; a per-check sweep kills leftover renderers (THI-449).

**Architecture:** A new `focus.rs` owns the window's focus state (one mutex: Unknown / Focused / UnfocusedSince). `recycle.rs` gets a pure `should_recycle(Inputs)` and `sweep_targets`, a rebuild that shares one channel across attempts and restores everything but visibility, a pid read that answers on a channel, and a sweep replacing the 10 s reaper.

**Tech Stack:** Rust (Tauri 2, tao, tauri-plugin-window-state 2.4, objc2, libc), macOS libproc / libSystem.

**Spec:** `docs/superpowers/specs/2026-11-01-webview-recycle-gate-design.md`. Read it, and `docs/tauri-shell.md`'s recycling paragraph (two hard-won invariants: rebuild on a LATER tick than destroy; the ExitRequested veto while `RECYCLING`), first.

**Worktree:** `/Users/thibaultdody/dev/worktrees/periscope/thi-449-webview-recycle`, branch `thibaultdody/thi-449-webview-recycle-never-fires-while-the-user-is-active-so-the`. Rust commands run in `src-tauri/` (`cargo test --quiet`: 30 tests at the start, ~40 s cold).

**Never** build into, copy over, quit, relaunch or signal the running app (`/Users/thibaultdody/github/tomplex/periscope/src-tauri/target/release/bundle/macos/Periscope.app`) or its WebContent processes — Task 6 asks first.

**Commits:** `area: what (THI-449)` single line, blank line, your model's `Co-Authored-By:` line; commit only the task's paths (`git commit -m … -- <paths>`).

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `src-tauri/src/focus.rs` | create | focus state + how long it has been lost |
| `src-tauri/src/notifications.rs` | modify | drop `FOCUSED`; read `focus::is_focused()` |
| `src-tauri/src/main.rs` | modify | `mod focus`; window events call `focus::set_focused` |
| `src-tauri/src/recycle.rs` | modify | gate + knobs, renderer age, shared-channel rebuild, focus-preserving restore, answered pid read, sweep, tests |
| `docs/tauri-shell.md`, `static/src/notify/persist.js` | modify | recycling paragraph; header comment |

---

### Task 1: `focus.rs`

**Files:** Create `src-tauri/src/focus.rs`; modify `src-tauri/src/notifications.rs` (~35, ~74–82, ~411), `src-tauri/src/main.rs` (mod list ~7–10, `on_window_event` ~136–148).

- [ ] **Step 1: Create the module with its test**

```rust
// Whether the dashboard window has focus, and since when it hasn't — kept by
// main.rs's window events. Read off the main thread by notification banners
// (quiet while focused; will_present runs on a thread UNUserNotificationCenter
// chooses) and by the recycle monitor (recycle once periscope is left). One
// mutex, so a reader never pairs "unfocused" with a stale time.

use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Debug)]
enum Focus {
    Unknown, // launch, before the first window event
    Focused,
    UnfocusedSince(Instant),
}

static FOCUS: Mutex<Focus> = Mutex::new(Focus::Unknown);

fn state() -> Focus {
    *FOCUS.lock().unwrap_or_else(|e| e.into_inner())
}

/// Only a real change moves the clock: a recycle's Destroyed after an earlier
/// Focused(false) keeps the unfocused time running.
pub fn set_focused(focused: bool) {
    let mut f = FOCUS.lock().unwrap_or_else(|e| e.into_inner());
    *f = match (*f, focused) {
        (_, true) => Focus::Focused,
        (Focus::UnfocusedSince(t), false) => Focus::UnfocusedSince(t),
        (_, false) => Focus::UnfocusedSince(Instant::now()),
    };
}

pub fn is_focused() -> bool {
    state() == Focus::Focused
}

/// How long periscope has been unfocused; None while focused or not yet known.
pub fn unfocused_for() -> Option<Duration> {
    match state() {
        Focus::UnfocusedSince(t) => Some(t.elapsed()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // One test: the state is process-global and cargo runs tests in parallel.
    #[test]
    fn tracks_focus_and_how_long_it_has_been_lost() {
        assert_eq!(unfocused_for(), None); // Unknown at start
        assert!(!is_focused());

        set_focused(true);
        assert!(is_focused());
        assert_eq!(unfocused_for(), None);

        set_focused(false);
        assert!(!is_focused());
        std::thread::sleep(Duration::from_millis(30));
        let first = unfocused_for().expect("unfocused");
        assert!(first >= Duration::from_millis(30));

        set_focused(false); // a recycle's Destroyed
        assert!(unfocused_for().expect("unfocused") >= first);

        set_focused(true);
        assert_eq!(unfocused_for(), None);
    }
}
```

- [ ] **Step 2: Rewire the callers**
  - `main.rs`: add `mod focus;` before `mod notifications;`. In `on_window_event` replace `notifications::set_focused(*focused)` and `notifications::set_focused(false)` with `focus::set_focused(…)`. In the comment above it, say the atomic now lives in `focus.rs` (keep the Destroyed explanation).
  - `notifications.rs`: delete the `FOCUSED` static with its doc comment and `pub fn set_focused`; drop `AtomicBool` from the `use std::sync::atomic::{…}` line (`AtomicUsize`, `Ordering` stay for `SEQ`); in `will_present` use `crate::focus::is_focused()`.

- [ ] **Step 3: Test** — `cargo test --quiet 2>&1 | tail -4` → 31 passed; `cargo build 2>&1 | grep -c '^warning'` → same count as before the change.

- [ ] **Step 4: Commit** — `git commit -m "shell: focus.rs owns the window's focus and how long it has been lost; notifications reads it (THI-449)" -- src-tauri/src/focus.rs src-tauri/src/notifications.rs src-tauri/src/main.rs`

---

### Task 2: the gate — unfocused, minimum age, 2 GB

**Files:** Modify `src-tauri/src/recycle.rs` (header comment, knobs, a renderer-born clock, `start_monitor`, `should_recycle` + tests).

- [ ] **Step 1: Failing tests** — append to `recycle.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    const GB: u64 = 1024 * 1024 * 1024;
    const MIN: u64 = 60;

    fn inputs(footprint: u64, age_s: u64, unfocused_s: Option<u64>, idle_s: u64) -> Inputs {
        Inputs {
            footprint,
            threshold: 2 * GB,
            age: Duration::from_secs(age_s),
            min_age: Duration::from_secs(30 * MIN),
            unfocused_for: unfocused_s.map(Duration::from_secs),
            unfocused_gate: Duration::from_secs(30),
            idle: Duration::from_secs(idle_s),
            idle_gate: Duration::from_secs(300),
        }
    }

    #[test]
    fn under_threshold_never_recycles() {
        assert!(!should_recycle(&inputs(2 * GB - 1, 3600, Some(3600), 3600)));
    }

    #[test]
    fn a_young_renderer_never_recycles() {
        // A fresh dashboard measures 1.1–1.6 GB at once; without this the gate loops.
        assert!(!should_recycle(&inputs(3 * GB, 30 * MIN - 1, Some(3600), 3600)));
    }

    #[test]
    fn unfocused_long_enough_recycles() {
        assert!(should_recycle(&inputs(3 * GB, 30 * MIN, Some(30), 0)));
        assert!(!should_recycle(&inputs(3 * GB, 30 * MIN, Some(29), 0)));
    }

    #[test]
    fn focused_or_unknown_waits_for_system_idle() {
        assert!(!should_recycle(&inputs(3 * GB, 30 * MIN, None, 299)));
        assert!(should_recycle(&inputs(3 * GB, 30 * MIN, None, 300)));
    }
}
```

`cargo test --quiet recycle 2>&1 | tail -3` → fails to compile (`Inputs`, `should_recycle`).

- [ ] **Step 2: Implement**

Knobs (replace `threshold_bytes`'s default and add two):

```rust
fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

// A fresh dashboard already measures 1.1–1.6 GB; leaked renderers reach 3–7.7.
fn threshold_bytes() -> u64 {
    (env_f64("PERISCOPE_RECYCLE_GB", 2.0) * 1024.0 * 1024.0 * 1024.0) as u64
}
```

and make `idle_gate_s` use `env_f64("PERISCOPE_RECYCLE_IDLE_S", 300.0)`; add `PERISCOPE_RECYCLE_UNFOCUSED_S` (30.0) and `PERISCOPE_RECYCLE_MIN_AGE_S` (1800.0) the same way. (One helper replaces the two copies of the parse.)

Renderer age:

```rust
// When the current renderer was built (the app's start, then each rebuild):
// D1's minimum age, so a fresh renderer that is already big can't loop.
static RENDERER_BORN: Mutex<Option<Instant>> = Mutex::new(None);

fn mark_renderer_born() {
    *RENDERER_BORN.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
}

fn renderer_age() -> Duration {
    RENDERER_BORN.lock().unwrap_or_else(|e| e.into_inner()).map_or(Duration::ZERO, |t| t.elapsed())
}
```

Policy:

```rust
struct Inputs {
    footprint: u64,
    threshold: u64,
    age: Duration,
    min_age: Duration,
    unfocused_for: Option<Duration>,
    unfocused_gate: Duration,
    idle: Duration,
    idle_gate: Duration,
}

/// Big enough, old enough, and either periscope has been left for another app
/// (the leak grows from input inside it) or the machine has gone idle.
fn should_recycle(i: &Inputs) -> bool {
    i.footprint >= i.threshold
        && i.age >= i.min_age
        && (i.unfocused_for.is_some_and(|d| d >= i.unfocused_gate) || i.idle >= i.idle_gate)
}
```

Imports: add `use std::sync::Mutex;` and `use std::time::Instant;` (keep `Duration`).

`start_monitor`: call `mark_renderer_born()` once before the loop; read the four knobs into `Duration`s (`Duration::from_secs_f64`) and log them in "monitor started (threshold … MB, min age …s, unfocused gate …s, idle gate …s)". Replace the block from `if fp < threshold {` through the "waiting for idle" `continue;` with:

```rust
            if fp < threshold {
                continue;
            }
            let inputs = Inputs {
                footprint: fp,
                threshold,
                age: renderer_age(),
                min_age,
                unfocused_for: crate::focus::unfocused_for(),
                unfocused_gate,
                idle: Duration::from_secs_f64(seconds_since_last_input().max(0.0)),
                idle_gate,
            };
            if !should_recycle(&inputs) {
                let focus = match inputs.unfocused_for {
                    Some(d) => format!("unfocused {}s", d.as_secs()),
                    None => "focused".to_string(),
                };
                shell_log(&format!(
                    "[recycle] over threshold ({} MB), renderer {}s old, {focus}, input {}s ago — waiting",
                    fp / (1024 * 1024),
                    inputs.age.as_secs(),
                    inputs.idle.as_secs()
                ));
                continue;
            }
```

Rewrite the header comment's "Policy:" paragraph to D1 (threshold 2 GB, min age 1800 s, unfocused 30 s OR idle 300 s, each with its env var), same length as now.

- [ ] **Step 3: Test** — `cargo test --quiet 2>&1 | tail -4` → all pass (35).
- [ ] **Step 4: Commit** — `git commit -m "shell: recycle once periscope has been unfocused 30 s, never within 30 min of a rebuild, above 2 GB (THI-449)" -- src-tauri/src/recycle.rs`

---

### Task 3: the rebuild — one channel, no focus theft

**Files:** Modify `src-tauri/src/recycle.rs` (`recycle_now`).

No unit test: this is main-thread/OS orchestration; Task 6 verifies it on the real app.

- [ ] **Step 1: Implement**
  - At the top of `recycle_now` (after the `RECYCLING.swap` guard), capture `let was_focused = crate::focus::is_focused();` — before `destroy()`, which sets focus to false.
  - In the spawned thread, create ONE channel before the loop, with this comment:

```rust
        // One channel for every attempt. The main thread runs the closures in
        // order, so an attempt that timed out but later built the window sends
        // its Ok ahead of the "already exists" errors of the attempts queued
        // behind it — 10-28 20:51 gave up on exactly that and leaked a 1.1 GB
        // renderer. An "already exists" that comes first is the OLD window still
        // registered (22 of 440 recycles), and the loop retries as before.
        let (tx, rx) = std::sync::mpsc::channel::<Result<(), String>>();
```

    Inside the loop drop the per-attempt `let (tx, rx) = …`, and clone into the closure: `let tx = tx.clone();`.
  - In the builder chain add `.focused(false)` (keep `.title(…)` and `.disable_drag_drop_handler()` with its comment).
  - Replace the `Ok(window) => { … }` body with:

```rust
                    Ok(window) => {
                        // Everything but visibility: the plugin's VISIBLE restore
                        // calls set_focus, i.e. activateIgnoringOtherApps — it would
                        // pull you out of the app you left periscope for.
                        let _ = window.restore_state(StateFlags::all() - StateFlags::VISIBLE);
                        let _ = window.show();
                        if was_focused {
                            let _ = window.set_focus();
                        }
                        // A fresh WKWebView starts at pageZoom 1.0.
                        crate::zoom::apply(&window);
                        WEBCONTENT_PID.store(0, Ordering::Relaxed); // re-learned next tick
                        mark_renderer_born();
                        Ok(())
                    }
```

    `was_focused` must be moved into the spawned thread and copied into each closure (it's `bool`, `Copy`).
  - If `StateFlags::all() - StateFlags::VISIBLE` doesn't compile with the plugin's bitflags version, use `StateFlags::all().difference(StateFlags::VISIBLE)`.

- [ ] **Step 2: Build** — `cargo build 2>&1 | grep -E '^(warning|error)'` → no new lines; `cargo test --quiet 2>&1 | tail -3` → all pass.
- [ ] **Step 3: Commit** — `git commit -m "shell: rebuild attempts share one result channel, and a rebuild only takes focus when periscope had it (THI-449)" -- src-tauri/src/recycle.rs`

---

### Task 4: answered pid read + the sweep

**Files:** Modify `src-tauri/src/recycle.rs` (replace `refresh_webcontent_pid` and `reap_old_webcontent`; `start_monitor`).

- [ ] **Step 1: Failing tests** — add inside `mod tests`:

```rust
    const MB: u64 = 1024 * 1024;

    #[test]
    fn sweep_takes_big_renderers_other_than_the_live_one() {
        let candidates = [(10, 2048 * MB), (11, 7 * MB), (12, 300 * MB), (13, 255 * MB)];
        assert_eq!(sweep_targets(&candidates, 10), vec![12]);
    }

    #[test]
    fn sweep_does_nothing_while_the_live_renderer_is_unknown() {
        assert!(sweep_targets(&[(12, 4096 * MB)], 0).is_empty());
    }
```

`cargo test --quiet sweep 2>&1 | tail -3` → fails to compile.

- [ ] **Step 2: Replace `refresh_webcontent_pid`** with a read that answers:

```rust
/// The live WebContent pid, read off the WKWebView on the main thread. 0 with
/// no `main` window or an OS without the selector; None when the main thread
/// didn't answer in time — a stale pid must never reach the sweep (wry reloads
/// the page itself when its WebContent dies, so the live pid can change
/// without a recycle).
fn read_webcontent_pid(app: &AppHandle) -> Option<i32> {
    let Some(window) = app.get_webview_window("main") else {
        return Some(0);
    };
    let (tx, rx) = std::sync::mpsc::channel::<i32>();
    window
        .with_webview(move |webview| {
            let wk = webview.inner() as *mut AnyObject;
            let mut pid = 0;
            if !wk.is_null() {
                unsafe {
                    let wk = &*wk;
                    let responds: bool = msg_send![wk, respondsToSelector: sel!(_webProcessIdentifier)];
                    if responds {
                        pid = msg_send![wk, _webProcessIdentifier];
                    }
                }
            }
            let _ = tx.send(pid);
        })
        .ok()?;
    rx.recv_timeout(Duration::from_secs(2)).ok()
}
```

- [ ] **Step 3: Replace `reap_old_webcontent`** (function and comment) with the sweep:

```rust
// WebKit parks a displaced WebContent in its process cache at full leaked
// size instead of exiting it (observed alive at 480MB 90s after its window
// was destroyed). Every check kills each WebContent this app is responsible
// for, other than the live one, at or above SWEEP_FLOOR — which spares
// WebKit's ~7 MB pre-warmed spare (the smallest displaced renderer the old
// reaper ever killed was 522 MB). One path covers a normal recycle, a rebuild
// that gave up, and anything else that leaves one behind.
const SWEEP_FLOOR: u64 = 256 * 1024 * 1024;

fn sweep_targets(candidates: &[(i32, u64)], live_pid: i32) -> Vec<i32> {
    if live_pid <= 0 {
        return Vec::new(); // the live renderer could be among them
    }
    candidates
        .iter()
        .filter(|(pid, footprint)| *pid != live_pid && *footprint >= SWEEP_FLOOR)
        .map(|(pid, _)| *pid)
        .collect()
}

extern "C" {
    fn proc_listallpids(buffer: *mut std::ffi::c_void, buffersize: i32) -> i32;
    // Private libSystem: the process macOS holds responsible — for a WebKit
    // helper, the app hosting the webview (periscope/resources.py uses it too).
    fn responsibility_get_pid_responsible_for_pid(pid: i32) -> i32;
}

/// (pid, footprint) of every WebContent process this app is responsible for.
fn app_webcontents() -> Vec<(i32, u64)> {
    let me = std::process::id() as i32;
    let mut pids = vec![0i32; 8192];
    // buffersize is in bytes; the return value is a count of pids.
    let n = unsafe {
        proc_listallpids(pids.as_mut_ptr() as *mut std::ffi::c_void, (pids.len() * 4) as i32)
    };
    pids.truncate(n.max(0) as usize);
    pids.into_iter()
        .filter(|&pid| pid > 0 && unsafe { responsibility_get_pid_responsible_for_pid(pid) } == me)
        .filter(|&pid| {
            let mut buf = [0u8; 4096];
            let len = unsafe {
                proc_pidpath(pid, buf.as_mut_ptr() as *mut std::ffi::c_void, buf.len() as u32)
            };
            len > 0 && String::from_utf8_lossy(&buf[..len as usize]).contains("WebKit.WebContent")
        })
        .filter_map(|pid| phys_footprint(pid).map(|fp| (pid, fp)))
        .collect()
}

fn sweep(live_pid: i32) {
    for pid in sweep_targets(&app_webcontents(), live_pid) {
        let fp = phys_footprint(pid).unwrap_or(0) / (1024 * 1024);
        unsafe { libc::kill(pid, libc::SIGKILL) };
        shell_log(&format!("[recycle] killed leftover WebContent {pid} ({fp} MB)"));
    }
}
```

Remove the `reap_old_webcontent(old_pid);` call from `recycle_now`'s success arm (`old_pid` stays for the "destroying" log line).

- [ ] **Step 4: Wire into `start_monitor`** — replace the refresh + 200 ms sleep + `WEBCONTENT_PID.load` with:

```rust
            let Some(pid) = read_webcontent_pid(&handle) else {
                continue; // main thread busy: no fresh pid, so no sweep and no recycle this tick
            };
            WEBCONTENT_PID.store(pid, Ordering::Relaxed);
            if !RECYCLING.load(Ordering::SeqCst) {
                sweep(pid);
            }
```

(the existing `let Some(fp) = phys_footprint(pid) else { continue; };` follows unchanged).

- [ ] **Step 5: Test** — `cargo test --quiet 2>&1 | tail -4` → all pass (37); `cargo build 2>&1 | grep -E '^(warning|error)'` → nothing new (no unused `run_on_main_thread` handle clones left behind).
- [ ] **Step 6: Commit** — `git commit -m "shell: every check sweeps leftover renderers the app owns, but only with a freshly read live pid; the after-recycle reaper goes (THI-449)" -- src-tauri/src/recycle.rs`

---

### Task 5: docs

**Files:** `docs/tauri-shell.md` (Webview recycling paragraph), `static/src/notify/persist.js` (header comment).

- [ ] **Step 1:** In `docs/tauri-shell.md`, rewrite the gate and reaper sentences to D1/D4/D5: recycles when the renderer's phys_footprint tops `PERISCOPE_RECYCLE_GB` (default 2.0), it has lived `PERISCOPE_RECYCLE_MIN_AGE_S` (default 1800 s) since the last rebuild, and either periscope has been unfocused `PERISCOPE_RECYCLE_UNFOCUSED_S` (default 30 s, `focus.rs`) or input has been idle `PERISCOPE_RECYCLE_IDLE_S` (300 s); the rebuild restores everything but visibility and takes focus only if periscope had it; rebuild attempts share one result channel; every check, with a freshly read live pid, SIGKILLs any other app-owned WebContent ≥ 256 MB. Keep both invariants and the `launchctl setenv` note (listing all four knobs).
- [ ] **Step 2:** In `static/src/notify/persist.js`'s header, change "recycle.rs rebuilds the webview once you've been idle a while" to "…once you've left periscope or been idle a while" (match the file's exact wording; JS comment only, so no `npm run build`).
- [ ] **Step 3: Commit** — `git commit -m "docs: tauri-shell and persist.js describe the new recycle gate, rebuild and sweep (THI-449)" -- docs/tauri-shell.md static/src/notify/persist.js`

---

### Task 6: build and verify on the real app (needs the user's go-ahead)

- [ ] **Step 1:** `cargo tauri build 2>&1 | tail -5` in the worktree's `src-tauri/` → bundle at the **worktree's** `src-tauri/target/release/bundle/macos/Periscope.app`. The running app is untouched.
- [ ] **Step 2: Ask the user** before swapping apps (quit the running Periscope.app, open the worktree bundle; single-instance means the old one must quit first). Quitting also ends today's leftover 41485.
- [ ] **Step 3: After the go-ahead:**
  - `launchctl setenv PERISCOPE_RECYCLE_GB 0.5; launchctl setenv PERISCOPE_RECYCLE_MIN_AGE_S 60; launchctl setenv PERISCOPE_RECYCLE_UNFOCUSED_S 10`;
  - `osascript -e 'quit app "Periscope"'`, then `open` the worktree bundle;
  - in `~/.config/periscope/shell.log` see "monitor started (threshold 512 MB, min age 60s, unfocused gate 10s, idle gate 300s)";
  - after ≥ 60 s, switch to the terminal for > 10 s: "destroying webview … webview rebuilt", and **the terminal stays frontmost**;
  - no second recycle within 60 s of the first;
  - View → Recycle Webview while periscope is frontmost leaves it frontmost;
  - minimize periscope for > 10 s: does it recycle? (record the answer);
  - every "killed leftover WebContent" pid differs from the live pid read just before (compare against `_webProcessIdentifier` via the next "destroying webview (WebContent pid …)" line).
  - Then `launchctl unsetenv` the three knobs and relaunch so the defaults apply.
- [ ] **Step 4:** Report results; ask before pushing and opening the PR (`THI-449: …`). After merge, rebuilding the long-lived app from the prod checkout is the same swap and needs the same go-ahead.
