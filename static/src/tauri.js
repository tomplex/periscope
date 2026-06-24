// Bridge from the browser-style dashboard to Tauri's native APIs.
// No-ops in a regular browser (Chrome/Safari at localhost:8765) — the
// dashboard keeps working unchanged. Only the Tauri shell .app has
// window.__TAURI_INTERNALS__, which is the runtime-injected IPC entry.
//
// Ported verbatim from static/tauri.js — it was already framework-free
// (raw __TAURI_INTERNALS__.invoke, no DOM ownership), so the only change is
// the module location. Callers invoke it from an effect (Alerts init).
//
// We call __TAURI_INTERNALS__.invoke directly instead of importing
// @tauri-apps/api modules so we stay in periscope's no-bundler regime.
// The IPC command paths (plugin:window|set_badge_count, etc.) are
// stable Tauri 2 contracts.
//
// Native notifications go through our own UNUserNotificationCenter bridge
// in Rust (src-tauri/src/notifications.rs), not tauri-plugin-notification:
// the plugin's macOS backend reports no click callback. We hand a banner
// to Rust over the "periscope:notify" event and Rust hands the click back
// over "periscope:notification-clicked" — events, because the webview is
// a remote origin that can't invoke Rust commands.

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

// Emit a banner to the Rust side, which owns UNUserNotificationCenter
// (delegate, authorization, click routing). `target` is the pane the
// banner is about — Rust stashes it so a click can route back here.
export async function notify({ title, body, target }) {
  if (!inTauri()) return;
  try {
    await invoke("plugin:event|emit", {
      event: "periscope:notify",
      payload: { title, body, target },
    });
  } catch (e) {
    console.warn("[tauri] notify failed:", e);
  }
}

// Register a handler for native-notification clicks. macOS already
// foregrounds Periscope on click; the payload is the pane target so the
// caller can open that pane's modal. transformCallback is the raw-JS way
// to receive a Tauri event without the @tauri-apps/api event module.
export async function onNotificationClick(cb) {
  if (!inTauri()) return;
  try {
    const handler = window.__TAURI_INTERNALS__.transformCallback((e) => {
      if (e?.payload) cb(e.payload);
    });
    await invoke("plugin:event|listen", {
      event: "periscope:notification-clicked",
      target: { kind: "Any" },
      handler,
    });
  } catch (e) {
    console.warn("[tauri] notification-click listener failed:", e);
  }
}

// Open a URL in the user's OS browser, regardless of host.
//
// Returns true on a path that should reach the user's browser, false if
// every fallback failed (caller can surface an error). Tries:
//   1. Tauri: direct plugin:opener|open_url invoke. Most reliable in
//      the shell — bypasses WKWebView's swallow-on-target=_blank quirk.
//   2. Browser: window.open. Works in Chrome/Safari from a real click;
//      may be null'd by popup blocker.
//   3. Synthetic <a target="_blank"> click fallback. Append+click+remove
//      so the click bubbles through document (so any capture listener
//      sees it, including `initExternalLinks` below).
export function openExternal(url) {
  if (inTauri()) {
    invoke("plugin:opener|open_url", { url }).catch((err) => {
      console.warn("[tauri] open_url failed:", err);
    });
    return true;
  }
  try {
    const popup = window.open(url, "_blank", "noopener,noreferrer");
    if (popup) return true;
  } catch (e) {
    console.warn("[periscope] window.open threw:", e);
  }
  try {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    document.body.appendChild(a);
    a.click();
    a.remove();
    return true;
  } catch (e) {
    console.warn("[periscope] anchor-click fallback threw:", e);
  }
  return false;
}

// In the Tauri shell, WKWebView silently swallows target="_blank" clicks
// and a plain cross-origin <a href> would replace the dashboard itself.
// Intercept clicks on external http(s) links and hand them to the OS
// browser via the opener plugin. No-op in a real browser — there
// target="_blank" already opens a tab, so this listener never installs.
//
// Capture phase is required: the PR/Linear card links carry a real
// onClick that calls e.stopPropagation(), which would prevent a
// bubble-phase document listener from ever seeing the click.
export function initExternalLinks() {
  if (!inTauri()) return;
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    let url;
    try {
      url = new URL(a.href);
    } catch {
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    if (url.origin === location.origin) return;  // same-origin: navigate normally
    e.preventDefault();
    e.stopPropagation();  // don't also open the card behind the link
    // open_url deserializes a `url` field (not `path`) — see
    // tauri-plugin-opener commands.rs / guest-js openUrl.
    invoke("plugin:opener|open_url", { url: url.href }).catch((err) => {
      console.warn("[tauri] open_url failed:", err);
    });
  }, true);
}
