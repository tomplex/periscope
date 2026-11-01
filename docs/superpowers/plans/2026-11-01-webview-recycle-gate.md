# Webview Recycle Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Tauri shell recycles the leaking webview once periscope has been unfocused for 30 s (not only after 5 min of no input anywhere), treats an "already exists" rebuild as success, and sweeps leftover WebContent processes every check (THI-449).

**Architecture:** A new `focus.rs` owns the window's focus state and how long it has been unfocused; `notifications.rs` and `recycle.rs` both read it. `recycle.rs` gets three pure, unit-tested policy functions (`should_recycle`, `rebuilt_by_earlier_attempt`, `sweep_targets`) and thin OS glue around them; the 10 s after-recycle reaper is replaced by a per-check sweep.

**Tech Stack:** Rust (Tauri 2, objc2, libc), macOS libproc / libSystem.

**Spec:** `docs/superpowers/specs/2026-11-01-webview-recycle-gate-design.md`. Read it, and `docs/tauri-shell.md`'s recycling paragraph, first.

**Worktree:** `/Users/thibaultdody/dev/worktrees/periscope/thi-449-webview-recycle`, branch `thibaultdody/thi-449-webview-recycle-never-fires-while-the-user-is-active-so-the`. Rust commands run in `src-tauri/`.

**Never** build into, copy over, quit or relaunch the running app (`/Users/thibaultdody/github/tomplex/periscope/src-tauri/target/release/bundle/macos/Periscope.app`) — that is the user's live app; Task 6 asks first.

**Commits:** one per task step marked Commit; `area: what (THI-449)` single line, blank line, your model's `Co-Authored-By:` line. Commit only the task's paths (`git commit -m … -- <paths>`).

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `src-tauri/src/focus.rs` | create | focus state + time since it last changed |
| `src-tauri/src/notifications.rs` | modify | drop its own `FOCUSED`; read `focus::is_focused()` |
| `src-tauri/src/main.rs` | modify | `mod focus`; window events call `focus::set_focused` |
| `src-tauri/src/recycle.rs` | modify | unfocused gate, "already exists" success, sweep replacing the reaper, policy fns + tests |
| `docs/tauri-shell.md` | modify | recycling paragraph |

---

### Task 1: `focus.rs` — one focus signal with a clock

**Files:** Create `src-tauri/src/focus.rs`; modify `src-tauri/src/notifications.rs:35,74-82,411`, `src-tauri/src/main.rs:7-10,136-148`.

- [ ] **Step 1: Write the module with its test**

Create `src-tauri/src/focus.rs`:

```rust
// Whether the dashboard window has focus, and since when — kept by main.rs's
// window events. Two readers on threads of their own: notification banners
// (quiet while focused; will_present runs on a thread UNUserNotificationCenter
// chooses) and the recycle monitor (recycle once unfocused long enough).
// Atomics answer both without a round trip to the main thread.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

static FOCUSED: AtomicBool = AtomicBool::new(false);
// ms since START of the last change; 0 = unfocused since launch.
static CHANGED_AT_MS: AtomicU64 = AtomicU64::new(0);
static START: OnceLock<Instant> = OnceLock::new();

fn now_ms() -> u64 {
    START.get_or_init(Instant::now).elapsed().as_millis() as u64
}

/// Only a real change moves the clock: a recycle's Destroyed after an earlier
/// Focused(false) must not restart the unfocused time.
pub fn set_focused(focused: bool) {
    if FOCUSED.swap(focused, Ordering::Relaxed) != focused {
        CHANGED_AT_MS.store(now_ms(), Ordering::Relaxed);
    }
}

pub fn is_focused() -> bool {
    FOCUSED.load(Ordering::Relaxed)
}

/// How long the window has been unfocused; None while it has focus.
pub fn unfocused_for() -> Option<Duration> {
    if is_focused() {
        return None;
    }
    Some(Duration::from_millis(now_ms().saturating_sub(CHANGED_AT_MS.load(Ordering::Relaxed))))
}

#[cfg(test)]
mod tests {
    use super::*;

    // One test: the state is process-global, and cargo runs tests in parallel.
    #[test]
    fn tracks_focus_and_how_long_it_has_been_lost() {
        set_focused(true);
        assert!(is_focused());
        assert_eq!(unfocused_for(), None);

        set_focused(false);
        assert!(!is_focused());
        std::thread::sleep(Duration::from_millis(30));
        let first = unfocused_for().expect("unfocused");
        assert!(first >= Duration::from_millis(30));

        // A repeated false (a recycle's Destroyed) keeps the clock running.
        set_focused(false);
        assert!(unfocused_for().expect("unfocused") >= first);

        set_focused(true);
        assert_eq!(unfocused_for(), None);
    }
}
```

- [ ] **Step 2: Rewire the callers**

`src-tauri/src/main.rs`: add `mod focus;` to the `mod` list (alphabetical, before `mod notifications;`), and in `on_window_event` replace the two `notifications::set_focused(…)` calls with `focus::set_focused(…)` (same arguments: `*focused` and `false`). Keep the comment above `on_window_event`, but make its first clause say the atomic lives in `focus.rs`.

`src-tauri/src/notifications.rs`: delete the `FOCUSED` static, its doc comment and `pub fn set_focused` (lines ~74–82); remove `AtomicBool` from the `use std::sync::atomic::{…}` import; in `will_present` replace `FOCUSED.load(Ordering::Relaxed)` with `crate::focus::is_focused()`.

- [ ] **Step 3: Build and test**

Run (in `src-tauri/`): `cargo test --quiet 2>&1 | tail -5`
Expected: all tests pass, including `focus::tests::tracks_focus_and_how_long_it_has_been_lost`; no `unused import` warnings (`cargo build 2>&1 | grep -c warning` → 0 new warnings vs. before).

- [ ] **Step 4: Commit**

```bash
git commit -m "shell: focus.rs owns the window's focus and how long it has been lost; notifications reads it (THI-449)" -- src-tauri/src/focus.rs src-tauri/src/notifications.rs src-tauri/src/main.rs
```

---

### Task 2: the unfocused gate

**Files:** Modify `src-tauri/src/recycle.rs` (header comment, env knobs, `start_monitor`, new `should_recycle` + tests).

- [ ] **Step 1: Write the failing tests**

Append to `src-tauri/src/recycle.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    const GB: u64 = 1024 * 1024 * 1024;

    fn recycles(footprint: u64, unfocused_s: Option<u64>, idle_s: f64) -> bool {
        should_recycle(footprint, GB, unfocused_s.map(Duration::from_secs), idle_s, 30.0, 300.0)
    }

    #[test]
    fn under_threshold_never_recycles() {
        assert!(!recycles(GB - 1, Some(3600), 3600.0));
    }

    #[test]
    fn unfocused_long_enough_recycles() {
        assert!(recycles(2 * GB, Some(30), 0.0));
        assert!(!recycles(2 * GB, Some(29), 0.0));
    }

    #[test]
    fn focused_waits_for_system_idle() {
        assert!(!recycles(2 * GB, None, 299.0));
        assert!(recycles(2 * GB, None, 300.0));
    }
}
```

Run: `cargo test --quiet recycle 2>&1 | tail -5` — Expected: FAIL to compile, `cannot find function should_recycle`.

- [ ] **Step 2: Implement**

In `recycle.rs`, next to `idle_gate_s()`:

```rust
fn unfocused_gate_s() -> f64 {
    std::env::var("PERISCOPE_RECYCLE_UNFOCUSED_S")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(30.0)
}

/// Over the threshold, and either periscope has been left for another app
/// (the leak grows from input inside it, so leaving is the moment) or the
/// whole machine has gone idle (periscope left frontmost while you're away).
fn should_recycle(
    footprint: u64,
    threshold: u64,
    unfocused_for: Option<Duration>,
    idle_s: f64,
    unfocused_gate_s: f64,
    idle_gate_s: f64,
) -> bool {
    footprint >= threshold
        && (unfocused_for.is_some_and(|d| d.as_secs_f64() >= unfocused_gate_s) || idle_s >= idle_gate_s)
}
```

In `start_monitor`: read `let unfocused_needed = unfocused_gate_s();` next to `idle_needed`, include it in the "monitor started" log (`… idle gate {idle_needed}s, unfocused gate {unfocused_needed}s`), and replace the block from `if fp < threshold {` through the "waiting for idle" `continue;` with:

```rust
            if fp < threshold {
                continue;
            }
            let unfocused = crate::focus::unfocused_for();
            let idle = seconds_since_last_input();
            if !should_recycle(fp, threshold, unfocused, idle, unfocused_needed, idle_needed) {
                let focus = match unfocused {
                    Some(d) => format!("unfocused {}s", d.as_secs()),
                    None => "focused".to_string(),
                };
                shell_log(&format!(
                    "[recycle] over threshold ({} MB) but {focus}, input {}s ago — waiting",
                    fp / (1024 * 1024),
                    idle as u64
                ));
                continue;
            }
```

Update the file's header comment "Policy:" paragraph: over `PERISCOPE_RECYCLE_GB` (default 1.0) AND (periscope unfocused for `PERISCOPE_RECYCLE_UNFOCUSED_S`, default 30 s, OR system input idle for `PERISCOPE_RECYCLE_IDLE_S`, default 300 s). Keep it to the paragraph's current length.

- [ ] **Step 3: Test**

Run: `cargo test --quiet 2>&1 | tail -5` — Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git commit -m "shell: recycle once periscope has been unfocused 30 s, not only after 5 min of no input anywhere (THI-449)" -- src-tauri/src/recycle.rs
```

---

### Task 3: an "already exists" rebuild counts as rebuilt

**Files:** Modify `src-tauri/src/recycle.rs` (`recycle_now`'s rebuild loop, new `rebuilt_by_earlier_attempt` + test).

- [ ] **Step 1: Write the failing test** — add inside `mod tests`:

```rust
    #[test]
    fn already_exists_means_an_earlier_attempt_landed() {
        // Tauri's text, as logged on 2026-10-28 20:51.
        assert!(rebuilt_by_earlier_attempt("a webview with label `main` already exists"));
        assert!(!rebuilt_by_earlier_attempt("window creation failed"));
    }
```

Run: `cargo test --quiet already_exists 2>&1 | tail -3` — Expected: FAIL to compile.

- [ ] **Step 2: Implement**

```rust
/// A rebuild attempt that timed out still runs on the main thread and builds
/// the window; the next attempt then fails with this. Seen 2026-10-28 20:51:
/// three timeouts, the page loaded, seven "already exists", give-up — and the
/// old renderer was never killed.
fn rebuilt_by_earlier_attempt(err: &str) -> bool {
    err.contains("already exists")
}
```

In the rebuild loop's `match rx.recv_timeout(…)`, add an arm before the generic `Ok(Err(e))` one:

```rust
                Ok(Err(e)) if rebuilt_by_earlier_attempt(&e) => {
                    shell_log("[recycle] webview rebuilt (an earlier attempt landed)");
                    RECYCLING.store(false, Ordering::SeqCst);
                    return;
                }
```

(Task 4 removes the `reap_old_webcontent(old_pid)` call from the plain-success arm; until then leave it.)

- [ ] **Step 3: Test** — `cargo test --quiet 2>&1 | tail -5` — Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git commit -m "shell: a rebuild that finds the window already built counts as rebuilt, not a give-up that leaks the old renderer (THI-449)" -- src-tauri/src/recycle.rs
```

---

### Task 4: sweep leftover WebContents every check

**Files:** Modify `src-tauri/src/recycle.rs` (replace `reap_old_webcontent`; new `sweep_targets`, `app_webcontents`, `sweep`; call from `start_monitor`).

- [ ] **Step 1: Write the failing test** — add inside `mod tests`:

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

Run: `cargo test --quiet sweep 2>&1 | tail -3` — Expected: FAIL to compile.

- [ ] **Step 2: Implement**

Replace the whole `reap_old_webcontent` function (and its comment) with:

```rust
// WebKit parks a displaced WebContent in its process cache at full leaked
// size instead of exiting it (observed alive at 480MB 90s after its window
// was destroyed). Every check kills each WebContent this app is responsible
// for, other than the live one, at or above SWEEP_FLOOR. The floor leaves
// WebKit's pre-warmed spare (~7 MB) alone; every displaced renderer in the
// log was ≥ 700 MB. One path covers a normal recycle, a rebuild that gave
// up, and anything else that leaves one behind.
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

Remove the `reap_old_webcontent(old_pid);` call in `recycle_now`'s success arm. `old_pid` is still used for the "destroying webview" log line — keep it.

In `start_monitor`, right after `let pid = WEBCONTENT_PID.load(Ordering::Relaxed);`, add:

```rust
            // Mid-recycle the stored pid is the renderer being torn down.
            if !RECYCLING.load(Ordering::SeqCst) {
                sweep(pid);
            }
```

- [ ] **Step 3: Test** — `cargo test --quiet 2>&1 | tail -5` and `cargo build 2>&1 | grep -E '^(warning|error)' | sort | uniq -c` — Expected: all pass; no new warnings (in particular no "unused" for `proc_pidpath` or `old_pid`).

- [ ] **Step 4: Commit**

```bash
git commit -m "shell: every check kills leftover WebContent renderers the app owns, replacing the after-recycle reaper (THI-449)" -- src-tauri/src/recycle.rs
```

---

### Task 5: docs

**Files:** Modify `docs/tauri-shell.md` (the **Webview recycling** paragraph).

- [ ] **Step 1: Rewrite the gate and reaper sentences** so the paragraph says: the shell recycles when the renderer's phys_footprint tops `PERISCOPE_RECYCLE_GB` (default 1.0) AND either periscope has been unfocused for `PERISCOPE_RECYCLE_UNFOCUSED_S` (default 30 s, tracked in `focus.rs`) or system input has been idle `PERISCOPE_RECYCLE_IDLE_S` (default 300 s); a rebuild attempt that fails with "already exists" counts as rebuilt (a timed-out earlier attempt built it); and every check SIGKILLs any WebContent the app is responsible for, other than the live one, at ≥ 256 MB (WebKit parks displaced renderers at full size; the floor spares its ~7 MB pre-warmed one). Keep the two existing invariants (later-tick rebuild + ExitRequested veto) and the `launchctl setenv` testing note; add `PERISCOPE_RECYCLE_UNFOCUSED_S` to it.

- [ ] **Step 2: Commit**

```bash
git commit -m "docs: tauri-shell describes the unfocused recycle gate and the leftover-renderer sweep (THI-449)" -- docs/tauri-shell.md
```

---

### Task 6: build and verify on the real app (needs the user's go-ahead)

- [ ] **Step 1: Release build in the worktree** (its own `target/`; the running app is untouched):

Run (in `src-tauri/`): `cargo tauri build 2>&1 | tail -5`
Expected: a bundle at `src-tauri/target/release/bundle/macos/Periscope.app` inside the **worktree**.

- [ ] **Step 2: Ask the user** before swapping apps: quitting the running Periscope.app and opening the worktree's bundle. The single-instance plugin focuses an already-running app instead of starting a second one, so the old one must quit first.

- [ ] **Step 3: After the go-ahead** — quit the running app (`osascript -e 'quit app "Periscope"'`), `open` the worktree bundle, then verify from `~/.config/periscope/shell.log`:
  - `monitor started (threshold 1024 MB, idle gate 300s, unfocused gate 30s)`;
  - within ~60 s: `killed leftover WebContent 41485 (… MB)` if it is still alive (`ps -p 41485`);
  - while over 1 GB, switching to another app for > 30 s produces `destroying webview … webview rebuilt`; while focused it logs `… but focused, input …s ago — waiting`.
  - Check whether minimizing the window produces a recycle (spec assumption: tao sends `Focused(false)`); record the answer in the PR.

- [ ] **Step 4: Hand off** — report results; ask before pushing and opening the PR (title `THI-449: …`). After merge, the user's long-lived app is rebuilt from the prod checkout (`cargo tauri build` there), which is the same swap and needs the same go-ahead.
