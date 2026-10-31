// Pure helpers, plus the shared apiCall wrapper. The pure ones don't
// touch the DOM; apiCall does, since it surfaces errors via toast.
//
// Ported verbatim from static/util.js — no behavior change. JSX
// auto-escapes, so escapeHtml only survives for the few imperative
// string-building call sites (e.g. md-link injection); most uses drop.

import { showToast } from "./overlays/Toast.jsx";
import { track } from "./track.js";

export const AGENT_META = {
  claude: { label: "Claude", glyph: "✻", className: "icon-claude" },
  codex: { label: "Codex", glyph: "⬡", className: "icon-codex" },
};

// "1 commit", "3 commits".
export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function paneLabel(w) {
  return w?.name || w?.agent || "shell";
}

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

// What a pane's needs-input waits on, as a kind, from the same raw values as
// waitLabel: the native notifications word a permission dialog, a multiple-
// choice question and any other dialog differently. null when unmapped.
export function waitKind(reason) {
  if (!reason) return null;
  const r = reason.toLowerCase();
  if (r === "permission prompt") return "permission";
  if (r === "approve askuserquestion") return "question";
  return "dialog";
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
// Cycle-hint chip content (rail ↻ chip + detail header). `mem` is the
// server's `w.mem` ({tier: "warn"|"bad", trigger: "mem"|"age", gb, age_s})
// or null/undefined — the server only sends it when a threshold tripped, so
// null → no chip. The label leads with whichever limit tripped. gb is null
// when claude's footprint couldn't be read.
export function memHint(mem) {
  if (!mem) return null;
  const days = Math.floor(mem.age_s / 86400);
  const hours = Math.floor((mem.age_s % 86400) / 3600);
  const up = days > 0 ? `${days}d ${hours}h` : `${hours}h`;
  const what = mem.gb == null ? `Claude has been running ${up}` : `Claude is using ${mem.gb}GB after ${up}`;
  return {
    label: mem.trigger === "mem" ? `${mem.gb}G` : `${days}d`,
    title: `${what} — consider exiting and resuming this session`,
    cls: `mem-${mem.tier}`,
  };
}

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

// A JSON POST that reports how it went instead of toasting: { ok, status,
// detail }. For callers that treat a status as an answer — a 409 from
// /api/pane/permission means "the banner is stale", not an error — which
// apiCall hides behind null after toasting. Never throws: a network failure,
// or a route that hangs past 10s, is status 0. A JSON body is also what the
// agent-acting routes require (routes/pane_actions.py), so this always sends
// one. AbortSignal.timeout isn't in every embedded webview; feature-detect
// rather than let a missing one throw before the request ever goes out.
export async function postJson(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      ...(typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
        ? { signal: AbortSignal.timeout(10000) }
        : {}),
    });
  } catch (err) {
    return { ok: false, status: 0, detail: err?.message || String(err) };
  }
  let data = {};
  try { data = (await res.json()) || {}; } catch (_) {}
  // FastAPI's 422 detail is a list of pydantic errors; the first's `msg` beats
  // the bare status when it's there.
  const detail = typeof data.detail === "string"
    ? data.detail
    : typeof data.error === "string"
      ? data.error
      : data.detail?.[0]?.msg || `HTTP ${res.status}`;
  return { ok: res.ok, status: res.status, detail };
}
