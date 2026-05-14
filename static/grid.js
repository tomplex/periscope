// Grid rendering, /api/state polling, drag-reorder, and one-time event
// delegation on the grid root. Handlers walk up from event.target via
// closest() to find the relevant card/header/button and resolve
// target/session from data-attributes — so a render() innerHTML rebuild
// doesn't invalidate any listeners.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, targetQuery, apiCall, relTime } from './util.js';
import { openModal } from './modal.js';

const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const usageEl = document.getElementById("usage");
const toggleAllBtn = document.getElementById("toggle-all");

const nameClickTimers = new Map();  // target -> setTimeout handle (single-click defer for dblclick rename)

function passesFilter(w) {
  if (state.currentFilter === "all") return true;
  if (state.currentFilter === "needs-input") return w.state === "needs-input";
  if (state.currentFilter === "working") return w.state === "working";
  if (state.currentFilter === "waiting") return w.state === "waiting";
  if (state.currentFilter === "claude") return w.is_claude;
  if (state.currentFilter === "shell") return w.state === "shell";
  if (state.currentFilter === "ci-bad") return w.ci === "✗";
  return true;
}

function ciSpan(ci) {
  if (!ci) return "";
  const cls = ci === "✓" ? "card-ci-ok" : ci === "✗" ? "card-ci-bad" : "card-ci-pending";
  return `<span class="${cls}">${ci}</span>`;
}

// `git` from server is "clean" or "+N -M [*]". Split into clean/dirty +
// formatted suffix for separate styling. The trailing " *" (unpushed) is
// preserved as part of the dirty text.
function gitMetaSpan(git) {
  if (!git) return "";
  if (git === "clean") return `<span class="card-clean">clean</span>`;
  return `<span class="card-dirty">${escapeHtml(git)}</span>`;
}

function renderCard(w) {
  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
  const kind = w.is_claude ? "claude" : "shell";

  // Meta row: branch · clean/dirty · #PR ci.  PR/CI stays on the card so
  // a glance still surfaces CI breakage; matches the existing scan pattern.
  const metaParts = [];
  if (w.branch) metaParts.push(`<span class="card-branch">${escapeHtml(w.branch)}</span>`);
  if (w.git) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(gitMetaSpan(w.git));
  }
  if (w.pr) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(
      `<a class="card-pr" href="https://github.com/faradayio/fdy/pull/${w.pr}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${w.pr}</a>`
    );
    if (w.ci) metaParts.push(ciSpan(w.ci));
  }
  const metaRow = metaParts.length
    ? `<div class="card-meta">${metaParts.join(" ")}</div>`
    : "";

  // Activity row. is-pending for unsubmitted user input (loudest because it
  // means "claude is going to act on whatever you type next"); is-output for
  // recap / last_line; is-shell when it's a bare shell pane with nothing
  // claude-shaped to show.
  let activity = "";
  if (w.pending_input) {
    activity = `<div class="card-activity is-pending"><span class="prompt">›</span>${escapeHtml(w.pending_input)}</div>`;
  } else if (w.recap) {
    activity = `<div class="card-activity is-output">${escapeHtml(w.recap)}</div>`;
  } else if (w.last_line) {
    const cls = w.is_claude ? "is-output" : "is-shell";
    activity = `<div class="card-activity ${cls}">${escapeHtml(w.last_line)}</div>`;
  }

  // Status label. needs-input wins over the spinner verb (a stale "envisioning…"
  // in scrollback shouldn't drown out the blocking prompt).
  const statusText = w.state === "needs-input"
    ? "needs input"
    : w.spinner
      ? `${w.spinner.toLowerCase()}…`
      : w.state;
  const statusLabel = `<span class="card-status">${escapeHtml(statusText)}</span>`;

  // Footer: progress bar + ctx% + model + viewed-age. Progress bar only when
  // we have a context % to fill; otherwise the row reads "model · viewed Xm".
  const footParts = [];
  if (w.context_pct != null) {
    footParts.push(`<div class="card-progress"><i style="width:${w.context_pct}%"></i></div>`);
    footParts.push(`<span class="card-pct">${w.context_pct}%</span>`);
  }
  if (w.model) {
    footParts.push(`<span class="card-model">${escapeHtml(w.model.replace(/\s*\(.*\)/, ""))}</span>`);
  }
  const recent = relTime(w.focused_at);
  if (recent) footParts.push(`<span class="card-viewed">viewed ${recent}</span>`);
  const footRow = footParts.length
    ? `<div class="card-foot">${footParts.join(" ")}</div>`
    : "";

  return `
    <article class="card ${stateClass}${ciBadCls}" data-target="${w.target}" data-kind="${kind}">
      <header class="card-head">
        <span class="card-title">${escapeHtml(w.name)}</span>
        <span class="card-idx">${w.index}</span>
        ${statusLabel}
        <button class="card-kill" data-target="${w.target}" data-name="${escapeHtml(w.name)}" title="kill this window">✕</button>
      </header>
      ${metaRow}
      ${activity}
      ${footRow}
    </article>
  `;
}

function renderNewTile(session) {
  // Three-way split: claude is the primary action (largest hit area) since
  // it's the most-used; shell and vim share the other half stacked. All
  // three POST to /api/window/new — the server picks the boot command from
  // `mode`. (When worktree integration lands, `+ claude` will route to the
  // separate /api/window/new-worktree endpoint instead; the other two stay
  // here. data-mode is the contract handleNewWindow keys off.)
  const s = escapeHtml(session);
  return `
    <div class="card card-new" data-session="${s}">
      <button class="new-window is-primary" data-session="${s}" data-mode="claude">+ claude</button>
      <div class="new-window-stack">
        <button class="new-window" data-session="${s}" data-mode="shell">+ shell</button>
        <button class="new-window" data-session="${s}" data-mode="vim">+ vim</button>
      </div>
    </div>
  `;
}

function orderedSessions(allSessions, bySession) {
  // User-pinned (drag-reordered) sessions float to the top in saved order.
  // Everything else sorts by most-recent activity across its windows, descending.
  const saved = prefs.getSessionOrder();
  const present = new Set(allSessions);
  const ordered = saved.filter((s) => present.has(s));
  const remaining = allSessions.filter((s) => !ordered.includes(s));
  const lastFocus = (s) =>
    Math.max(0, ...(bySession.get(s) || []).map((w) => w.focused_at || 0));
  remaining.sort((a, b) => {
    const da = lastFocus(b) - lastFocus(a);
    if (da !== 0) return da;
    return a.localeCompare(b);
  });
  return [...ordered, ...remaining];
}

function sessionPill(ws) {
  const needsInput = ws.filter((w) => w.state === "needs-input").length;
  const waiting = ws.filter((w) => w.state === "waiting").length;
  const working = ws.filter((w) => w.state === "working").length;
  const ciBad = ws.filter((w) => w.ci === "✗").length;
  const parts = [];
  if (needsInput) parts.push(`${needsInput} needs input`);
  if (waiting) parts.push(`${waiting} waiting`);
  if (working) parts.push(`${working} working`);
  if (ciBad) parts.push(`${ciBad} ✗`);
  if (!parts.length) parts.push(`${ws.length}`);
  // Pill color hierarchy: needs-input is the loudest signal (a pane is
  // blocked on me) > ci-bad > waiting > working. Anything quieter loses.
  let cls = "session-pill";
  if (needsInput) cls += " has-needs-input";
  else if (ciBad) cls += " has-ci-bad";
  else if (waiting) cls += " has-waiting";
  else if (working) cls += " has-working";
  return `<span class="${cls}">${parts.join(" · ")}</span>`;
}

function renderSession(session, ws, totalWindows) {
  const shown = ws.length;
  const meta = shown === totalWindows
    ? `${totalWindows} windows`
    : `${shown}/${totalWindows} windows`;
  const collapsed = state.collapsedSessions.has(session) ? " collapsed" : "";
  const recent = Math.max(0, ...ws.map((w) => w.focused_at || 0));
  const recentLabel = recent ? relTime(recent) : "";
  const s = escapeHtml(session);
  return `
    <section class="session-group${collapsed}" data-session="${s}">
      <div class="session-header" draggable="true" data-session="${s}">
        <span class="chevron">▾</span>
        <h2>${s}</h2>
        <span class="session-meta">${meta}${recentLabel ? ` · ${recentLabel}` : ""}</span>
        ${sessionPill(ws)}
        <button class="auto-rename" data-session="${s}" title="ask Claude to auto-rename windows in this session">✨ rename</button>
        <button class="kill-session" data-session="${s}" title="kill this tmux session">✕</button>
      </div>
      <div class="cards">
        ${ws.map(renderCard).join("")}
        ${renderNewTile(session)}
      </div>
    </section>
  `;
}

// The header "collapse/expand all" toggle reflects, and operates on, only the
// sessions currently rendered — filtering out hidden sessions keeps the button
// label honest and avoids surprise mutations to off-screen state.
function updateToggleAll(visibleSessions) {
  if (!toggleAllBtn) return;
  if (visibleSessions.length === 0) {
    toggleAllBtn.hidden = true;
    return;
  }
  toggleAllBtn.hidden = false;
  const allCollapsed = visibleSessions.every((s) => state.collapsedSessions.has(s));
  toggleAllBtn.textContent = allCollapsed ? "▸ expand all" : "▾ collapse all";
}

function handleToggleAll() {
  const visible = [...grid.querySelectorAll(".session-group")].map((g) => g.dataset.session);
  if (visible.length === 0) return;
  const allCollapsed = visible.every((s) => state.collapsedSessions.has(s));
  if (allCollapsed) {
    for (const s of visible) state.collapsedSessions.delete(s);
  } else {
    for (const s of visible) state.collapsedSessions.add(s);
  }
  prefs.setCollapsed(state.collapsedSessions);
  render(state.lastWindows);
}

// Stream-view priority — needs > working > waiting > shell. Anything else
// (e.g., a transient "error" state) sorts last.
const STREAM_STATE_PRIORITY = { "needs-input": 0, working: 1, waiting: 2, shell: 3 };

function streamIcon(s) {
  if (s === "needs-input") return "!";
  if (s === "working") return `<span class="stream-spin">◐</span>`;
  if (s === "waiting") return "✓";
  return "$";
}

function streamAction(s) {
  if (s === "needs-input") return `<span class="stream-action-respond">respond ↵</span>`;
  if (s === "working") return "watch";
  if (s === "waiting") return "resume";
  return "focus";
}

function renderStreamRow(w) {
  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
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
    <div class="stream-row ${stateClass}${ciBadCls}" data-target="${w.target}">
      <span class="stream-time">${when}</span>
      <span class="stream-icon">${streamIcon(w.state)}</span>
      <div class="stream-body">
        <div class="stream-title">
          <b>${escapeHtml(w.name)}</b>
          <em>${branchPart}</em>
          <span class="stream-extra">${ctxPart}</span>
        </div>
        <div class="stream-msg">${msg}</div>
      </div>
      <div class="stream-action">${streamAction(w.state)}</div>
    </div>
  `;
}

function renderStream(windows) {
  // Stream considers *only* windows Tom has actually engaged with in
  // periscope (acted_at > 0). Sessions Tom has switched to in tmux but
  // never opened in the dashboard don't show here.
  const opened = windows.filter((w) => w.acted_at > 0);
  if (!opened.length) {
    grid.innerHTML = `<div class="empty-state">no tabs opened yet — click a card in grid view to start tracking activity</div>`;
    updateToggleAll([]);
    return;
  }
  const visible = opened.filter(passesFilter);
  if (!visible.length) {
    grid.innerHTML = `<div class="empty-state">no opened tabs match the current filter</div>`;
    updateToggleAll([]);
    return;
  }

  visible.sort((a, b) => {
    const da = (STREAM_STATE_PRIORITY[a.state] ?? 99) - (STREAM_STATE_PRIORITY[b.state] ?? 99);
    if (da !== 0) return da;
    return (b.acted_at || 0) - (a.acted_at || 0);
  });

  const attention = visible.filter(
    (w) => w.state === "needs-input" || w.state === "working"
  ).length;
  const banner = `<div class="stream-banner">Now · ${attention} ${attention === 1 ? "needs" : "need"} attention</div>`;
  grid.innerHTML = banner + `<div class="stream">${visible.map(renderStreamRow).join("")}</div>`;
  updateToggleAll([]);  // toggle-all is grid-only; hide while in stream
}

function renderGrid(windows) {
  const filtered = windows.filter(passesFilter);
  const bySession = new Map();
  for (const w of filtered) {
    if (!bySession.has(w.session)) bySession.set(w.session, []);
    bySession.get(w.session).push(w);
  }

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state">no windows match the current filter</div>`;
    updateToggleAll([]);
    return;
  }

  const sessionOrder = orderedSessions([...bySession.keys()], bySession);
  const totals = new Map();
  for (const w of windows) totals.set(w.session, (totals.get(w.session) || 0) + 1);

  grid.innerHTML = sessionOrder
    .map((s) => {
      // Within a session, sort by recent user focus desc; index as tiebreak.
      const ws = bySession.get(s).slice().sort((a, b) => {
        const da = (b.focused_at || 0) - (a.focused_at || 0);
        if (da !== 0) return da;
        return a.index - b.index;
      });
      return renderSession(s, ws, totals.get(s));
    })
    .join("");

  updateToggleAll(sessionOrder);
}

export function render(windows) {
  // Dispatch on the view attribute the user toggled via the view-switch.
  // Defaults to grid when unset (first paint, or no localStorage entry).
  const view = document.body.dataset.view === "stream" ? "stream" : "grid";
  if (view === "stream") renderStream(windows);
  else renderGrid(windows);

  // Counts in header — same in both views. Lead with needs-input — that's
  // the only count that means "drop what you're doing"; renders only when
  // nonzero. Each count is its own classed span so CSS colors them by
  // status.
  const total = windows.length;
  const needsInput = windows.filter((w) => w.state === "needs-input").length;
  const working = windows.filter((w) => w.state === "working").length;
  const waiting = windows.filter((w) => w.state === "waiting").length;
  const sep = `<span class="count-sep">·</span>`;
  const segments = [`<span><b>${total}</b> windows</span>`];
  if (needsInput) segments.push(`<span class="count-needs">${needsInput} needs input</span>`);
  segments.push(`<span class="count-working">${working} working</span>`);
  segments.push(`<span class="count-waiting">${waiting} waiting</span>`);
  counts.innerHTML = segments.join(` ${sep} `);
}

function startRename(nameEl, target, currentName) {
  if (state.editingTarget) return;
  state.editingTarget = target;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "rename-input";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    state.editingTarget = null;
    if (save && newName && newName !== currentName) {
      try {
        await fetch(`/api/rename?${targetQuery(target)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newName }),
        });
      } catch (e) {
        // poll() below will resync from tmux either way
      }
    }
    poll();  // immediate refresh so the new name appears
  };

  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
}

async function handleAutoRename(autoBtn) {
  if (autoBtn.dataset.busy) return;
  const session = autoBtn.dataset.session;
  autoBtn.dataset.busy = "1";
  const orig = autoBtn.innerHTML;
  autoBtn.innerHTML = "✨ thinking…";
  autoBtn.disabled = true;
  try {
    const res = await fetch(
      `/api/auto-rename-session?session=${encodeURIComponent(session)}`,
      { method: "POST" }
    );
    const data = await res.json();
    if (!data.ok) {
      autoBtn.innerHTML = `✗ ${escapeHtml(data.error || "failed").slice(0, 40)}`;
      setTimeout(() => { autoBtn.innerHTML = orig; }, 4000);
    } else {
      const n = (data.applied || []).length;
      autoBtn.innerHTML = n ? `✓ renamed ${n}` : "✓ all good";
      setTimeout(() => { autoBtn.innerHTML = orig; }, 2500);
      poll();
    }
  } catch (err) {
    autoBtn.innerHTML = `✗ ${err.message}`.slice(0, 40);
    setTimeout(() => { autoBtn.innerHTML = orig; }, 4000);
  } finally {
    autoBtn.disabled = false;
    delete autoBtn.dataset.busy;
  }
}

async function handleKillSession(btn) {
  const session = btn.dataset.session;
  const n = state.lastWindows.filter((w) => w.session === session).length;
  const msg = `Kill session '${session}'?\n\nCloses ${n} window${n === 1 ? "" : "s"} and detaches any attached client.`;
  if (!confirm(msg)) return;
  await apiCall("kill session", `/api/session?session=${encodeURIComponent(session)}`, {
    method: "DELETE",
  });
  poll();
}

async function handleKillWindow(btn) {
  const target = btn.dataset.target;
  const name = btn.dataset.name;
  if (!confirm(`Kill window '${name}' (${target})?`)) return;
  await apiCall("kill window", `/api/window?${targetQuery(target)}`, {
    method: "DELETE",
  });
  poll();
}

async function handleNewWindow(btn) {
  const session = btn.dataset.session;
  const mode = btn.dataset.mode;
  const tile = btn.closest(".card-new");
  // Disable both buttons in the tile while the request is in flight so a
  // double-click can't spawn two windows.
  tile.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await apiCall(
      "new window",
      `/api/window/new?session=${encodeURIComponent(session)}&mode=${encodeURIComponent(mode)}`,
      { method: "POST" }
    );
  } finally {
    tile.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
  poll();
}

function reorderSessions(src, dst, before) {
  // Build the order from current DOM (so we capture auto-sorted positions of new sessions too)
  const all = [...grid.querySelectorAll(".session-group")].map(
    (g) => g.dataset.session
  );
  const without = all.filter((s) => s !== src);
  const dstIdx = without.indexOf(dst);
  const insertAt = before ? dstIdx : dstIdx + 1;
  without.splice(insertAt, 0, src);
  prefs.setSessionOrder(without);
  render(state.lastWindows);
}

function wireGrid() {
  if (toggleAllBtn) toggleAllBtn.addEventListener("click", handleToggleAll);

  grid.addEventListener("click", (e) => {
    // Mutation buttons inside the grid take priority over the more general
    // header-toggle / card-open handlers below.
    const autoBtn = e.target.closest(".auto-rename");
    if (autoBtn) {
      e.stopPropagation();
      handleAutoRename(autoBtn);
      return;
    }
    const killSessionBtn = e.target.closest(".kill-session");
    if (killSessionBtn) {
      e.stopPropagation();
      handleKillSession(killSessionBtn);
      return;
    }
    const killWindowBtn = e.target.closest(".card-kill");
    if (killWindowBtn) {
      e.stopPropagation();
      handleKillWindow(killWindowBtn);
      return;
    }
    const newWindowBtn = e.target.closest(".new-window");
    if (newWindowBtn) {
      e.stopPropagation();
      handleNewWindow(newWindowBtn);
      return;
    }
    // Header click toggles collapse (unless the header is mid-drag).
    const header = e.target.closest(".session-header");
    if (header && !header.classList.contains("dragging")) {
      const session = header.dataset.session;
      if (state.collapsedSessions.has(session)) state.collapsedSessions.delete(session);
      else state.collapsedSessions.add(session);
      prefs.setCollapsed(state.collapsedSessions);
      header.closest(".session-group").classList.toggle("collapsed");
      return;
    }
    // Stream-row click: open modal. Stream rows don't carry a renameable
    // title surface, so no dblclick-defer needed — checked before .card so
    // the next branch's renameable-title logic doesn't apply.
    const streamRow = e.target.closest(".stream-row");
    if (streamRow) {
      openModal(streamRow.dataset.target);
      return;
    }
    // Card click: open modal, but defer if the click is on the name (so a
    // dblclick can win and start a rename instead).
    const card = e.target.closest(".card");
    if (!card) return;
    const target = card.dataset.target;
    const onName = !!e.target.closest(".card-title");
    if (!onName) {
      openModal(target);
      return;
    }
    if (nameClickTimers.has(target)) return;
    const timer = setTimeout(() => {
      nameClickTimers.delete(target);
      openModal(target);
    }, 220);
    nameClickTimers.set(target, timer);
  });

  grid.addEventListener("dblclick", (e) => {
    const nameEl = e.target.closest(".card-title");
    if (!nameEl) return;
    const card = nameEl.closest(".card");
    const target = card.dataset.target;
    e.stopPropagation();
    const timer = nameClickTimers.get(target);
    if (timer) {
      clearTimeout(timer);
      nameClickTimers.delete(target);
    }
    startRename(nameEl, target, nameEl.textContent);
  });

  // Drag-and-drop session reordering — delegated. dragstart/dragover/etc all
  // bubble, so a single listener on grid covers every header.
  grid.addEventListener("dragstart", (e) => {
    const header = e.target.closest(".session-header");
    if (!header) return;
    header.classList.add("dragging");
    e.dataTransfer.setData("text/plain", header.dataset.session);
    e.dataTransfer.effectAllowed = "move";
  });

  grid.addEventListener("dragend", (e) => {
    const header = e.target.closest(".session-header");
    if (header) header.classList.remove("dragging");
    grid.querySelectorAll(".session-header").forEach((h) => {
      h.classList.remove("drag-over-top", "drag-over-bottom");
    });
  });

  grid.addEventListener("dragover", (e) => {
    const header = e.target.closest(".session-header");
    if (!header) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    header.classList.toggle("drag-over-top", before);
    header.classList.toggle("drag-over-bottom", !before);
  });

  grid.addEventListener("dragleave", (e) => {
    const header = e.target.closest(".session-header");
    if (header) header.classList.remove("drag-over-top", "drag-over-bottom");
  });

  grid.addEventListener("drop", (e) => {
    const header = e.target.closest(".session-header");
    if (!header) return;
    e.preventDefault();
    const src = e.dataTransfer.getData("text/plain");
    const dst = header.dataset.session;
    if (src === dst) return;
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    reorderSessions(src, dst, before);
  });
}

export async function poll() {
  if (state.editingTarget) return;  // user is mid-rename; don't blow away their input
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    state.lastWindows = data.windows;
    render(state.lastWindows);
    updateUsagePill(data.usage_scrape, data.usage);
    lastUpdate.textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    lastUpdate.textContent = `poll failed: ${e.message}`;
  }
}

function fmtTokens(n) {
  if (!n) return "0";
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}

function fmtResetCountdown(epochSec) {
  if (!epochSec) return "5h window open";
  const diff = epochSec - Math.floor(Date.now() / 1000);
  if (diff <= 0) return "resets now";
  if (diff < 60) return `resets in ${diff}s`;
  if (diff < 3600) return `resets in ${Math.floor(diff / 60)}m`;
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return `resets in ${h}h ${m}m`;
}

function meterBar(label, pct, resets) {
  const tone = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
  return `
    <div class="usage-item" title="${escapeHtml(label)} — ${pct}% used. Resets ${escapeHtml(resets || "")}">
      <span class="usage-item-label">${escapeHtml(label)}</span>
      <span class="usage-item-bar"><span class="usage-item-fill ${tone}" style="width:${pct}%"></span></span>
      <b>${pct}%</b>
    </div>
  `;
}

function updateUsagePill(scraped, fallback) {
  if (!usageEl) return;
  // Prefer the scraped TUI data (real plan percentages). Fall back to the
  // JSONL-derived 5h pill when the scrape hasn't completed yet (first ~20s
  // after server start) or failed.
  if (scraped && scraped.available && scraped.meters) {
    const m = scraped.meters;
    const order = ["session", "week_all", "week_sonnet"];
    const compactLabels = { session: "session", week_all: "week", week_sonnet: "sonnet" };
    usageEl.classList.remove("usage-fallback");
    usageEl.innerHTML = order
      .filter((k) => m[k])
      .map((k) => meterBar(compactLabels[k], m[k].percent, m[k].resets))
      .join("");
    usageEl.title = order
      .filter((k) => m[k])
      .map((k) => `${m[k].label}: ${m[k].percent}% used\n  Resets ${m[k].resets}`)
      .join("\n\n");
    return;
  }
  if (!fallback || !fallback.available) {
    usageEl.classList.remove("usage-fallback");
    usageEl.textContent = "";
    usageEl.title = "";
    return;
  }
  const active = (fallback.input_tokens || 0) + (fallback.cache_creation_tokens || 0) + (fallback.output_tokens || 0);
  usageEl.classList.add("usage-fallback");
  usageEl.textContent = `5h: ${fmtTokens(active)} · ${fmtResetCountdown(fallback.reset_at)}`;
  usageEl.title = `Claude Code plan usage estimate (JSONL-derived; scrape not yet ready)\n` +
    `  ${fallback.messages} assistant messages\n` +
    `  ${fmtTokens(active)} active tokens\n` +
    `  ${fmtTokens(fallback.cache_read_tokens)} cache reads (discounted)`;
}

export function initGrid() {
  wireGrid();
  poll();
  setInterval(poll, POLL_MS);
}
