// Pure helpers, plus the shared apiCall wrapper. The pure ones don't
// touch the DOM; apiCall does, since it surfaces errors via toast.

import { showToast } from "./toast.js";

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function targetQuery(target) {
  // target looks like "session:index" — but session may contain ":" if any
  // session name has one (rare in tmux but legal). Split on the last ":".
  const i = target.lastIndexOf(":");
  const session = target.slice(0, i);
  const index = target.slice(i + 1);
  return `session=${encodeURIComponent(session)}&index=${encodeURIComponent(index)}`;
}

export function relTime(epochSec) {
  if (!epochSec) return "";
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

// Shared error-surfacing wrapper. FastAPI returns `{detail: ...}` on 404/422,
// not our `{ok, error}` shape, so naive `data.error` reads as "undefined" when
// e.g. the wrong server version is running. Normalize both shapes.
//
// Errors surface via showToast (not alert()) — alert() is blocking and
// silently no-ops in WKWebView/Tauri, so it was wrong on both fronts.
// Toasts give the user a 6-second window to read the failure without
// stealing focus from whatever they're typing into.
export async function apiCall(label, path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    showToast(`${label} failed: ${err.message}`, "bad", 6000);
    return null;
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || data.ok === false) {
    const err = data.error || data.detail || `HTTP ${res.status}`;
    showToast(`${label} failed: ${err}`, "bad", 6000);
    return null;
  }
  return data;
}
