// Entry point. Filter buttons + new-session button + bootstrap. Everything
// else lives in its subsystem module:
//   state.js     — shared mutable state + localStorage persistence
//   util.js      — escapeHtml, targetQuery, relTime, apiCall
//   terminal.js  — xterm.js + WebSocket lifecycle (with auto-reconnect)
//   modal.js     — modal open/close/header/rename/paste
//   grid.js      — grid rendering, polling, drag-reorder, card handlers

import { state, loadView, saveView } from './state.js';
import { apiCall } from './util.js';
import { initModal } from './modal.js';
import { initGrid, poll, render } from './grid.js';

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

// View switch (grid ↔ stream). Persisted via state.js. Applied via
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
applyView(loadView());
viewSwitch.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (!btn) return;
  const v = btn.dataset.view;
  if (document.body.dataset.view === v) return;  // no-op click on active
  applyView(v);
  saveView(v);
  render(state.lastWindows);  // re-render against cached data, no refetch
});

initModal();
initGrid();
