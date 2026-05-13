// Grid rendering, /api/state polling, drag-reorder, and one-time event
// delegation on the grid root. Handlers walk up from event.target via
// closest() to find the relevant card/header/button and resolve
// target/session from data-attributes — so a render() innerHTML rebuild
// doesn't invalidate any listeners.

import { state, loadOrder, saveOrder, saveCollapsed } from './state.js';
import { escapeHtml, targetQuery, apiCall, relTime } from './util.js';
import { openModal } from './modal.js';

const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const usageEl = document.getElementById("usage");

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
  const cls = ci === "✓" ? "ci-ok" : ci === "✗" ? "ci-bad" : "ci-pending";
  return `<span class="${cls}">${ci}</span>`;
}

function renderCard(w) {
  const stateClass = `state-${w.state}`;
  const ciBad = w.ci === "✗" ? " ci-bad" : "";
  const branch = w.branch
    ? `<div class="card-branch">
         <span>${escapeHtml(w.branch)}</span>
         ${w.git ? `<span>· ${escapeHtml(w.git)}</span>` : ""}
         ${
           w.pr
             ? `<a class="pr" href="https://github.com/faradayio/fdy/pull/${w.pr}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${w.pr}</a> ${ciSpan(w.ci)}`
             : ""
         }
       </div>`
    : "";

  let snippet = "";
  if (w.pending_input) {
    snippet = `<div class="card-snippet pending">❯ ${escapeHtml(w.pending_input)}</div>`;
  } else if (w.recap) {
    snippet = `<div class="card-snippet recap">※ ${escapeHtml(w.recap)}</div>`;
  } else if (w.last_line) {
    snippet = `<div class="card-snippet">${escapeHtml(w.last_line)}</div>`;
  }

  // needs-input wins over the spinner verb (a stale "envisioning…" in
  // scrollback shouldn't drown out the blocking prompt). For other states,
  // show the spinner phrase if we have one, else the bare state name.
  const stateLabel = w.state === "needs-input"
    ? `<span class="card-state ${stateClass}">needs input</span>`
    : w.spinner
      ? `<span class="card-state ${stateClass}">${escapeHtml(w.spinner.toLowerCase())}…</span>`
      : `<span class="card-state ${stateClass}">${w.state}</span>`;

  const foot = [];
  if (w.context_pct != null) foot.push(`${w.context_pct}%`);
  if (w.model) foot.push(escapeHtml(w.model.replace(/\s*\(.*\)/, "")));
  const recent = relTime(w.focused_at);
  if (recent) foot.push(`viewed ${recent}`);
  const footHtml = foot.length
    ? `<div class="card-foot">${foot.join(" · ")}</div>`
    : "";

  return `
    <div class="card ${stateClass}${ciBad}" data-target="${w.target}">
      <div class="card-head">
        <span class="card-name">${escapeHtml(w.name)}</span>
        <span class="card-idx mono">${w.index}</span>
        ${stateLabel}
        <button class="card-kill" data-target="${w.target}" data-name="${escapeHtml(w.name)}" title="kill this window">✕</button>
      </div>
      ${branch}
      ${snippet}
      ${footHtml}
    </div>
  `;
}

function renderNewTile(session) {
  const s = escapeHtml(session);
  return `
    <div class="card card-new" data-session="${s}">
      <button class="new-window" data-session="${s}" data-mode="claude">+ claude</button>
      <button class="new-window" data-session="${s}" data-mode="shell">+ shell</button>
    </div>
  `;
}

function orderedSessions(allSessions, bySession) {
  // User-pinned (drag-reordered) sessions float to the top in saved order.
  // Everything else sorts by most-recent activity across its windows, descending.
  const saved = loadOrder();
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

export function render(windows) {
  const filtered = windows.filter(passesFilter);
  const bySession = new Map();
  for (const w of filtered) {
    if (!bySession.has(w.session)) bySession.set(w.session, []);
    bySession.get(w.session).push(w);
  }

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state">no windows match the current filter</div>`;
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

  // Counts in header. Lead with needs-input — that's the only count that
  // means "drop what you're doing and look here", so it earns top billing
  // and only renders when nonzero.
  const total = windows.length;
  const needsInput = windows.filter((w) => w.state === "needs-input").length;
  const working = windows.filter((w) => w.state === "working").length;
  const waiting = windows.filter((w) => w.state === "waiting").length;
  const parts = [`${total} windows`];
  if (needsInput) parts.push(`${needsInput} needs input`);
  parts.push(`${working} working`, `${waiting} waiting`);
  counts.textContent = parts.join(" · ");
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
  saveOrder(without);
  render(state.lastWindows);
}

function wireGrid() {
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
      saveCollapsed(state.collapsedSessions);
      header.closest(".session-group").classList.toggle("collapsed");
      return;
    }
    // Card click: open modal, but defer if the click is on the name (so a
    // dblclick can win and start a rename instead).
    const card = e.target.closest(".card");
    if (!card) return;
    const target = card.dataset.target;
    const onName = !!e.target.closest(".card-name");
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
    const nameEl = e.target.closest(".card-name");
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
    updateUsagePill(data.usage);
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

function updateUsagePill(u) {
  if (!usageEl) return;
  if (!u || !u.available) {
    usageEl.textContent = "";
    usageEl.title = "";
    return;
  }
  // Headline: "active" tokens = everything except cache_read, which is
  // heavily discounted and would massively overstate plan-spend. Tooltip
  // shows the full breakdown so you can sanity-check.
  const active = (u.input_tokens || 0) + (u.cache_creation_tokens || 0) + (u.output_tokens || 0);
  usageEl.textContent = `5h: ${fmtTokens(active)} · ${fmtResetCountdown(u.reset_at)}`;
  usageEl.title =
    `Claude Code plan usage, last ${u.window_hours}h:\n` +
    `  ${u.messages} assistant messages\n` +
    `  ${fmtTokens(u.input_tokens)} fresh input\n` +
    `  ${fmtTokens(u.cache_creation_tokens)} cache creation\n` +
    `  ${fmtTokens(u.output_tokens)} output\n` +
    `  ─\n` +
    `  ${fmtTokens(active)} active total (above)\n` +
    `  ${fmtTokens(u.cache_read_tokens)} cache reads (heavily discounted)\n` +
    `  ${fmtTokens(u.total_tokens)} grand total`;
}

export function initGrid() {
  wireGrid();
  poll();
  setInterval(poll, POLL_MS);
}
