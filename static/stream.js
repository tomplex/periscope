// Stream view: a flat, recency-sorted list of every pane Tom has engaged
// with through periscope (acted_at > 0). The grid view's sibling — render()
// in grid.js dispatches here when the stream view is active.
//
// stream.js <-> grid.js is a deliberate, tolerated circular import (same
// shape as grid.js <-> modal.js): grid.js imports renderStream; this module
// imports passesFilter / updateToggleAll / poll. All uses are call-time, so
// ESM resolves the cycle fine.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, relTime, apiCall } from './util.js';
import { passesFilter, updateToggleAll, poll } from './grid.js';

const grid = document.getElementById("grid");

// Stream view sorts strictly by acted_at desc — most-recently-engaged at
// top, regardless of state. State color/icon still convey urgency; we don't
// also force state-priority into the sort key (doing so kept hours-old
// needs-input rows pinned above tabs you just opened).

function streamIcon(s) {
  if (s === "needs-input") return "!";
  if (s === "working") return `<span class="stream-spin">◐</span>`;
  if (s === "done") return "✓";
  if (s === "idle") return "·";
  return "$";
}

function streamAction(s) {
  if (s === "needs-input") return `<span class="stream-action-respond">respond ↵</span>`;
  if (s === "working") return "watch";
  if (s === "done") return "review";
  if (s === "idle") return "resume";
  return "focus";
}

function renderStreamRow(w) {
  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
  const apiErrCls = w.api_error ? " api-error" : "";
  const focusedCls = w.target === state.streamFocusedTarget ? " is-focused" : "";
  const needHumanCls = hasUnreadNeedHuman(w) ? " has-need-human" : "";
  const sessionLabel = escapeHtml(w.session);
  const branchPart = w.branch
    ? `${sessionLabel} · ${escapeHtml(w.branch)}`
    : sessionLabel;
  const ctxPart = w.is_claude && w.context_pct != null
    ? ` · ${escapeHtml((w.model || "").replace(/\s*\(.*\)/, ""))} · ${w.context_pct}%`
    : "";

  let msg = "";
  if (w.pending_input) {
    msg = `<span class="stream-prompt">›</span> ${escapeHtml(w.pending_input)}`;
  } else if (w.recap) {
    msg = escapeHtml(w.recap);
  } else if (w.last_line) {
    msg = escapeHtml(w.last_line);
  }

  // acted_at is guaranteed > 0 here (renderStream filtered).
  const when = relTime(w.acted_at) || "now";

  return `
    <div class="stream-row ${stateClass}${ciBadCls}${apiErrCls}${focusedCls}${needHumanCls}" data-target="${w.target}">
      <span class="stream-time">${when}</span>
      <span class="stream-icon">${streamIcon(w.state)}</span>
      <div class="stream-body">
        <div class="stream-title">
          <b>${escapeHtml(w.name)}</b>
          <em>${branchPart}</em>
          ${prefs.hasAnnotation(w.pid) ? `<span class="stream-anno" title="has notes">📝</span>` : ""}
          ${w.api_error ? `<span class="stream-api-error" title="last tool result was an API error — pane is waiting for a nudge">⚠ API error</span>` : ""}
          <span class="stream-extra">${ctxPart}</span>
        </div>
        <div class="stream-msg">${msg}</div>
      </div>
      <div class="stream-action">${streamAction(w.state)}</div>
    </div>
  `;
}

function passesStreamQuery(w, q) {
  if (!q) return true;
  const needle = q.toLowerCase();
  return (
    (w.name || "").toLowerCase().includes(needle) ||
    (w.session || "").toLowerCase().includes(needle)
  );
}

// Channel `need_human` notification with unread = pane is paging the user.
// Same gate as the dashboard-wide attention fade in render(); kept in sync
// so what gets pinned at the top of the stream matches what's lit up
// elsewhere.
function hasUnreadNeedHuman(w) {
  if (!(w.channel_unread > 0)) return false;
  return (w.channel_alerts || []).some((r) => r.kind === "need_human");
}

const STREAM_QUERY_KEY = "periscope-stream-query";

function loadStreamQuery() {
  try {
    return localStorage.getItem(STREAM_QUERY_KEY) || "";
  } catch {
    return "";
  }
}

function saveStreamQuery(q) {
  try {
    if (q) localStorage.setItem(STREAM_QUERY_KEY, q);
    else localStorage.removeItem(STREAM_QUERY_KEY);
  } catch {
    // Quota or disabled storage — query falls back to in-memory only.
  }
}

function ensureStreamScaffold() {
  // Stream toolbar (filter + new-tab) is built once and re-used across
  // polls. Rebuilding the input every 1.5s would yank focus and clobber
  // the user's typing mid-keystroke; we only refresh the dynamic parts
  // (banner text, row list, new-tab session label).
  if (document.getElementById("stream-toolbar")) return;
  // Hydrate the query from localStorage on first build. State already
  // holds it across re-renders within a single page load; this picks up
  // a value from the previous load.
  if (!state.streamQuery) state.streamQuery = loadStreamQuery();
  grid.innerHTML = `
    <div class="stream-toolbar" id="stream-toolbar">
      <input id="stream-filter" class="stream-filter" type="text"
             placeholder="filter by name or session… (press / to focus)" autocomplete="off"
             value="${escapeHtml(state.streamQuery || "")}">
      <button id="stream-new-tab" class="stream-new-tab" type="button" hidden></button>
    </div>
    <div class="stream-banner" id="stream-banner"></div>
    <div class="stream" id="stream-list"></div>
  `;
  const input = document.getElementById("stream-filter");
  input.addEventListener("input", () => {
    state.streamQuery = input.value;
    saveStreamQuery(input.value);
    renderStream(state.lastWindows);
  });
  // Esc clears the query and re-renders. Doesn't blur — Esc is more useful
  // as "abort current filter" than "leave the search," especially since
  // there's no other Esc handler bound to the stream view.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && input.value) {
      e.stopPropagation();
      input.value = "";
      state.streamQuery = "";
      saveStreamQuery("");
      renderStream(state.lastWindows);
    }
  });
  document.getElementById("stream-new-tab").addEventListener(
    "click", handleStreamNewTab,
  );
}

async function handleStreamNewTab(e) {
  const btn = e.currentTarget;
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  if (!session) return;
  btn.disabled = true;
  try {
    await apiCall(
      "new window",
      `/api/window/new?session=${encodeURIComponent(session)}&exec=${encodeURIComponent(exec)}`,
      { method: "POST" },
    );
  } finally {
    btn.disabled = false;
  }
  poll();
}

function updateStreamNewTab(topRow) {
  const btn = document.getElementById("stream-new-tab");
  if (!btn) return;
  const commands = prefs.getCommands();
  const primary = commands[0];
  // Need both: a session to spawn into (from the topmost row) AND a
  // primary command (so we know what to launch). Without either, hide
  // the button — header buttons handle the "from-scratch" cases.
  if (!topRow || !primary) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.dataset.session = topRow.session;
  btn.dataset.exec = primary.exec || "";
  btn.textContent = `+ ${primary.label} in ${topRow.session}`;
  btn.title = `spawn \`${primary.exec || "shell"}\` as a new window in tmux session '${topRow.session}'`;
}

export function renderStream(windows) {
  // Stream considers *only* windows Tom has actually engaged with in
  // periscope (acted_at > 0). Sessions Tom has switched to in tmux but
  // never opened in the dashboard don't show here.
  ensureStreamScaffold();
  const banner = document.getElementById("stream-banner");
  const list = document.getElementById("stream-list");

  const opened = windows.filter((w) => w.acted_at > 0);
  // Two-key sort:
  //   1. needs-human-and-unread group first (Claude is paging the user via
  //      the channel — this outranks anything else, including a tab opened
  //      30s ago, because the alert IS the reason to look at the stream).
  //   2. acted_at desc within each group.
  const visible = opened
    .filter(passesFilter)
    .filter((w) => passesStreamQuery(w, state.streamQuery))
    .sort((a, b) => {
      const ah = hasUnreadNeedHuman(a) ? 0 : 1;
      const bh = hasUnreadNeedHuman(b) ? 0 : 1;
      if (ah !== bh) return ah - bh;
      return (b.acted_at || 0) - (a.acted_at || 0);
    });

  // Track the rendered order so ↑/↓ key handlers can step through it
  // without recomputing the sort.
  state.streamVisible = visible.map((w) => w.target);

  // Reconcile focused target with what's actually visible. If the focused
  // row got filtered out (or it's still null on first paint), snap to the
  // top of the list.
  if (
    !state.streamFocusedTarget ||
    !state.streamVisible.includes(state.streamFocusedTarget)
  ) {
    state.streamFocusedTarget = state.streamVisible[0] || null;
  }

  // Topmost row's session powers the "+ new tab" button — keep this
  // before the empty-state early returns so the button updates even when
  // the filtered list is empty (it stays usable while you're searching).
  updateStreamNewTab(visible[0] || [...opened].sort((a, b) => b.acted_at - a.acted_at)[0]);

  if (!opened.length) {
    banner.textContent = "";
    list.innerHTML = `<div class="empty-state">no tabs opened yet — click a card in grid view to start tracking activity</div>`;
    updateToggleAll([]);
    return;
  }
  if (!visible.length) {
    const reason = state.streamQuery
      ? `no opened tabs match "${state.streamQuery}"`
      : "no opened tabs match the current filter";
    banner.textContent = "";
    list.innerHTML = `<div class="empty-state">${escapeHtml(reason)}</div>`;
    updateToggleAll([]);
    return;
  }

  const attention = visible.filter(
    (w) => w.state === "needs-input" || w.state === "working"
  ).length;
  banner.textContent = `Now · ${attention} ${attention === 1 ? "needs" : "need"} attention`;
  list.innerHTML = visible.map(renderStreamRow).join("");
  updateToggleAll([]);  // toggle-all is grid-only; hide while in stream
}
