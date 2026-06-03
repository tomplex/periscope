// Pure helpers, plus the shared apiCall wrapper. The pure ones don't
// touch the DOM; apiCall does, since it surfaces errors via toast.
//
// Ported verbatim from static/util.js — no behavior change. JSX
// auto-escapes, so escapeHtml only survives for the few imperative
// string-building call sites (e.g. md-link injection); most uses drop.

import { showToast } from "./overlays/Toast.jsx";

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

// Human label for a pane's needs-input reason. `reason` is the session
// status file's `waitingFor` (best-effort; values vary by Claude version), or
// falsy for unmapped panes that hit the scraping fallback. Known values get a
// friendly phrasing; anything else shows lowercased verbatim.
export function waitLabel(reason) {
  if (!reason) return "needs input";
  const map = {
    "approve askuserquestion": "needs answer",
    "permission prompt": "needs approval",
    "dialog open": "needs input",
  };
  return map[reason.toLowerCase()] || reason.toLowerCase();
}

// GitHub PR URL for a pane's repo. `slug` ("owner/repo") is derived
// server-side from `git remote get-url origin`; null when the repo's origin
// isn't a GitHub remote — callers then render the PR badge unlinked.
export function prUrl(slug, pr) {
  return slug ? `https://github.com/${slug}/pull/${pr}` : null;
}

// Rewrite an LGTM-server URL's hostname to match the parent page's host.
// The server hands out 127.0.0.1; the parent may be on localhost or LAN
// IP, and same-host iframes avoid mixed-host browser headaches.
export function rewriteLgtmHost(url) {
  try {
    const u = new URL(url);
    u.hostname = window.location.hostname;
    return u.toString();
  } catch {
    return url;
  }
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
