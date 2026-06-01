// Right-rail alerts feed: cross-pane reverse-chronological view of every
// notify() the panes have sent through the channel. Polls /api/alerts/recent
// on the same 3s cadence as the main /api/state poll, but on its own
// interval so the rail can refresh while the user is anywhere in the UI.
//
// Ported from static/alerts.js. The imperative innerHTML rendering becomes
// JSX (convention #8 — no innerHTML); the open/close state, the badge, the
// --header-h ResizeObserver, the native-notify dedupe sentinel, and the
// poll lifecycle are preserved verbatim. The CSS contract carries over
// unchanged: #alerts-rail / .alerts-rail-head / .alerts-rail-body /
// .alerts-row / .alerts-row-${kind} / .alerts-empty / .alerts-badge, the
// body[data-alerts] attribute, and the --header-h custom prop.
//
// Open/closed state is persisted via prefs (alerts_open). The header toggle
// button (rendered by <Header>, chrome surface) shows a count badge for
// unread need_human alerts only — info/done alerts are lower-signal and
// don't earn the badge. We wire to that button + badge by id (they carry
// stable ids #alerts-toggle / #alerts-badge), the same imperative-by-id
// bridge <Grid> uses for send-bulk / collapse-all.
import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import * as prefs from "../prefs.js";
import { relTime } from "../util.js";
import { showToast } from "./Toast.jsx";
import { openModal } from "../modal/Modal.jsx";
import { setBadgeCount, notify, onNotificationClick, inTauri } from "../tauri.js";

const POLL_MS = 3000;

// Feed items (poll-fed) + open state, as signals so the rail re-renders.
const items = signal([]);
const open = signal(false);

// Edge-triggered toast state: emit a "feed unavailable" toast on the first
// failure of a healthy run and a "reconnected" toast when the next poll
// succeeds. Repeated failures stay silent.
let pollFailed = false;

// Native-notification dedupe (Tauri only). The first poll snapshots the
// existing need_human alerts so we don't fire a banner for every backlog
// item on app open. Subsequent polls diff against this Set and notify only
// on entries new to us. `null` is the first-poll sentinel — must NOT be
// pre-seeded to an empty Set, or the backlog would all read as "new."
let seenAlertKeys = null;
function alertKey(r) {
  return `${r.target}|${r.ts}|${(r.message || "").slice(0, 60)}`;
}

// Keep --header-h synced with .periscope-header's height so the rail tucks
// under the sticky header instead of sliding up over it. The filter row can
// wrap on narrow widths, which is why we observe rather than measure once.
function trackHeaderHeight() {
  // Prefer the Preact header (inside #app); the vanilla one is display:none in
  // Preact mode and would measure 0, collapsing #split-view to top:0. When the
  // chrome surface is Preact-owned, <Header> also sets --header-h from its ref
  // (authoritative); both agree on the visible header's height.
  const header =
    document.querySelector("#app .periscope-header") ||
    document.querySelector(".periscope-header");
  if (!header) return () => {};
  const apply = () => {
    document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  };
  apply();
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(apply);
    ro.observe(header);
    return () => ro.disconnect();
  }
  window.addEventListener("resize", apply);
  return () => window.removeEventListener("resize", apply);
}

function applyOpen(next) {
  open.value = next;
  document.body.dataset.alerts = next ? "open" : "closed";
  const toggleBtn = document.getElementById("alerts-toggle");
  if (toggleBtn) toggleBtn.setAttribute("aria-pressed", next ? "true" : "false");
}

function updateBadge(list) {
  // Only need_human gets a badge — info/done are noise at the dashboard
  // level. The whole panel still shows them, but the user doesn't need to
  // be summoned for "✓ tests pass."
  const count = list.filter((r) => r.kind === "need_human").length;
  const badge = document.getElementById("alerts-badge");
  if (badge) {
    if (count > 0) {
      badge.hidden = false;
      badge.textContent = String(count);
    } else {
      badge.hidden = true;
    }
  }
  // Mirror the count to the macOS dock badge when running in the Tauri
  // shell. No-ops in a regular browser tab.
  setBadgeCount(count);
}

function maybeNativeNotify(list) {
  if (!inTauri()) return;
  const needHuman = list.filter((r) => r.kind === "need_human");
  // First successful poll: snapshot current state, don't notify. The backlog
  // of alerts that existed before the app launched isn't news.
  if (seenAlertKeys === null) {
    seenAlertKeys = new Set(needHuman.map(alertKey));
    return;
  }
  for (const r of needHuman) {
    const k = alertKey(r);
    if (seenAlertKeys.has(k)) continue;
    seenAlertKeys.add(k);
    const paneLabel = `${r.session} · ${r.name || `:${r.index}`}`;
    notify({ title: `⚠ ${paneLabel}`, body: r.message || "", target: r.target });
  }
  // Keep the set bounded — drop entries that aren't in the current feed
  // anymore so the Set doesn't grow unboundedly across a long session.
  const current = new Set(needHuman.map(alertKey));
  for (const k of seenAlertKeys) {
    if (!current.has(k)) seenAlertKeys.delete(k);
  }
}

async function poll() {
  // Direct fetch (not apiCall) so a transient failure doesn't pop a toast
  // every 3s. Same pattern as the /api/state poll.
  try {
    const res = await fetch("/api/alerts/recent?limit=100");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (pollFailed) {
      pollFailed = false;
      showToast("notifications feed reconnected", "good");
    }
    const list = data.items || [];
    maybeNativeNotify(list);
    updateBadge(list);
    items.value = list;
  } catch (e) {
    if (!pollFailed) {
      pollFailed = true;
      showToast(`notifications feed unavailable: ${e.message}`, "bad");
    }
    // Keep the stale list visible — better than blanking the panel.
  }
}

function rowIcon(kind) {
  if (kind === "need_human") return "⚠";
  if (kind === "done") return "✓";
  if (kind === "milestone") return "★";
  return "•";
}

function AlertRow({ r }) {
  const kind = r.kind || "info";
  const time = r.ts ? relTime(r.ts) : "";
  const paneLabel = `${r.session} · ${r.name || `:${r.index}`}`;
  return (
    <div
      class={`alerts-row alerts-row-${kind}`}
      onClick={() => { if (r.target) openModal(r.target); }}
    >
      <div class="alerts-row-head">
        <span class="alerts-row-icon">{rowIcon(kind)}</span>
        <span class="alerts-row-pane" title={r.target}>{paneLabel}</span>
        <span class="alerts-row-time">{time}</span>
      </div>
      <div class="alerts-row-body">{r.message}</div>
    </div>
  );
}

export function Alerts() {
  // Restore persisted open state once, then own the poll loop + the toggle
  // wiring + the header-height observer.
  useEffect(() => {
    applyOpen(prefs.getAlertsOpen());

    const toggleBtn = document.getElementById("alerts-toggle");
    function onToggle() {
      const next = !open.value;
      applyOpen(next);
      prefs.setAlertsOpen(next);
    }
    if (toggleBtn) toggleBtn.addEventListener("click", onToggle);

    // A click on a native macOS notification routes here from the Rust
    // bridge — open the modal for the pane that fired the banner. No-op
    // outside the Tauri shell.
    onNotificationClick((target) => {
      if (target) openModal(target);
    });

    const stopHeaderTrack = trackHeaderHeight();

    // Poll always, regardless of open state — keeps the badge fresh so a
    // need_human firing while the panel is closed still alerts the user.
    poll();
    const timer = setInterval(poll, POLL_MS);

    return () => {
      clearInterval(timer);
      if (toggleBtn) toggleBtn.removeEventListener("click", onToggle);
      stopHeaderTrack();
    };
  }, []);

  const list = items.value;
  function close() {
    applyOpen(false);
    prefs.setAlertsOpen(false);
  }

  return (
    <aside id="alerts-rail" aria-label="notifications">
      <header class="alerts-rail-head">
        <h2>Notifications</h2>
        <button id="alerts-rail-close" title="close panel" onClick={close}>×</button>
      </header>
      <div id="alerts-rail-body" class="alerts-rail-body">
        {list.length === 0 ? (
          <div class="alerts-empty">
            No notifications yet. Panes call <code>notify()</code> through the channel to show up here.
          </div>
        ) : (
          list.map((r) => <AlertRow key={alertKey(r)} r={r} />)
        )}
      </div>
    </aside>
  );
}
