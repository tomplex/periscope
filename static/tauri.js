// Bridge from the browser-style dashboard to Tauri's native APIs.
// No-ops in a regular browser (Chrome/Safari at localhost:8765) — the
// dashboard keeps working unchanged. Only the Tauri shell .app has
// window.__TAURI_INTERNALS__, which is the runtime-injected IPC entry.
//
// We call __TAURI_INTERNALS__.invoke directly instead of importing
// @tauri-apps/api modules so we stay in periscope's no-bundler regime.
// The IPC command paths (plugin:window|set_badge_count, etc.) are
// stable Tauri 2 contracts — see tauri-2/scripts/bundle.global.js and
// tauri-plugin-notification/src/commands.rs.
//
// macOS gotcha: the first notification triggers a system permission
// prompt. We probe permission lazily (on the first attempt to notify),
// not on init, so the prompt only appears when there's actually a
// notification to show.

export function inTauri() {
  return typeof window !== "undefined"
    && typeof window.__TAURI_INTERNALS__ !== "undefined"
    && typeof window.__TAURI_INTERNALS__.invoke === "function";
}

function invoke(cmd, payload) {
  return window.__TAURI_INTERNALS__.invoke(cmd, payload || {});
}

export async function setBadgeCount(n) {
  if (!inTauri()) return;
  try {
    // null clears the macOS dock badge; any positive number shows that.
    const count = n > 0 ? Math.floor(n) : null;
    await invoke("plugin:window|set_badge_count", { count });
  } catch (e) {
    console.warn("[tauri] setBadgeCount failed:", e);
  }
}

// Cache only the positive outcome. is_permission_granted is a pure OS
// query (no prompt UX), so re-asking every notify() is cheap and lets
// us recover automatically when the user enables notifications in
// System Settings after an earlier dismissal — no page reload needed.
let _granted = false;

async function ensureNotifyPermission() {
  if (!inTauri()) return false;
  if (_granted) return true;
  try {
    const granted = await invoke("plugin:notification|is_permission_granted");
    if (granted === true) { _granted = true; return true; }
    if (granted === false) return false;
    // null → needs prompt. Triggers the OS dialog. If the user dismisses
    // it, request_permission returns something other than "granted"; we
    // return false but DON'T sticky-cache the denial — the next notify
    // will re-check and pick up a later grant from System Settings.
    const next = await invoke("plugin:notification|request_permission");
    if (next === "granted") { _granted = true; return true; }
    return false;
  } catch (e) {
    console.warn("[tauri] notification permission check failed:", e);
    return false;
  }
}

export async function notify({ title, body }) {
  if (!inTauri()) return;
  if (!(await ensureNotifyPermission())) return;
  try {
    await invoke("plugin:notification|notify", { options: { title, body } });
  } catch (e) {
    console.warn("[tauri] notify failed:", e);
  }
}
