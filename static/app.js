// Entry point. Loads prefs first so render() sees collapsed/order, then wires
// the filter + new-session + view switch handlers and starts the grid loop.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { apiCall } from './util.js';
import { initModal, openModal } from './modal.js';
import { initGrid, poll, render } from './grid.js';
import { renderRail } from './rail.js';
import { refreshDetail, detailTeardown } from './detail.js';
import { initCommandsModal, openCommandsModal } from './commands-modal.js';
import { initNewProjectModal } from './new-project-modal.js';
import { initReviewPRModal } from './review-pr-modal.js';
import { initCleanupModal } from './cleanup-modal.js';
import { initAlerts } from './alerts.js';
import { initExternalLinks } from './tauri.js';
import { initSettingsModal } from './settings-modal.js';
import { initOpenPicker } from './open-picker-modal.js';
import { initLauncher } from './launcher-modal.js';
import { pushEscape, popEscape } from './overlay.js';
import { confirmDialog, promptDialog, alertDialog } from './dialog.js';

// Per-surface mount switch (set in index.html before this script runs).
// When a surface is Preact-owned, the vanilla equivalent must no-op so we
// never run two /api/state polls, two keydown handlers, or two
// body[data-view] writers at once. Deleted with the rest of vanilla in the
// final cutover task.
const PREACT_SURFACES = window.__PREACT_SURFACES__ || new Set();

// ⌘/ from anywhere on the dashboard → /history. (On the history page itself,
// the same shortcut focuses the search input — handled in history.js.)
//
// Stream-view shortcuts (gated on body[data-view="stream"] and modal-closed):
//   /           focus the filter input
//   ↑ / ↓       step the focused row (works while the filter has focus too —
//               single-line inputs swallow nothing useful from vertical arrows)
//   Enter       open the modal for the focused row
if (!PREACT_SURFACES.has("chrome")) document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "/") {
    e.preventDefault();
    window.location.href = "/history";
    return;
  }

  // Tab cycles between grid and stream views. Suppressed when any input/
  // textarea is focused (so the user's Tab through form fields keeps
  // working) and when the modal is open (its own keybindings own that
  // surface). Shift+Tab also flips — with only two views the direction
  // doesn't matter.
  const activeTagGlobal = (document.activeElement?.tagName || "").toLowerCase();
  const editableActive =
    activeTagGlobal === "input" ||
    activeTagGlobal === "textarea" ||
    document.activeElement?.isContentEditable;
  const modalElGlobal = document.getElementById("modal");
  const modalOpen = modalElGlobal && !modalElGlobal.classList.contains("hidden");
  if (
    e.key === "Tab" &&
    !e.metaKey &&
    !e.ctrlKey &&
    !e.altKey &&
    !editableActive &&
    !modalOpen
  ) {
    e.preventDefault();
    const next = nextView(document.body.dataset.view);
    const btn = document.querySelector(`[data-view="${next}"]`);
    if (btn) btn.click();
    return;
  }

  if (document.body.dataset.view !== "stream") return;
  const modalEl = document.getElementById("modal");
  if (modalEl && !modalEl.classList.contains("hidden")) return;

  const activeTag = (document.activeElement?.tagName || "").toLowerCase();
  const activeId = document.activeElement?.id || "";
  const inFilter = activeId === "stream-filter";
  const inOtherInput =
    (activeTag === "input" || activeTag === "textarea") && !inFilter;
  if (inOtherInput) return;

  if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    if (inFilter) return;
    const filterEl = document.getElementById("stream-filter");
    if (!filterEl) return;
    e.preventDefault();
    filterEl.focus();
    filterEl.select();
    return;
  }

  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const visible = state.streamVisible;
    if (!visible.length) return;
    e.preventDefault();
    const cur = state.streamFocusedTarget;
    const idx = visible.indexOf(cur);
    const step = e.key === "ArrowDown" ? 1 : -1;
    const next =
      idx === -1
        ? visible[0]
        : visible[Math.max(0, Math.min(visible.length - 1, idx + step))];
    if (next === cur) return;
    state.streamFocusedTarget = next;
    // Update the focused class in place — cheaper and less jumpy than
    // re-rendering the whole stream every keystroke.
    document
      .querySelectorAll(".stream-row.is-focused")
      .forEach((el) => el.classList.remove("is-focused"));
    const row = document.querySelector(
      `.stream-row[data-target="${CSS.escape(next)}"]`,
    );
    if (row) {
      row.classList.add("is-focused");
      row.scrollIntoView({ block: "nearest" });
    }
    return;
  }

  if (e.key === "Enter" && state.streamFocusedTarget) {
    // Enter on the filter input with no rows visible just does nothing;
    // we won't try to "submit search" or similar.
    if (!state.streamVisible.includes(state.streamFocusedTarget)) return;
    e.preventDefault();
    openModal(state.streamFocusedTarget);
  }
});

// `[data-filter]` scope excludes the action chips (+ session, collapse all)
// that share the .filters parent — those have their own handlers. The
// filter buttons live inside the `state ▾` dropdown menu now; we keep the
// querySelector scope to `#filters` so the wiring works whether the
// buttons are surfaced as chips or as menu items.
const filterButtons = document.querySelectorAll("#filters button[data-filter]");
const filterLabel = document.getElementById("filter-dd-label");
filterButtons.forEach((b) => {
  b.addEventListener("click", () => {
    filterButtons.forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.currentFilter = b.dataset.filter;
    if (filterLabel) filterLabel.textContent = b.textContent.trim();
    closeAllDropdowns();
    render(state.lastWindows);
  });
});

// ─── Toolbar dropdowns (state filter / + new / ⋯) ────────────────────
// One open at a time. Outside click + Escape close. Each toggle button
// has `aria-expanded`; CSS rotates the chevron off of it.
let _openDropdown = null;

function openDropdown(dd) {
  if (_openDropdown && _openDropdown !== dd) closeDropdown(_openDropdown);
  if (_openDropdown === dd) return;
  const toggle = dd.querySelector(".tb-dd-toggle");
  const menu = dd.querySelector(".tb-dd-menu");
  if (!toggle || !menu) return;
  menu.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  _openDropdown = dd;
  pushEscape(_escapeDropdown);
  // Defer the outside-click listener so the click that opened the menu
  // doesn't immediately count as "outside" (same pattern modal.js uses
  // for the LGTM docs dropdown).
  setTimeout(() => document.addEventListener("click", _outsideDropdownClick), 0);
}

function closeDropdown(dd) {
  if (!dd) return;
  const toggle = dd.querySelector(".tb-dd-toggle");
  const menu = dd.querySelector(".tb-dd-menu");
  if (menu) menu.hidden = true;
  if (toggle) toggle.setAttribute("aria-expanded", "false");
  if (_openDropdown === dd) {
    popEscape(_escapeDropdown);
    document.removeEventListener("click", _outsideDropdownClick);
    _openDropdown = null;
  }
}

function closeAllDropdowns() {
  if (_openDropdown) closeDropdown(_openDropdown);
}

function _escapeDropdown() {
  if (_openDropdown) closeDropdown(_openDropdown);
}

function _outsideDropdownClick(e) {
  if (!_openDropdown) return;
  if (e.target.closest(".tb-dd") === _openDropdown) return;
  closeDropdown(_openDropdown);
}

document.querySelectorAll(".tb-dd").forEach((dd) => {
  const toggle = dd.querySelector(".tb-dd-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    if (_openDropdown === dd) closeDropdown(dd);
    else openDropdown(dd);
  });
});

// Menu items that don't have their own click handler still need to close
// the menu after firing. Action buttons inside (#new-session, #cleanup-btn,
// etc.) have their own listeners further down/elsewhere; we just close
// the dropdown on any item click that bubbles up here. The handlers run
// first (capture order: handler bound to button, then this delegated
// listener on the .tb-dd-menu container).
document.querySelectorAll(".tb-dd-menu").forEach((menu) => {
  menu.addEventListener("click", (e) => {
    if (!e.target.closest(".tb-dd-item")) return;
    closeAllDropdowns();
  });
});

document.getElementById("new-session").addEventListener("click", async () => {
  const name = await promptDialog("session name:", { placeholder: "e.g. tc/feature" });
  if (!name) return;
  await apiCall("new session", `/api/session/new`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  poll();
});

// Bulk broadcast — paste the prompted text + Enter into every pane currently
// shown by the active filter. Two safety guards: (1) the button itself only
// appears when visible count > 1; (2) `confirm()` shows the text + count
// before the fan-out fires, because typing into 15 panes simultaneously is a
// footgun if anything goes wrong.
const sendBulkBtn = document.getElementById("send-bulk");
sendBulkBtn.addEventListener("click", async () => {
  if (sendBulkBtn.dataset.busy) return;  // re-entrancy guard while a broadcast is in flight
  const visible = state.lastWindows.filter((w) => {
    // Mirror grid.js passesFilter — kept in this file via the active filter
    // pill rather than re-importing to avoid a circular dep with grid.js.
    const f = state.currentFilter;
    if (f === "all") return true;
    if (f === "needs-input") return w.state === "needs-input";
    if (f === "working") return w.state === "working";
    if (f === "done") return w.state === "done";
    if (f === "idle") return w.state === "idle";
    if (f === "claude") return w.is_claude;
    if (f === "shell") return w.state === "shell";
    if (f === "ci-bad") return w.ci === "✗";
    return true;
  });
  if (visible.length < 2) return;
  const text = await promptDialog(`Send to ${visible.length} pane(s) — text to paste (Enter submits):`);
  if (text === null || text === "") return;
  // Preview is truncated so a multi-line paste doesn't make the dialog scroll
  // off-screen; the full text is what actually gets sent.
  const preview = text.length > 80 ? text.slice(0, 77) + "…" : text;
  if (!(await confirmDialog(`Send "${preview}" + Enter to ${visible.length} pane(s)?`, { okLabel: "Send" }))) return;
  sendBulkBtn.dataset.busy = "1";
  const prevLabel = sendBulkBtn.textContent;
  sendBulkBtn.textContent = `sending…`;
  try {
    const res = await apiCall("send-bulk", `/api/send-bulk`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        targets: visible.map((w) => w.target),
        paste: text,
        keys: ["Enter"],
      }),
    });
    if (res && res.sent != null) {
      const failed = res.total - res.sent;
      if (failed > 0) {
        const errs = (res.results || [])
          .filter((r) => !r.ok)
          .map((r) => `${r.target}: ${r.error}`)
          .slice(0, 5)
          .join("\n");
        await alertDialog(`Sent to ${res.sent}/${res.total}. ${failed} failed:\n${errs}`);
      }
    }
  } finally {
    delete sendBulkBtn.dataset.busy;
    sendBulkBtn.textContent = prevLabel;
    poll();  // surface any pending_input updates / spinner activations
  }
});

// View switch (split / grid / stream). Persisted via prefs.js. Applied via
// body.dataset.view; the renderer dispatches on the attribute, and CSS keys
// off it to hide grid-only chrome (collapse-all toggle) in stream view.
const viewSwitch = document.getElementById("view-switch");
const viewButtons = viewSwitch.querySelectorAll("[data-view]");

function nextView(current) {
  const order = ["split", "grid", "stream"];
  const i = order.indexOf(current);
  return order[(i + 1) % order.length];
}

function applyView(view) {
  document.body.dataset.view = view;
  const grid = document.getElementById("grid");
  const split = document.getElementById("split-view");

  // Grid container is shared by grid AND stream views (stream renders into #grid).
  grid.classList.toggle("hidden", view === "split");
  split.classList.toggle("hidden", view !== "split");

  viewButtons.forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });

  // Add the split branch — defer rail/detail rendering.
  if (view === "split") {
    renderRail();
    refreshDetail();
  } else {
    detailTeardown();
  }
}

// Register the service worker. Required for PWA install eligibility
// (Chrome's install button, Firefox-via-PWAsForFirefox, Safari Add-to-Dock).
// SW behavior itself is a no-op — see static/sw.js for why we don't cache.
// Silent-catch so a hostile env (file://, no-SW browser) never breaks the
// page; the rest of the app works without install support.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

async function bootstrap() {
  await prefs.loadPrefs();
  // Seed the in-memory collapsed set from server state. Subsequent toggles
  // mutate `state.collapsedSessions` directly and call prefs.setCollapsed.
  state.collapsedSessions = prefs.getCollapsed();
  // applyView writes body[data-view] (chrome surface) and kicks the
  // grid/split render path; skip both when those surfaces are Preact-owned.
  if (!PREACT_SURFACES.has("chrome")) applyView(prefs.getView());
  if (!PREACT_SURFACES.has("modal")) initModal();
  if (!PREACT_SURFACES.has("grid")) initGrid();
  // Bridge: while the grid surface is Preact-owned but the modal is still
  // vanilla (Task 6 not yet done), expose the vanilla opener so the Preact
  // grid's card clicks can open the modal. Removed in the final cutover.
  if (PREACT_SURFACES.has("grid") && !PREACT_SURFACES.has("modal")) {
    window.__periscopeOpenModal = openModal;
  }
  initCommandsModal();
  initNewProjectModal();
  initReviewPRModal();
  initCleanupModal();
  initAlerts();
  initSettingsModal();
  initOpenPicker();
  initLauncher();
  initExternalLinks();
  document.getElementById("open-commands").addEventListener("click", openCommandsModal);

  // `?modal=<target>` is the handoff signal from /history resume (and any
  // future deep-link). Strip it before opening so a refresh doesn't keep
  // re-popping the modal.
  const params = new URLSearchParams(window.location.search);
  const modalTarget = params.get("modal");
  if (modalTarget) {
    params.delete("modal");
    const qs = params.toString();
    history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : ""));
    openModal(modalTarget);
  }
}

if (!PREACT_SURFACES.has("chrome")) viewSwitch.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (!btn) return;
  const v = btn.dataset.view;
  if (document.body.dataset.view === v) return;  // no-op click on active
  applyView(v);
  prefs.setView(v);
  render(state.lastWindows);  // re-render against cached data, no refetch
});

bootstrap();
