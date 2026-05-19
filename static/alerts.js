// Right-rail alerts feed: cross-pane reverse-chronological view of every
// reply() the panes have sent through the channel. Polls /api/alerts/recent
// on the same 3s cadence as the main /api/state poll, but on its own
// interval so the rail can refresh while the user is anywhere in the UI.
//
// Open/closed state is persisted via prefs (alerts_open). The button in
// the header shows a count badge for unread need_human alerts only —
// info/done alerts are lower-signal and don't earn the badge.

import * as prefs from './prefs.js';
import { escapeHtml, relTime } from './util.js';
import { openModal } from './modal.js';
import { showToast } from './toast.js';

const POLL_MS = 3000;

let rail = null;
let body = null;
let toggleBtn = null;
let badge = null;
let pollTimer = null;
let lastItems = [];
// Edge-triggered toast state: emit a "feed unavailable" toast on the
// first failure of a healthy run and a "reconnected" toast when the
// next poll succeeds. Repeated failures stay silent.
let pollFailed = false;

export function initAlerts() {
  rail = document.getElementById("alerts-rail");
  body = document.getElementById("alerts-rail-body");
  toggleBtn = document.getElementById("alerts-toggle");
  badge = document.getElementById("alerts-badge");
  if (!rail || !toggleBtn) return;

  // Restore persisted open state.
  applyOpen(prefs.getAlertsOpen());

  toggleBtn.addEventListener("click", () => {
    const next = !isOpen();
    applyOpen(next);
    prefs.setAlertsOpen(next);
  });

  const closeBtn = document.getElementById("alerts-rail-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      applyOpen(false);
      prefs.setAlertsOpen(false);
    });
  }

  // Click delegation on row → open the pane modal. Empty-state and header
  // clicks fall through harmlessly.
  body.addEventListener("click", (e) => {
    const row = e.target.closest(".alerts-row");
    if (!row) return;
    const target = row.dataset.target;
    if (target) openModal(target);
  });

  // Keep --header-h synced with .periscope-header's height so the rail
  // tucks under the sticky header instead of sliding up over it. The
  // filter row can wrap on narrow widths, which is why we observe rather
  // than measure once.
  trackHeaderHeight();

  // Poll always, regardless of open state — keeps the badge fresh so a
  // need_human firing while the panel is closed still alerts the user.
  // The interval is cheap (one in-memory walk + json serialize).
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

function trackHeaderHeight() {
  const header = document.querySelector(".periscope-header");
  if (!header) return;
  const apply = () => {
    document.documentElement.style.setProperty(
      "--header-h", header.offsetHeight + "px"
    );
  };
  apply();
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(apply).observe(header);
  } else {
    window.addEventListener("resize", apply);
  }
}

function isOpen() {
  return document.body.dataset.alerts === "open";
}

function applyOpen(open) {
  document.body.dataset.alerts = open ? "open" : "closed";
  if (toggleBtn) toggleBtn.setAttribute("aria-pressed", open ? "true" : "false");
}

async function poll() {
  // Direct fetch (not apiCall) so a transient failure doesn't pop a
  // modal alert every 3s. Same pattern as grid.js's /api/state poll.
  try {
    const res = await fetch("/api/alerts/recent?limit=100");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (pollFailed) {
      pollFailed = false;
      showToast("alerts feed reconnected", "good");
    }
    lastItems = data.items || [];
    render();
  } catch (e) {
    if (!pollFailed) {
      pollFailed = true;
      showToast(`alerts feed unavailable: ${e.message}`, "bad");
    }
    // Keep the stale list visible — better than blanking the panel.
  }
}

function render() {
  if (!body) return;
  updateBadge();
  if (!lastItems.length) {
    body.innerHTML = `<div class="alerts-empty">No alerts yet. Panes call <code>reply()</code> through the channel to show up here.</div>`;
    return;
  }
  body.innerHTML = lastItems.map(renderRow).join("");
}

function renderRow(r) {
  const kind = r.kind || "info";
  const icon = kind === "need_human" ? "⚠" : kind === "done" ? "✓" : "•";
  const time = r.ts ? relTime(r.ts) : "";
  const paneLabel = `${r.session} · ${r.name || `:${r.index}`}`;
  return `
    <div class="alerts-row alerts-row-${kind}" data-target="${escapeHtml(r.target)}">
      <div class="alerts-row-head">
        <span class="alerts-row-icon">${icon}</span>
        <span class="alerts-row-pane" title="${escapeHtml(r.target)}">${escapeHtml(paneLabel)}</span>
        <span class="alerts-row-time">${escapeHtml(time)}</span>
      </div>
      <div class="alerts-row-body">${escapeHtml(r.message)}</div>
    </div>
  `;
}

function updateBadge() {
  if (!badge) return;
  // Only need_human gets a badge — info/done are noise at the dashboard
  // level. The whole panel still shows them, but the user doesn't need
  // to be summoned for "✓ tests pass."
  const count = lastItems.filter((r) => r.kind === "need_human").length;
  if (count > 0) {
    badge.hidden = false;
    badge.textContent = String(count);
  } else {
    badge.hidden = true;
  }
}
