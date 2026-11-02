// Webview recycling — the only cure for WKWebView's graphics-region leak.
//
// Real (trusted) user input mints IOSurface-backed regions in the WebContent
// process that nothing reclaims: not idle, not page reload (same process),
// not memory_pressure -l critical. ~2.5 regions per rail click; 2645 regions
// ≈ 4.8GB after four days had the renderer swapping the machine. Only
// WebContent process death frees them, and a reload reuses the process — so
// we destroy and recreate the webview window itself.
//
// Policy: every CHECK_INTERVAL_S the renderer's phys_footprint is read via
// proc_pid_rusage (same number `footprint` prints; RSS is misleading — most
// of the leak compresses). Over PERISCOPE_RECYCLE_GB (default 2.0), the
// renderer at least PERISCOPE_RECYCLE_MIN_AGE_S old (default 1800s, so a
// fresh big renderer can't loop), and either unfocused for
// PERISCOPE_RECYCLE_UNFOCUSED_S (default 30s) or system input-idle for
// PERISCOPE_RECYCLE_IDLE_S (default 300s) → recycle. There is also a manual
// View → Recycle Webview item with no gate.
//
// The WebContent pid comes from WKWebView's private `_webProcessIdentifier`
// selector — exact, where pid-guessing via lsof breaks as soon as a browser
// tab is also open on :17374. Private API is acceptable here: personal
// debug-build app, guarded by respondsToSelector so an OS that drops the
// selector degrades to "manual recycle only" instead of crashing.

use std::collections::BTreeSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use objc2::runtime::AnyObject;
use objc2::{msg_send, sel};
use tauri::{AppHandle, Manager, WebviewUrl};
use tauri_plugin_window_state::{AppHandleExt, StateFlags};

use crate::focus::Focus;

const CHECK_INTERVAL_S: u64 = 60;
const DASHBOARD_URL: &str = "http://127.0.0.1:17374";

// Set while a recycle is mid-flight so the ExitRequested handler in main.rs
// can veto the "last window closed → quit" path between destroy and rebuild.
pub static RECYCLING: AtomicBool = AtomicBool::new(false);

// The WebContent pids the monitor has read as live: `last` names the renderer
// a recycle destroys, `seen` is what the sweep may kill once it isn't live.
struct LiveRenderers {
    last: Option<i32>,
    seen: BTreeSet<i32>,
}

static LIVE_RENDERERS: Mutex<LiveRenderers> = Mutex::new(LiveRenderers { last: None, seen: BTreeSet::new() });

fn live_renderers() -> std::sync::MutexGuard<'static, LiveRenderers> {
    LIVE_RENDERERS.lock().unwrap_or_else(|e| e.into_inner())
}

// A knob that isn't a finite, non-negative number falls back to its default
// ("inf", "NaN" and "-1" all parse as f64).
fn env_number(name: &str) -> Option<f64> {
    std::env::var(name).ok()?.parse::<f64>().ok().filter(|n| n.is_finite() && *n >= 0.0)
}

// A fresh dashboard already measures 1.1–1.6 GB; leaked renderers reach 3–7.7.
fn threshold_bytes() -> u64 {
    let gb = env_number("PERISCOPE_RECYCLE_GB").unwrap_or(2.0);
    (gb * 1024.0 * 1024.0 * 1024.0) as u64
}

// A finite value can still overflow a Duration ("1e30"), where from_secs_f64
// would panic the monitor thread.
fn env_secs(name: &str, default_s: u64) -> Duration {
    env_number(name)
        .and_then(|s| Duration::try_from_secs_f64(s).ok())
        .unwrap_or(Duration::from_secs(default_s))
}

// When the current renderer was built (the app's start, then each rebuild):
// PERISCOPE_RECYCLE_MIN_AGE_S's minimum age, so a fresh renderer that is
// already big can't loop.
static RENDERER_BORN: Mutex<Option<Instant>> = Mutex::new(None);

fn mark_renderer_born() {
    *RENDERER_BORN.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
}

fn renderer_born_at() -> Option<Instant> {
    *RENDERER_BORN.lock().unwrap_or_else(|e| e.into_inner())
}

fn renderer_age() -> Duration {
    renderer_born_at().map_or(Duration::ZERO, |t| t.elapsed())
}

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

// --- phys_footprint via proc_pid_rusage (flavor 0 already carries it) ---

#[repr(C)]
struct RusageInfoV0 {
    ri_uuid: [u8; 16],
    ri_user_time: u64,
    ri_system_time: u64,
    ri_pkg_idle_wkups: u64,
    ri_interrupt_wkups: u64,
    ri_pageins: u64,
    ri_wired_size: u64,
    ri_resident_size: u64,
    ri_phys_footprint: u64,
    ri_proc_start_abstime: u64,
    ri_proc_exit_abstime: u64,
}

extern "C" {
    fn proc_pid_rusage(pid: i32, flavor: i32, buffer: *mut RusageInfoV0) -> i32;
}

fn phys_footprint(pid: i32) -> Option<u64> {
    let mut info = RusageInfoV0 {
        ri_uuid: [0; 16],
        ri_user_time: 0,
        ri_system_time: 0,
        ri_pkg_idle_wkups: 0,
        ri_interrupt_wkups: 0,
        ri_pageins: 0,
        ri_wired_size: 0,
        ri_resident_size: 0,
        ri_phys_footprint: 0,
        ri_proc_start_abstime: 0,
        ri_proc_exit_abstime: 0,
    };
    let rc = unsafe { proc_pid_rusage(pid, 0, &mut info) };
    if rc == 0 {
        Some(info.ri_phys_footprint)
    } else {
        None
    }
}

// --- system input idle (no permissions required) ---

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGEventSourceSecondsSinceLastEventType(state_id: u32, event_type: u32) -> f64;
}

fn seconds_since_last_input() -> f64 {
    // 1 = kCGEventSourceStateHIDSystemState, !0 = kCGAnyInputEventType.
    unsafe { CGEventSourceSecondsSinceLastEventType(1, u32::MAX) }
}

// --- WebContent pid off the live WKWebView (the read dispatches to the main thread) ---

/// The live WebContent pid, read off the WKWebView on the main thread. None
/// with no `main` window, an OS without the selector, or no answer within the
/// timeout — a stale pid must never reach the sweep (wry reloads the page
/// itself when its WebContent dies, so the live pid can change without a
/// recycle).
fn read_webcontent_pid(app: &AppHandle) -> Option<i32> {
    let window = app.get_webview_window("main")?;
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
    rx.recv_timeout(Duration::from_secs(2)).ok().filter(|&pid| pid > 0)
}

// Append-only shell log — `open`-launched apps have no visible stderr, and
// the first version of this feature died silently for exactly that reason.
pub fn shell_log(msg: &str) {
    use std::io::Write;
    let path = dirs_path();
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(f, "{ts} {msg}");
    }
}

fn dirs_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    std::path::PathBuf::from(home).join(".config/periscope/shell.log")
}

// Destroy the sole window, then rebuild it on a later tick. Rebuilding the
// same label in the same main-thread closure as destroy() killed the whole
// app (process gone, no window) — the deferred rebuild plus the
// ExitRequested veto in main.rs covers the windowless gap.
pub fn recycle_now(app: &AppHandle) {
    if RECYCLING.swap(true, Ordering::SeqCst) {
        return; // one at a time
    }
    let old_pid = live_renderers().last;
    let fp = old_pid.and_then(phys_footprint).unwrap_or(0);
    shell_log(&format!(
        "[recycle] destroying webview (WebContent pid {}, footprint {} MB)",
        old_pid.map_or("unknown".to_string(), |pid| pid.to_string()),
        fp / (1024 * 1024)
    ));
    // Persist the frame for the plugin to restore on the rebuilt window; its
    // own save fires on exit only.
    let _ = app.save_window_state(StateFlags::all());
    let was_minimized = match app.get_webview_window("main") {
        Some(window) => {
            let minimized = window.is_minimized().unwrap_or(false);
            let _ = window.destroy();
            minimized
        }
        None => false,
    };
    let handle = app.clone();
    std::thread::spawn(move || {
        let submit = |attempt: u32, tx: Sender<Result<(), String>>| {
            std::thread::sleep(Duration::from_millis(if attempt == 1 { 250 } else { 1000 }));
            let h = handle.clone();
            let queued = handle.run_on_main_thread(move || {
                let result = tauri::WebviewWindowBuilder::new(
                    &h,
                    "main",
                    WebviewUrl::External(DASHBOARD_URL.parse().expect("static url parses")),
                )
                .title("Periscope")
                // tauri.conf.json's window config is read ONLY for the windows
                // Tauri builds at startup — a WebviewWindowBuilder starts from
                // WebviewAttributes::default(), where drag_drop_handler_enabled
                // is TRUE. That handler makes the webview swallow OS drag
                // events, killing every HTML5 drag-and-drop in the page (309ad35
                // set dragDropEnabled=false for exactly this). So the first
                // recycle silently broke rail reordering until the app was
                // relaunched.
                .disable_drag_drop_handler()
                .build();
                // By now the window-state plugin has restored the frame (main.rs).
                // Nothing in the rebuild activates the app — wry's own activate()
                // is declined while another app is frontmost — so keystrokes stay
                // with the app you're in.
                let _ = tx.send(match result {
                    Ok(window) => {
                        // The builder makes a shown window, and the plugin has no
                        // minimized state to restore.
                        if was_minimized {
                            let _ = window.minimize();
                        }
                        // A fresh WKWebView starts at pageZoom 1.0.
                        crate::zoom::apply(&window);
                        live_renderers().last = None; // re-learned next tick
                        mark_renderer_born();
                        Ok(())
                    }
                    Err(e) => Err(e.to_string()),
                });
            });
            if queued.is_err() {
                shell_log("[recycle] run_on_main_thread failed; retrying");
            }
            queued.is_ok()
        };
        let log = |msg: &str| shell_log(&format!("[recycle] {msg}"));
        if await_rebuild(REBUILD_ATTEMPTS, Duration::from_secs(5), submit, log) {
            shell_log("[recycle] webview rebuilt");
        } else {
            shell_log(&format!("[recycle] giving up after {REBUILD_ATTEMPTS} attempts — app may now exit"));
        }
        // Clears the exit veto too: after a give-up the windowless app can quit.
        RECYCLING.store(false, Ordering::SeqCst);
    });
}

const REBUILD_ATTEMPTS: u32 = 10;

/// Submits rebuild attempts until one reports Ok; false after `attempts`
/// without one. Every attempt sends on one channel: the main thread runs them
/// in order, so an attempt that timed out but later built the window sends its
/// Ok ahead of the "already exists" errors of the attempts queued behind it.
/// An "already exists" that comes first is the old window still registered,
/// and the next attempt retries. `submit` returns whether it queued the attempt.
fn await_rebuild(
    attempts: u32,
    timeout: Duration,
    mut submit: impl FnMut(u32, Sender<Result<(), String>>) -> bool,
    log: impl Fn(&str),
) -> bool {
    let (tx, rx) = std::sync::mpsc::channel();
    for attempt in 1..=attempts {
        if !submit(attempt, tx.clone()) {
            continue;
        }
        match rx.recv_timeout(timeout) {
            Ok(Ok(())) => return true,
            // A late Err can belong to an earlier attempt than `attempt` names.
            Ok(Err(e)) => log(&format!("a rebuild attempt failed: {e}")),
            Err(_) => log(&format!("rebuild attempt {attempt} timed out")),
        }
    }
    false
}

// WebKit parks a displaced WebContent in its process cache at full leaked
// size instead of exiting it (observed alive at 480MB 90s after its window
// was destroyed). Every check kills each WebContent this app is responsible
// for that the monitor read as live on an earlier tick and that isn't live
// now — never WebKit's pre-warmed spare or another renderer the page never
// ran in. The live pid is spared even when seen before: WebKit's process
// cache can hand a parked renderer back to a new webview. One path covers a
// normal recycle, a rebuild that gave up, and anything else that leaves one
// behind.

/// The candidates in `seen` other than `live_pid`; drops the pids that are no
/// longer candidates from `seen`.
fn sweep_targets(candidates: &[i32], seen: &mut BTreeSet<i32>, live_pid: i32) -> Vec<i32> {
    seen.retain(|pid| candidates.contains(pid));
    seen.iter().copied().filter(|&pid| pid != live_pid).collect()
}

extern "C" {
    fn proc_listallpids(buffer: *mut std::ffi::c_void, buffersize: i32) -> i32;
    // Private libSystem: the process macOS holds responsible — for a WebKit
    // helper, the app hosting the webview (periscope/resources.py uses it too).
    fn responsibility_get_pid_responsible_for_pid(pid: i32) -> i32;
    fn proc_pidpath(pid: i32, buffer: *mut std::ffi::c_void, buffersize: u32) -> i32;
}

/// Every WebContent process this app is responsible for.
fn app_webcontents() -> Vec<i32> {
    let me = std::process::id() as i32;
    let mut pids = vec![0i32; 8192];
    // buffersize is in bytes; the return value is a count of pids.
    let n = unsafe {
        proc_listallpids(
            pids.as_mut_ptr() as *mut std::ffi::c_void,
            std::mem::size_of_val(pids.as_slice()) as i32,
        )
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
        .collect()
}

/// Kills each target while `still_valid` holds, rechecked before every kill: a
/// recycle can finish mid-sweep and make a target the live renderer.
fn sweep(targets: &[i32], still_valid: impl Fn() -> bool, mut kill: impl FnMut(i32)) {
    for &pid in targets {
        if !still_valid() {
            return;
        }
        kill(pid);
    }
}

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

    #[test]
    fn env_secs_falls_back_on_bad_values() {
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_UNSET", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_INF", "inf");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_INF", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_NAN", "NaN");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_NAN", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_HUGE", "1e30");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_HUGE", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_NEG", "-1");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_NEG", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_OK", "45");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_OK", 7), Duration::from_secs_f64(45.0));
    }

    // One test: PERISCOPE_RECYCLE_GB is process-global and cargo runs tests in parallel.
    #[test]
    fn threshold_bytes_defaults_to_2_gb_and_falls_back_on_bad_gb() {
        std::env::remove_var("PERISCOPE_RECYCLE_GB");
        assert_eq!(threshold_bytes(), 2 * GB);
        for bad in ["-1", "inf", "NaN"] {
            std::env::set_var("PERISCOPE_RECYCLE_GB", bad);
            assert_eq!(threshold_bytes(), 2 * GB, "PERISCOPE_RECYCLE_GB={bad}");
        }
        std::env::set_var("PERISCOPE_RECYCLE_GB", "0.5");
        assert_eq!(threshold_bytes(), GB / 2);
        std::env::remove_var("PERISCOPE_RECYCLE_GB");
    }

    #[test]
    fn sweep_takes_renderers_seen_live_that_are_no_longer_live() {
        let mut seen = BTreeSet::from([10, 12]);
        assert_eq!(sweep_targets(&[10, 11, 12], &mut seen, 10), vec![12]);
    }

    #[test]
    fn sweep_never_takes_the_live_renderer() {
        // WebKit's process cache can hand a parked renderer back to a new webview.
        let mut seen = BTreeSet::from([10]);
        assert!(sweep_targets(&[10], &mut seen, 10).is_empty());
    }

    #[test]
    fn sweep_never_takes_a_renderer_it_has_not_seen_live() {
        let mut seen = BTreeSet::new();
        assert!(sweep_targets(&[11], &mut seen, 10).is_empty());
    }

    #[test]
    fn sweep_forgets_renderers_that_are_gone() {
        let mut seen = BTreeSet::from([10, 12, 99]);
        assert_eq!(sweep_targets(&[10, 12], &mut seen, 10), vec![12]);
        assert_eq!(seen, BTreeSet::from([10, 12]));
    }

    const ALREADY_EXISTS: &str = "a webview with label `main` already exists";
    const SHORT: Duration = Duration::from_millis(10);

    #[test]
    fn a_late_ok_from_a_timed_out_attempt_is_a_rebuild() {
        let mut first = None;
        let rebuilt = await_rebuild(
            REBUILD_ATTEMPTS,
            SHORT,
            |attempt, tx| {
                match attempt {
                    1 => first = Some(tx), // times out
                    2 => {
                        // The main thread gets to attempt 1, then attempt 2 finds its window.
                        let _ = first.take().expect("attempt 1's sender").send(Ok(()));
                        let _ = tx.send(Err(ALREADY_EXISTS.into()));
                    }
                    _ => {
                        let _ = tx.send(Err(ALREADY_EXISTS.into()));
                    }
                }
                true
            },
            |_| {},
        );
        assert!(rebuilt);
    }

    #[test]
    fn an_old_window_still_registered_is_retried() {
        let mut submitted = 0;
        let rebuilt = await_rebuild(
            REBUILD_ATTEMPTS,
            SHORT,
            |attempt, tx| {
                submitted += 1;
                let _ = tx.send(if attempt == 1 { Err(ALREADY_EXISTS.into()) } else { Ok(()) });
                true
            },
            |_| {},
        );
        assert!(rebuilt);
        assert_eq!(submitted, 2);
    }

    #[test]
    fn a_rebuild_gives_up_after_every_attempt_fails() {
        let mut submitted = 0;
        let rebuilt = await_rebuild(
            REBUILD_ATTEMPTS,
            SHORT,
            |_, tx| {
                submitted += 1;
                let _ = tx.send(Err("boom".into()));
                true
            },
            |_| {},
        );
        assert!(!rebuilt);
        assert_eq!(submitted, REBUILD_ATTEMPTS);
    }

    #[test]
    fn sweep_stops_killing_once_the_recheck_fails() {
        let valid = std::cell::Cell::new(true);
        let mut killed = Vec::new();
        sweep(
            &[12, 13, 14],
            || valid.get(),
            |pid| {
                killed.push(pid);
                valid.set(false); // a rebuild landed after the first kill
            },
        );
        assert_eq!(killed, vec![12]);
    }
}

pub fn start_monitor(app: &AppHandle) {
    mark_renderer_born();
    let handle = app.clone();
    std::thread::spawn(move || {
        let threshold = threshold_bytes();
        let min_age = env_secs("PERISCOPE_RECYCLE_MIN_AGE_S", 1800);
        let unfocused_gate = env_secs("PERISCOPE_RECYCLE_UNFOCUSED_S", 30);
        let idle_gate = env_secs("PERISCOPE_RECYCLE_IDLE_S", 300);
        shell_log(&format!(
            "[recycle] monitor started (threshold {} MB, min age {}s, unfocused gate {}s, idle gate {}s)",
            threshold / (1024 * 1024),
            min_age.as_secs(),
            unfocused_gate.as_secs(),
            idle_gate.as_secs()
        ));
        loop {
            std::thread::sleep(Duration::from_secs(CHECK_INTERVAL_S));
            let renderer_born = renderer_born_at(); // snapshot before the pid read; sweep rechecks against it
            let Some(pid) = read_webcontent_pid(&handle) else {
                continue; // no fresh pid: no sweep and no recycle this tick
            };
            if !RECYCLING.load(Ordering::SeqCst) {
                let targets = sweep_targets(&app_webcontents(), &mut live_renderers().seen, pid);
                sweep(
                    &targets,
                    || !RECYCLING.load(Ordering::SeqCst) && renderer_born_at() == renderer_born,
                    |target| {
                        let fp = phys_footprint(target).unwrap_or(0) / (1024 * 1024);
                        unsafe { libc::kill(target, libc::SIGKILL) };
                        shell_log(&format!("[recycle] killed leftover WebContent {target} ({fp} MB)"));
                    },
                );
            }
            {
                let mut renderers = live_renderers();
                renderers.last = Some(pid);
                renderers.seen.insert(pid);
            }
            let Some(fp) = phys_footprint(pid) else {
                continue;
            };
            if fp < threshold {
                continue;
            }
            let focus = crate::focus::focus(); // one snapshot for the gate and the log line
            let inputs = Inputs {
                footprint: fp,
                threshold,
                age: renderer_age(),
                min_age,
                unfocused_for: focus.unfocused_for(),
                unfocused_gate,
                idle: Duration::try_from_secs_f64(seconds_since_last_input()).unwrap_or_default(),
                idle_gate,
            };
            if !should_recycle(&inputs) {
                let focus = match focus {
                    Focus::UnfocusedSince(t) => format!("unfocused {}s", t.elapsed().as_secs()),
                    Focus::Focused => "focused".to_string(),
                    Focus::Unknown => "focus unknown".to_string(),
                };
                shell_log(&format!(
                    "[recycle] over threshold ({} MB), renderer {}s old, {focus}, input {}s ago — waiting",
                    fp / (1024 * 1024),
                    inputs.age.as_secs(),
                    inputs.idle.as_secs()
                ));
                continue;
            }
            let h = handle.clone();
            let _ = handle.run_on_main_thread(move || recycle_now(&h));
        }
    });
}
