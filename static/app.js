// Entry point. Loads prefs first so render() sees collapsed/order, then wires
// the filter + new-session + view switch handlers and starts the grid loop.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { apiCall } from './util.js';
import { initModal, openModal } from './modal.js';
import { initGrid, poll, render } from './grid.js';
import { initCommandsModal, openCommandsModal } from './commands-modal.js';

// ⌘/ from anywhere on the dashboard → /history. (On the history page itself,
// the same shortcut focuses the search input — handled in history.js.)
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "/") {
    e.preventDefault();
    window.location.href = "/history";
  }
});

// `[data-filter]` scope excludes the action chips (+ session, collapse all)
// that share the .filters parent — those have their own handlers.
const filterButtons = document.querySelectorAll("#filters button[data-filter]");
filterButtons.forEach((b) => {
  b.addEventListener("click", () => {
    filterButtons.forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.currentFilter = b.dataset.filter;
    render(state.lastWindows);
  });
});

document.getElementById("new-session").addEventListener("click", async () => {
  const name = prompt("session name:");
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
  const text = prompt(`Send to ${visible.length} pane(s) — text to paste (Enter submits):`);
  if (text === null || text === "") return;
  // Preview is truncated so a multi-line paste doesn't make the dialog scroll
  // off-screen; the full text is what actually gets sent.
  const preview = text.length > 80 ? text.slice(0, 77) + "…" : text;
  if (!confirm(`Send "${preview}" + Enter to ${visible.length} pane(s)?`)) return;
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
        alert(`Sent to ${res.sent}/${res.total}. ${failed} failed:\n${errs}`);
      }
    }
  } finally {
    delete sendBulkBtn.dataset.busy;
    sendBulkBtn.textContent = prevLabel;
    poll();  // surface any pending_input updates / spinner activations
  }
});

// View switch (grid ↔ stream). Persisted via prefs.js. Applied via
// body.dataset.view; the renderer dispatches on the attribute, and CSS keys
// off it to hide grid-only chrome (collapse-all toggle) in stream view.
const viewSwitch = document.getElementById("view-switch");
const viewButtons = viewSwitch.querySelectorAll("[data-view]");
function applyView(view) {
  document.body.dataset.view = view;
  viewButtons.forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
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
  applyView(prefs.getView());
  initModal();
  initGrid();
  initCommandsModal();
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

viewSwitch.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (!btn) return;
  const v = btn.dataset.view;
  if (document.body.dataset.view === v) return;  // no-op click on active
  applyView(v);
  prefs.setView(v);
  render(state.lastWindows);  // re-render against cached data, no refetch
});

bootstrap();
