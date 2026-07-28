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

// Destroy + rebuild the sole window. Must run on the main thread.
pub fn recycle_now(app: &AppHandle) {
    if RECYCLING.swap(true, Ordering::SeqCst) {
        return; // one at a time
    }
    let fp = phys_footprint(WEBCONTENT_PID.load(Ordering::Relaxed)).unwrap_or(0);
    eprintln!(
        "[recycle] recreating webview (WebContent pid {}, footprint {} MB)",
        WEBCONTENT_PID.load(Ordering::Relaxed),
        fp / (1024 * 1024)
    );
    // Persist the frame explicitly; the plugin's own save fires on exit, not
    // on destroy-and-rebuild.
    let _ = app.save_window_state(StateFlags::all());
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.destroy();
    }
    let result = tauri::WebviewWindowBuilder::new(
        app,
        "main",
        WebviewUrl::External(DASHBOARD_URL.parse().expect("static url parses")),
    )
    .title("Periscope")
    .build();
    match result {
        Ok(window) => {
            let _ = window.restore_state(StateFlags::all());
            WEBCONTENT_PID.store(0, Ordering::Relaxed); // re-learned next tick
        }
        Err(e) => eprintln!("[recycle] window rebuild FAILED: {e}"),
    }
    RECYCLING.store(false, Ordering::SeqCst);
}

pub fn start_monitor(app: &AppHandle) {
    let handle = app.clone();
    std::thread::spawn(move || {
        let threshold = threshold_bytes();
        let idle_needed = idle_gate_s();
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
                eprintln!(
                    "[recycle] over threshold ({} MB) but input {}s ago — waiting for idle",
                    fp / (1024 * 1024),
                    idle as u64
                );
                continue;
            }
            let h = handle.clone();
            let _ = handle.run_on_main_thread(move || recycle_now(&h));
        }
    });
}
