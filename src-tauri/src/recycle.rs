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
// of the leak compresses). Over PERISCOPE_RECYCLE_GB (default 1.0) AND the
// system input-idle for PERISCOPE_RECYCLE_IDLE_S (default 300s) → recycle.
// The idle gate keeps the ~2s blip invisible; there is also a manual
// View → Recycle Webview item with no gate.
//
// The WebContent pid comes from WKWebView's private `_webProcessIdentifier`
// selector — exact, where pid-guessing via lsof breaks as soon as a browser
// tab is also open on :8765. Private API is acceptable here: personal
// debug-build app, guarded by respondsToSelector so an OS that drops the
// selector degrades to "manual recycle only" instead of crashing.

use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::time::Duration;

use objc2::runtime::AnyObject;
use objc2::{msg_send, sel};
use tauri::{AppHandle, Manager, WebviewUrl};
use tauri_plugin_window_state::{AppHandleExt, StateFlags, WindowExt};

const CHECK_INTERVAL_S: u64 = 60;
const DASHBOARD_URL: &str = "http://127.0.0.1:8765";

// Set while a recycle is mid-flight so the ExitRequested handler in main.rs
// can veto the "last window closed → quit" path between destroy and rebuild.
pub static RECYCLING: AtomicBool = AtomicBool::new(false);
static WEBCONTENT_PID: AtomicI32 = AtomicI32::new(0);

fn threshold_bytes() -> u64 {
    let gb: f64 = std::env::var("PERISCOPE_RECYCLE_GB")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(1.0);
    (gb * 1024.0 * 1024.0 * 1024.0) as u64
}

fn idle_gate_s() -> f64 {
    std::env::var("PERISCOPE_RECYCLE_IDLE_S")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(300.0)
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

// --- WebContent pid off the live WKWebView (main thread only) ---

fn refresh_webcontent_pid(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let _ = window.with_webview(|webview| {
        let wk = webview.inner() as *mut AnyObject;
        if wk.is_null() {
            return;
        }
        unsafe {
            let wk = &*wk;
            let responds: bool = msg_send![wk, respondsToSelector: sel!(_webProcessIdentifier)];
            if responds {
                let pid: i32 = msg_send![wk, _webProcessIdentifier];
                WEBCONTENT_PID.store(pid, Ordering::Relaxed);
            }
        }
    });
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
// app (observed 2026-07-28: process gone, no window) — the deferred rebuild
// plus the ExitRequested veto in main.rs covers the windowless gap.
pub fn recycle_now(app: &AppHandle) {
    if RECYCLING.swap(true, Ordering::SeqCst) {
        return; // one at a time
    }
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
        for attempt in 1..=10 {
            std::thread::sleep(Duration::from_millis(if attempt == 1 { 250 } else { 1000 }));
            let (tx, rx) = std::sync::mpsc::channel::<Result<(), String>>();
            let h = handle.clone();
            let ok = handle.run_on_main_thread(move || {
                let result = tauri::WebviewWindowBuilder::new(
                    &h,
                    "main",
                    WebviewUrl::External(DASHBOARD_URL.parse().expect("static url parses")),
                )
                .title("Periscope")
                .build();
                let _ = tx.send(match result {
                    Ok(window) => {
                        let _ = window.restore_state(StateFlags::all());
                        WEBCONTENT_PID.store(0, Ordering::Relaxed); // re-learned next tick
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
                    reap_old_webcontent(old_pid);
                    return;
                }
                Ok(Err(e)) => shell_log(&format!("[recycle] rebuild attempt {attempt} failed: {e}")),
                Err(_) => shell_log(&format!("[recycle] rebuild attempt {attempt} timed out")),
            }
        }
        // Give up: clear the exit veto so the windowless app can quit normally.
        shell_log("[recycle] giving up after 10 attempts — app may now exit");
        RECYCLING.store(false, Ordering::SeqCst);
    });
}

// WebKit parks the displaced WebContent in its process cache instead of
// exiting it — observed alive at 480MB 90s after the window it hosted was
// destroyed, which would hoard exactly the leaked regions recycling exists to
// free. Grace period, then SIGKILL — guarded by proc_pidpath so a reused pid
// (some new unrelated process) is never the target.
fn reap_old_webcontent(old_pid: i32) {
    if old_pid <= 0 {
        return;
    }
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        let mut buf = [0u8; 4096];
        let n = unsafe {
            proc_pidpath(old_pid, buf.as_mut_ptr() as *mut std::ffi::c_void, buf.len() as u32)
        };
        if n <= 0 {
            shell_log(&format!("[recycle] old WebContent {old_pid} exited on its own"));
            return;
        }
        let path = String::from_utf8_lossy(&buf[..n as usize]).into_owned();
        if !path.contains("WebKit.WebContent") {
            shell_log(&format!("[recycle] pid {old_pid} reused by {path}; not killing"));
            return;
        }
        let fp = phys_footprint(old_pid).unwrap_or(0) / (1024 * 1024);
        unsafe { libc::kill(old_pid, libc::SIGKILL) };
        shell_log(&format!("[recycle] killed cached old WebContent {old_pid} ({fp} MB)"));
    });
}

extern "C" {
    fn proc_pidpath(pid: i32, buffer: *mut std::ffi::c_void, buffersize: u32) -> i32;
}

pub fn start_monitor(app: &AppHandle) {
    let handle = app.clone();
    std::thread::spawn(move || {
        let threshold = threshold_bytes();
        let idle_needed = idle_gate_s();
        shell_log(&format!(
            "[recycle] monitor started (threshold {} MB, idle gate {}s)",
            threshold / (1024 * 1024),
            idle_needed
        ));
        loop {
            std::thread::sleep(Duration::from_secs(CHECK_INTERVAL_S));
            // pid read must touch the webview → main thread; measurement and
            // policy stay here on the worker.
            let h = handle.clone();
            let _ = handle.run_on_main_thread(move || refresh_webcontent_pid(&h));
            std::thread::sleep(Duration::from_millis(200));
            let pid = WEBCONTENT_PID.load(Ordering::Relaxed);
            let Some(fp) = phys_footprint(pid) else {
                continue;
            };
            if fp < threshold {
                continue;
            }
            let idle = seconds_since_last_input();
            if idle < idle_needed {
                shell_log(&format!(
                    "[recycle] over threshold ({} MB) but input {}s ago — waiting for idle",
                    fp / (1024 * 1024),
                    idle as u64
                ));
                continue;
            }
            let h = handle.clone();
            let _ = handle.run_on_main_thread(move || recycle_now(&h));
        }
    });
}
