// Webview recycling — the only cure for WKWebView's graphics-region leak.
//
// Real (trusted) user input mints IOSurface-backed regions in the WebContent
// process that nothing reclaims: not idle, not page reload (same process),
// not memory_pressure -l critical. Measured 2026-07-28: ~2.5 regions per rail
// click, 197 → 922 regions in an hour of use, 2645 regions ≈ 4.8GB after four
// days — the renderer was swapping the machine. Only WebContent process death
// frees them, and a reload reuses the process — so we destroy and recreate
// the webview window itself.
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

use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use objc2::runtime::AnyObject;
use objc2::{class, msg_send, sel};
use tauri::{AppHandle, Manager, WebviewUrl};
use tauri_plugin_window_state::{AppHandleExt, StateFlags, WindowExt};

const CHECK_INTERVAL_S: u64 = 60;
const DASHBOARD_URL: &str = "http://127.0.0.1:17374";

// Set while a recycle is mid-flight so the ExitRequested handler in main.rs
// can veto the "last window closed → quit" path between destroy and rebuild.
pub static RECYCLING: AtomicBool = AtomicBool::new(false);
static WEBCONTENT_PID: AtomicI32 = AtomicI32::new(0);

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

// A fresh dashboard already measures 1.1–1.6 GB; leaked renderers reach 3–7.7.
fn threshold_bytes() -> u64 {
    let gb = env_f64("PERISCOPE_RECYCLE_GB", 2.0);
    let gb = if gb.is_finite() && gb >= 0.0 { gb } else { 2.0 };
    (gb * 1024.0 * 1024.0 * 1024.0) as u64
}

// Duration::from_secs_f64 panics on a negative, NaN, infinite or overflowing
// value, and "inf"/"-1"/"1e30" all parse as f64 — a bad knob must not kill
// the monitor thread.
fn env_secs(name: &str, default_s: u64) -> Duration {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
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
    if pid <= 0 {
        return None;
    }
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

/// The live WebContent pid, read off the WKWebView on the main thread. 0 with
/// no `main` window or an OS without the selector; None with no fresh pid —
/// the window going away, with_webview failing, or no answer within the
/// timeout — a stale pid must never reach the sweep (wry reloads the page
/// itself when its WebContent dies, so the live pid can change without a
/// recycle).
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

// --- handing focus back after a rebuild (main thread) ---

// wry activates the app for every webview it creates (macOS 14+), and macOS
// honours it even while you're in another app: on 2026-11-01 a recycle fired
// with Claude frontmost and left Periscope frontmost 0.5 s later. So a recycle
// that started unfocused notes the frontmost app and activates it again.
fn frontmost_other_app() -> Option<i32> {
    unsafe {
        let workspace: *mut AnyObject = msg_send![class!(NSWorkspace), sharedWorkspace];
        let app: *mut AnyObject = msg_send![workspace, frontmostApplication];
        if app.is_null() {
            return None;
        }
        let pid: i32 = msg_send![app, processIdentifier];
        (pid != std::process::id() as i32).then_some(pid)
    }
}

fn activate_app(pid: i32) {
    unsafe {
        let app: *mut AnyObject =
            msg_send![class!(NSRunningApplication), runningApplicationWithProcessIdentifier: pid];
        if !app.is_null() {
            let _: bool = msg_send![app, activateWithOptions: 0usize];
        }
    }
}

// Destroy the sole window, then rebuild it on a later tick. Rebuilding the
// same label in the same main-thread closure as destroy() killed the whole
// app (observed 2026-07-28: process gone, no window) — the deferred rebuild
// plus the ExitRequested veto in main.rs covers the windowless gap.
pub fn recycle_now(app: &AppHandle) {
    if RECYCLING.swap(true, Ordering::SeqCst) {
        return; // one at a time
    }
    let was_focused = crate::focus::is_focused(); // before destroy() sets focus to false
    let hand_back_to = if was_focused { None } else { frontmost_other_app() };
    let old_pid = WEBCONTENT_PID.load(Ordering::Relaxed);
    let fp = phys_footprint(old_pid).unwrap_or(0);
    shell_log(&format!(
        "[recycle] destroying webview (WebContent pid {old_pid}, footprint {} MB)",
        fp / (1024 * 1024)
    ));
    // Persist the frame explicitly; the plugin's own save fires on exit, not
    // on destroy-and-rebuild.
    let _ = app.save_window_state(StateFlags::all());
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.destroy();
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        // One channel for every attempt. The main thread runs the closures in
        // order, so an attempt that timed out but later built the window sends
        // its Ok ahead of the "already exists" errors of the attempts queued
        // behind it — 10-28 20:51 gave up on exactly that and leaked a 1.1 GB
        // renderer. An "already exists" that comes first is the OLD window still
        // registered (22 of 440 recycles), and the loop retries as before.
        let (tx, rx) = std::sync::mpsc::channel::<Result<(), String>>();
        for attempt in 1..=10 {
            std::thread::sleep(Duration::from_millis(if attempt == 1 { 250 } else { 1000 }));
            let h = handle.clone();
            let tx = tx.clone();
            let ok = handle.run_on_main_thread(move || {
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
                let _ = tx.send(match result {
                    Ok(window) => {
                        // Everything but VISIBLE: the plugin's VISIBLE restore calls
                        // set_focus (activateIgnoringOtherApps: YES). wry's own
                        // activate() on every webview it builds still lands, so an
                        // unfocused recycle hands focus back once the rebuild is in.
                        let _ = window.restore_state(StateFlags::all() - StateFlags::VISIBLE);
                        if was_focused {
                            let _ = window.set_focus();
                        }
                        // A fresh WKWebView starts at pageZoom 1.0.
                        crate::zoom::apply(&window);
                        WEBCONTENT_PID.store(0, Ordering::Relaxed); // re-learned next tick
                        mark_renderer_born();
                        Ok(())
                    }
                    Err(e) => Err(e.to_string()),
                });
            });
            if ok.is_err() {
                shell_log("[recycle] run_on_main_thread failed; retrying");
                continue;
            }
            match rx.recv_timeout(Duration::from_secs(5)) {
                Ok(Ok(())) => {
                    shell_log("[recycle] webview rebuilt");
                    RECYCLING.store(false, Ordering::SeqCst);
                    if let Some(pid) = hand_back_to {
                        // After wry's own activate() has landed. Only while Periscope
                        // (wry's activation) or that app is frontmost: if you moved
                        // to a third app meanwhile, stay there.
                        std::thread::sleep(Duration::from_millis(500));
                        let _ = handle.run_on_main_thread(move || match frontmost_other_app() {
                            Some(other) if other != pid => {
                                shell_log(&format!("[recycle] left focus with pid {other}"))
                            }
                            _ => {
                                activate_app(pid);
                                shell_log(&format!("[recycle] handed focus back to pid {pid}"));
                            }
                        });
                    }
                    return;
                }
                // The channel is shared across every attempt, so a late Err can
                // surface here for an earlier attempt than `attempt` names.
                Ok(Err(e)) => shell_log(&format!("[recycle] a rebuild attempt failed: {e}")),
                Err(_) => shell_log(&format!("[recycle] rebuild attempt {attempt} timed out")),
            }
        }
        // Give up: clear the exit veto so the windowless app can quit normally.
        shell_log("[recycle] giving up after 10 attempts — app may now exit");
        RECYCLING.store(false, Ordering::SeqCst);
    });
}

// WebKit parks a displaced WebContent in its process cache at full leaked
// size instead of exiting it (observed alive at 480MB 90s after its window
// was destroyed). Every check kills each WebContent this app is responsible
// for, other than the live one, at or above SWEEP_FLOOR — which spares
// WebKit's ~7 MB pre-warmed spare (the smallest displaced renderer the old
// reaper ever killed was 522 MB). One path covers a normal recycle, a rebuild
// that gave up, and anything else that leaves one behind.
const SWEEP_FLOOR: u64 = 256 * 1024 * 1024;

fn sweep_targets(candidates: &[(i32, u64)], live_pid: i32) -> Vec<(i32, u64)> {
    if live_pid <= 0 {
        return Vec::new(); // the live renderer could be among them
    }
    candidates
        .iter()
        .copied()
        .filter(|(pid, footprint)| *pid != live_pid && *footprint >= SWEEP_FLOOR)
        .collect()
}

extern "C" {
    fn proc_listallpids(buffer: *mut std::ffi::c_void, buffersize: i32) -> i32;
    // Private libSystem: the process macOS holds responsible — for a WebKit
    // helper, the app hosting the webview (periscope/resources.py uses it too).
    fn responsibility_get_pid_responsible_for_pid(pid: i32) -> i32;
    fn proc_pidpath(pid: i32, buffer: *mut std::ffi::c_void, buffersize: u32) -> i32;
}

/// (pid, footprint) of every WebContent process this app is responsible for.
fn app_webcontents() -> Vec<(i32, u64)> {
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
        .filter_map(|pid| phys_footprint(pid).map(|fp| (pid, fp)))
        .collect()
}

// `renderer_born` is the value RENDERER_BORN held when the live pid was read;
// a recycle can finish between that read and a kill here, so every kill is
// rechecked against the live state instead of trusting the snapshot.
fn sweep(live_pid: i32, renderer_born: Option<Instant>) {
    for (pid, footprint) in sweep_targets(&app_webcontents(), live_pid) {
        if RECYCLING.load(Ordering::SeqCst) || renderer_born_at() != renderer_born {
            continue; // a rebuild landed since the pid read; this candidate may now be live
        }
        let fp = footprint / (1024 * 1024);
        unsafe { libc::kill(pid, libc::SIGKILL) };
        shell_log(&format!("[recycle] killed leftover WebContent {pid} ({fp} MB)"));
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

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_NEG", "-1");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_NEG", 7), Duration::from_secs(7));

        std::env::set_var("PERISCOPE_TEST_ENV_SECS_OK", "45");
        assert_eq!(env_secs("PERISCOPE_TEST_ENV_SECS_OK", 7), Duration::from_secs_f64(45.0));
    }

    #[test]
    fn threshold_bytes_falls_back_on_bad_gb() {
        std::env::set_var("PERISCOPE_RECYCLE_GB", "-1");
        assert_eq!(threshold_bytes(), (2.0 * 1024.0 * 1024.0 * 1024.0) as u64);
        std::env::remove_var("PERISCOPE_RECYCLE_GB");
    }

    const MB: u64 = 1024 * 1024;

    #[test]
    fn sweep_takes_big_renderers_other_than_the_live_one() {
        let candidates = [(10, 2048 * MB), (11, 7 * MB), (12, 300 * MB), (13, 255 * MB)];
        assert_eq!(sweep_targets(&candidates, 10), vec![(12, 300 * MB)]);
    }

    #[test]
    fn sweep_does_nothing_while_the_live_renderer_is_unknown() {
        assert!(sweep_targets(&[(12, 4096 * MB)], 0).is_empty());
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
            WEBCONTENT_PID.store(pid, Ordering::Relaxed);
            if !RECYCLING.load(Ordering::SeqCst) {
                sweep(pid, renderer_born);
            }
            let Some(fp) = phys_footprint(pid) else {
                continue;
            };
            if fp < threshold {
                continue;
            }
            let unfocused_for = crate::focus::unfocused_for();
            let is_focused = crate::focus::is_focused(); // read alongside unfocused_for, not later in the log path
            let inputs = Inputs {
                footprint: fp,
                threshold,
                age: renderer_age(),
                min_age,
                unfocused_for,
                unfocused_gate,
                idle: Duration::try_from_secs_f64(seconds_since_last_input()).unwrap_or_default(),
                idle_gate,
            };
            if !should_recycle(&inputs) {
                let focus = match inputs.unfocused_for {
                    Some(d) => format!("unfocused {}s", d.as_secs()),
                    None if is_focused => "focused".to_string(),
                    None => "focus unknown".to_string(),
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
