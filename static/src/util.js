// Pure helpers, plus the shared apiCall wrapper. The pure ones don't
// touch the DOM; apiCall does, since it surfaces errors via toast.
//
// Ported verbatim from static/util.js — no behavior change. JSX
// auto-escapes, so escapeHtml only survives for the few imperative
// string-building call sites (e.g. md-link injection); most uses drop.

import { showToast } from "./overlays/Toast.jsx";
import { track } from "./track.js";

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

export function paneIdQuery(paneId) {
  // The /ws/pane terminal bridge keys on the stable tmux pane id (%N) so
  // the open terminal's address survives window-index drift (renumber).
  return `pane_id=${encodeURIComponent(paneId)}`;
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

// Shortest trailing-path suffix of `name` that is unique among `allNames`.
// Branch/session names are slash-paths (tc/model-train/feature-store); we show
// the fewest trailing segments needed to disambiguate from the other names
// currently on screen. Falls back to the full name when even that collides
// (e.g. an exact duplicate). `allNames` should include `name` itself.
export function shortestUniqueSuffix(name, allNames) {
  if (!name) return name;
  const segs = name.split("/");
  for (let k = 1; k < segs.length; k++) {
    const suffix = segs.slice(-k).join("/");
    const collides = allNames.some(
      (o) => o !== name && o && o.split("/").slice(-k).join("/") === suffix
    );
    if (!collides) return suffix;
  }
  return name;
}

// GitHub PR URL for a pane's repo. `slug` ("owner/repo") is derived
// server-side from `git remote get-url origin`; null when the repo's origin
// isn't a GitHub remote — callers then render the PR badge unlinked.
export function prUrl(slug, pr) {
  return slug ? `https://github.com/${slug}/pull/${pr}` : null;
}

// A linked PR's lifecycle state (open/merged/closed, resolved server-side by
// number) → a modifier class + title suffix for the badge. Open/unknown render
// as before. Merged and closed PRs no longer masquerade as live open work.
export function prStateMeta(state) {
  if (state === "merged") return { cls: "is-merged", suffix: " (merged)" };
  if (state === "closed") return { cls: "is-closed", suffix: " (closed)" };
  return { cls: "", suffix: "" };
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
  const method = opts.method || "GET";
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    track(`api:${label}`, { path, method, ok: false });
    showToast(`${label} failed: ${err.message}`, "bad", 6000);
    return null;
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || data.ok === false) {
    track(`api:${label}`, { path, method, ok: false });
    const err = data.error || data.detail || `HTTP ${res.status}`;
    showToast(`${label} failed: ${err}`, "bad", 6000);
    return null;
  }
  track(`api:${label}`, { path, method, ok: true });
  return data;
}
