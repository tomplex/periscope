// Entry point. Filter buttons + new-session button + bootstrap. Everything
// else lives in its subsystem module:
//   state.js     — shared mutable state + localStorage persistence
//   util.js      — escapeHtml, targetQuery, relTime, apiCall
//   terminal.js  — xterm.js + WebSocket lifecycle (with auto-reconnect)
//   modal.js     — modal open/close/header/rename/paste
//   grid.js      — grid rendering, polling, drag-reorder, card handlers

import { state } from './state.js';
import { apiCall } from './util.js';
import { initModal } from './modal.js';
import { initGrid, poll, render } from './grid.js';

const filterButtons = document.querySelectorAll("#filters button");
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

initModal();
initGrid();
