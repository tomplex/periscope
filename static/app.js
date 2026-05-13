const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalXtermEl = document.getElementById("modal-xterm");
const modalFocus = document.getElementById("modal-focus");
const modalClose = document.getElementById("modal-close");
const sendInput = document.getElementById("send-input");
const keyButtons = document.getElementById("key-buttons");
const modalSubtitle = document.getElementById("modal-subtitle");
const modalBrief = document.getElementById("modal-brief");
const sendStatus = document.getElementById("send-status");

// Live terminal (xterm.js + WebSocket) wiring. The xterm instance is created
// fresh per modal-open and disposed on close, so each pane gets a clean state.
let term = null;
let termWs = null;

const MODAL_POLL_MS = 1500;
let modalPollHandle = null;
let lastSpinner = null;          // last detected spinner verb (for hysteresis)
let lastSpinnerSeenAt = 0;       // epoch ms of last positive spinner detection
let sendStatusTimer = null;      // protects the transient "sending…" text

// Tmux capture-pane sometimes catches Claude's TUI mid-redraw, dropping the
// spinner line for one poll cycle even though Claude is still working. Keep
// showing "thinking" through brief gaps so the status doesn't flicker.
const SPINNER_GRACE_MS = 4000;

let currentFilter = "all";
let lastWindows = [];
let activeTarget = null;

// Persisted UI state
const ORDER_KEY = "periscope:sessionOrder";
const COLLAPSED_KEY = "periscope:collapsedSessions";

// One-time migration from the pre-rename keys. Reads the old value if present
// and the new key is empty, then deletes the old key. Safe to leave in place;
// after one load it's a no-op.
function migrateOldKey(oldK, newK) {
  const v = localStorage.getItem(oldK);
  if (v !== null && localStorage.getItem(newK) === null) {
    localStorage.setItem(newK, v);
  }
  if (v !== null) localStorage.removeItem(oldK);
}
migrateOldKey("work-dashboard:sessionOrder", ORDER_KEY);
migrateOldKey("work-dashboard:collapsedSessions", COLLAPSED_KEY);

function loadOrder() {
  try { return JSON.parse(localStorage.getItem(ORDER_KEY)) || []; }
  catch { return []; }
}
function saveOrder(order) {
  localStorage.setItem(ORDER_KEY, JSON.stringify(order));
}
function loadCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY)) || []); }
  catch { return new Set(); }
}
function saveCollapsed(set) {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
}

let collapsedSessions = loadCollapsed();
let editingTarget = null;  // pauses polling while a rename input is open

const filterButtons = document.querySelectorAll("#filters button");
filterButtons.forEach((b) => {
  b.addEventListener("click", () => {
    filterButtons.forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    currentFilter = b.dataset.filter;
    render(lastWindows);
  });
});

modalClose.addEventListener("click", closeModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

modalFocus.addEventListener("click", async () => {
  if (!activeTarget) return;
  await fetch(`/api/focus?${targetQuery(activeTarget)}`, { method: "POST" });
});

function targetQuery(target) {
  // target looks like "session:index" — but session may contain ":" if any
  // session name has one (rare in tmux but legal). Split on the last ":".
  const i = target.lastIndexOf(":");
  const session = target.slice(0, i);
  const index = target.slice(i + 1);
  return `session=${encodeURIComponent(session)}&index=${encodeURIComponent(index)}`;
}

function passesFilter(w) {
  if (currentFilter === "all") return true;
  if (currentFilter === "working") return w.state === "working";
  if (currentFilter === "waiting") return w.state === "waiting";
  if (currentFilter === "claude") return w.is_claude;
  if (currentFilter === "shell") return w.state === "shell";
  if (currentFilter === "ci-bad") return w.ci === "✗";
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

  const stateLabel = w.spinner
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
      </div>
      ${branch}
      ${snippet}
      ${footHtml}
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

function relTime(epochSec) {
  if (!epochSec) return "";
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

function sessionPill(ws) {
  const waiting = ws.filter((w) => w.state === "waiting").length;
  const working = ws.filter((w) => w.state === "working").length;
  const ciBad = ws.filter((w) => w.ci === "✗").length;
  const parts = [];
  if (waiting) parts.push(`${waiting} waiting`);
  if (working) parts.push(`${working} working`);
  if (ciBad) parts.push(`${ciBad} ✗`);
  if (!parts.length) parts.push(`${ws.length}`);
  let cls = "session-pill";
  if (ciBad) cls += " has-ci-bad";
  else if (waiting) cls += " has-waiting";
  else if (working) cls += " has-working";
  return `<span class="${cls}">${parts.join(" · ")}</span>`;
}

function renderSession(session, ws, totalWindows) {
  const shown = ws.length;
  const meta = shown === totalWindows
    ? `${totalWindows} windows`
    : `${shown}/${totalWindows} windows`;
  const collapsed = collapsedSessions.has(session) ? " collapsed" : "";
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
      </div>
      <div class="cards">
        ${ws.map(renderCard).join("")}
      </div>
    </section>
  `;
}

function render(windows) {
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

  // Counts in header
  const total = windows.length;
  const working = windows.filter((w) => w.state === "working").length;
  const waiting = windows.filter((w) => w.state === "waiting").length;
  counts.textContent = `${total} windows · ${working} working · ${waiting} waiting`;
}

function startRename(nameEl, target, currentName) {
  if (editingTarget) return;
  editingTarget = target;
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
    editingTarget = null;
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

// One-time event delegation on the grid root. Survives every render() rebuild
// without re-attaching handlers per element.
//
// Handlers dispatch by walking up from event.target via closest() to find the
// relevant card/header/button and resolve target/session from data-attributes.
const nameClickTimers = new Map();  // target -> setTimeout handle

function wireGrid() {
  grid.addEventListener("click", (e) => {
    // Auto-rename button takes priority (it's inside the header).
    const autoBtn = e.target.closest(".auto-rename");
    if (autoBtn) {
      e.stopPropagation();
      handleAutoRename(autoBtn);
      return;
    }
    // Header click toggles collapse (unless the header is mid-drag).
    const header = e.target.closest(".session-header");
    if (header && !header.classList.contains("dragging")) {
      const session = header.dataset.session;
      if (collapsedSessions.has(session)) collapsedSessions.delete(session);
      else collapsedSessions.add(session);
      saveCollapsed(collapsedSessions);
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
  render(lastWindows);
}

async function openModal(target) {
  activeTarget = target;
  modalTitle.textContent = target;
  modalSubtitle.innerHTML = "";
  modalBrief.classList.add("hidden");
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  resetHistoryNav();
  setSendStatus("", "");
  lastSpinner = null;
  lastSpinnerSeenAt = 0;
  startLiveTerminal(target);
  // Header poll keeps the subtitle/brief/spinner fresh; the terminal body
  // itself streams live via the WebSocket, no polling needed.
  await refreshModalHeader();
  sendInput.value = loadDraft(target);
  // The xterm.js terminal owns focus by default; this only matters if the
  // user clicks into the textarea to compose a long message.
  modalPollHandle = setInterval(refreshModalHeader, MODAL_POLL_MS);
}

function startLiveTerminal(target) {
  // Fresh xterm.js instance per modal-open. Dispose any leftover from a prior
  // session before creating a new one.
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
  modalXtermEl.innerHTML = "";

  term = new Terminal({
    fontFamily: '"SF Mono", "JetBrains Mono", "Menlo", monospace',
    fontSize: 12,
    cursorBlink: true,
    scrollback: 5000,
    convertEol: false,
    theme: {
      background: "#0d1117",
      foreground: "#e6edf3",
      cursor: "#58a6ff",
      cursorAccent: "#0d1117",
      selectionBackground: "rgba(88,166,255,0.35)",
      black: "#1d1f21",        red: "#cc6666",  green: "#b5bd68",
      yellow: "#f0c674",       blue: "#81a2be", magenta: "#b294bb",
      cyan: "#8abeb7",         white: "#c5c8c6",
      brightBlack: "#969896",  brightRed: "#ff7373",
      brightGreen: "#c8e094",  brightYellow: "#ffd47b",
      brightBlue: "#9ec5fe",   brightMagenta: "#d8b6db",
      brightCyan: "#a8e0d8",   brightWhite: "#ffffff",
    },
  });
  term.open(modalXtermEl);
  term.focus();

  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  termWs = new WebSocket(`${wsProto}://${location.host}/ws/pane?${targetQuery(target)}`);
  termWs.binaryType = "arraybuffer";

  termWs.onmessage = (event) => {
    if (typeof event.data === "string") {
      // Control message (JSON) — currently only `{type:"size", cols, rows}`.
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "size") {
          term.resize(msg.cols, msg.rows);
        }
      } catch (_) {
        // Not JSON; treat as data
        term.write(event.data);
      }
    } else {
      // Binary terminal bytes
      term.write(new Uint8Array(event.data));
    }
  };

  termWs.onerror = () => {
    term.writeln("\r\n\x1b[31m[periscope: WebSocket error]\x1b[0m");
  };
  termWs.onclose = (e) => {
    if (!e.wasClean) {
      term.writeln("\r\n\x1b[33m[periscope: stream closed]\x1b[0m");
    }
  };

  // Forward every keystroke (including escape sequences for arrows, esc, etc.)
  // straight to the server. The server passes them through tmux send-keys -l.
  term.onData((data) => {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(data);
    }
  });
}

function stopLiveTerminal() {
  if (termWs) {
    try { termWs.close(); } catch (_) {}
    termWs = null;
  }
  if (term) {
    try { term.dispose(); } catch (_) {}
    term = null;
  }
}

async function refreshModalHeader() {
  // /api/pane is now used only for parsed status fields (branch, PR, recap,
  // spinner). The terminal content itself streams live via WebSocket and
  // doesn't need this poll. We pass lines=40 since we only need enough buffer
  // for the parser to find the status block and most recent recap.
  if (!activeTarget) return;
  try {
    const res = await fetch(`/api/pane?${targetQuery(activeTarget)}&lines=80`);
    if (!res.ok) return;
    const data = await res.json();
    applySpinnerHysteresis(data);
    updateModalHeader(data);
    updateSendStatusFromPane(data);
  } catch (_) {
    // Transient — next tick will retry
  }
}

function updateModalHeader(data) {
  // Title: target + curated window name (e.g. "main:1 — SUPERVISOR")
  const nameSuffix = data.name && data.name !== data.target ? ` — ${data.name}` : "";
  modalTitle.textContent = `${data.target}${nameSuffix}`;

  // Subtitle: branch · PR · CI · context% · model · spinner
  const parts = [];
  if (data.branch) parts.push(`<span class="mono">${escapeHtml(data.branch)}</span>`);
  if (data.pr) {
    const ciCls = data.ci === "✓" ? "ci-ok" : data.ci === "✗" ? "ci-bad" : "ci-pending";
    const ci = data.ci ? `<span class="${ciCls}">${data.ci}</span>` : "";
    parts.push(
      `<a class="pr" href="https://github.com/faradayio/fdy/pull/${data.pr}" target="_blank" rel="noopener">#${data.pr}</a> ${ci}`
    );
  }
  if (data.context_pct != null) parts.push(`${data.context_pct}%`);
  if (data.model) parts.push(escapeHtml(data.model.replace(/\s*\(.*\)/, "")));
  if (data.spinner) {
    parts.push(
      `<span class="spinner-tag">✻ ${escapeHtml(data.spinner.toLowerCase())}…</span>`
    );
  } else if (data.pending_input) {
    parts.push(
      `<span class="spinner-tag" style="color: var(--fg-dim); font-style: normal;">↗ pending</span>`
    );
  }
  modalSubtitle.innerHTML = parts.join(`<span class="sep">·</span> `);

  // Brief: most recent recap
  if (data.recap) {
    modalBrief.textContent = `※ ${data.recap}`;
    modalBrief.classList.remove("hidden");
  } else {
    modalBrief.classList.add("hidden");
  }
}

function setSendStatus(text, kind) {
  sendStatus.textContent = text;
  sendStatus.className = "send-status" + (kind ? ` ${kind}` : "");
}

function applySpinnerHysteresis(data) {
  // Tmux capture-pane occasionally catches Claude's TUI mid-redraw and the
  // spinner line is briefly absent from the snapshot. We treat any positive
  // detection as sticky for SPINNER_GRACE_MS and mutate data.spinner so every
  // downstream consumer (modal subtitle, send-status, etc.) sees the same
  // smoothed value instead of toggling.
  if (data.spinner) {
    lastSpinner = data.spinner;
    lastSpinnerSeenAt = Date.now();
    return;
  }
  if (lastSpinner && Date.now() - lastSpinnerSeenAt < SPINNER_GRACE_MS) {
    data.spinner = lastSpinner;
    return;
  }
  lastSpinner = null;
}

function updateSendStatusFromPane(data) {
  // Only the polled refresh drives this — the transient "sending…" set by
  // submitText takes precedence for ~600ms (sendStatusTimer guards it).
  if (sendStatusTimer) return;
  if (data.spinner) {
    setSendStatus(`Claude is ${data.spinner.toLowerCase()}…`, "thinking");
  } else {
    setSendStatus("", "");
  }
}

// --- Send history (Up/Down to recall, per-target) ---

const SEND_HISTORY_KEY = "periscope:sendHistory";
const HISTORY_MAX = 20;
let historyIndex = null;  // null = live draft; 0+ = entry in history (0 = most recent)
let liveDraft = "";

function loadSendHistory() {
  try { return JSON.parse(localStorage.getItem(SEND_HISTORY_KEY)) || {}; }
  catch { return {}; }
}
function saveSendHistory(h) {
  localStorage.setItem(SEND_HISTORY_KEY, JSON.stringify(h));
}
function pushSendHistory(target, msg) {
  if (!msg) return;
  const h = loadSendHistory();
  const arr = h[target] || [];
  if (arr[0] === msg) return;  // don't duplicate the most-recent entry
  arr.unshift(msg);
  h[target] = arr.slice(0, HISTORY_MAX);
  saveSendHistory(h);
}

// --- Per-target in-progress drafts ---
// Anything sitting in the textarea when the modal closes is preserved per
// target so reopening returns you to exactly where you left off. Cleared on
// successful submit.

const DRAFTS_KEY = "periscope:drafts";

function loadDrafts() {
  try { return JSON.parse(localStorage.getItem(DRAFTS_KEY)) || {}; }
  catch { return {}; }
}
function saveDraft(target, text) {
  const drafts = loadDrafts();
  if (text && text.trim()) drafts[target] = text;
  else delete drafts[target];
  localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
}
function loadDraft(target) {
  return loadDrafts()[target] || "";
}
function resetHistoryNav() {
  historyIndex = null;
  liveDraft = "";
}

function cursorOnFirstLine(input) {
  return input.value.slice(0, input.selectionStart).indexOf("\n") === -1;
}
function cursorOnLastLine(input) {
  return input.value.slice(input.selectionEnd).indexOf("\n") === -1;
}
function moveCursorToEnd(input) {
  const end = input.value.length;
  input.setSelectionRange(end, end);
}

function recallHistory(direction) {
  if (!activeTarget) return false;
  const history = loadSendHistory()[activeTarget] || [];
  if (history.length === 0) return false;
  if (direction === "older") {
    if (historyIndex === null) {
      liveDraft = sendInput.value;
      historyIndex = 0;
    } else if (historyIndex + 1 < history.length) {
      historyIndex++;
    } else {
      return true;  // already at oldest; consume key but don't change
    }
    sendInput.value = history[historyIndex];
    moveCursorToEnd(sendInput);
    setSendStatus(`history ${historyIndex + 1}/${history.length}`, "history");
    return true;
  }
  if (direction === "newer") {
    if (historyIndex === null) return false;  // not in history; let cursor move
    if (historyIndex === 0) {
      historyIndex = null;
      sendInput.value = liveDraft;
      moveCursorToEnd(sendInput);
      setSendStatus("", "");
      return true;
    }
    historyIndex--;
    sendInput.value = history[historyIndex];
    moveCursorToEnd(sendInput);
    setSendStatus(`history ${historyIndex + 1}/${history.length}`, "history");
    return true;
  }
  return false;
}

async function sendToTmux(payload) {
  if (!activeTarget) return;
  await fetch(`/api/send?${targetQuery(activeTarget)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  // Quick refresh so the user sees it land before the next poll tick.
  // The xterm stream picks up Claude's response automatically; we only need
  // to nudge the header refresh so the spinner/recap update quickly.
  setTimeout(refreshModalHeader, 80);
}

function sendKeys(keys) {
  return sendToTmux({ keys });
}

function closeModal() {
  // Preserve any in-progress prompt for this target — reopening restores it.
  if (activeTarget) saveDraft(activeTarget, sendInput.value);
  stopLiveTerminal();
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalPollHandle) {
    clearInterval(modalPollHandle);
    modalPollHandle = null;
  }
  if (sendStatusTimer) {
    clearTimeout(sendStatusTimer);
    sendStatusTimer = null;
  }
  resetHistoryNav();
  setSendStatus("", "");
  lastSpinner = null;
  lastSpinnerSeenAt = 0;
  activeTarget = null;
}

sendInput.addEventListener("keydown", (e) => {
  // History recall: Up at first line goes older, Down at last line goes newer.
  // Only intercept when the cursor would otherwise move past the buffer edge,
  // so multi-line editing inside the textarea still works.
  if (e.key === "ArrowUp" && cursorOnFirstLine(sendInput)) {
    if (recallHistory("older")) { e.preventDefault(); return; }
  }
  if (e.key === "ArrowDown" && cursorOnLastLine(sendInput)) {
    if (recallHistory("newer")) { e.preventDefault(); return; }
  }

  // Cmd/Ctrl+Enter submits. Bare Enter inserts a newline (default textarea
  // behavior — much friendlier for composing multi-line messages).
  const isSubmit = e.key === "Enter" && (e.metaKey || e.ctrlKey);
  if (!isSubmit) return;
  e.preventDefault();
  const text = sendInput.value;
  sendInput.value = "";
  if (activeTarget) {
    pushSendHistory(activeTarget, text.trim());
    saveDraft(activeTarget, "");  // submitted, so the draft is consumed
  }
  resetHistoryNav();
  // Transient "sending…" — protected for 600ms so the pane poll doesn't
  // overwrite it before the user gets feedback.
  setSendStatus("sending…", "sending");
  if (sendStatusTimer) clearTimeout(sendStatusTimer);
  sendStatusTimer = setTimeout(() => {
    sendStatusTimer = null;
    // The next pane poll (every 1.5s) will set "thinking…" if Claude is
    // working; otherwise updateSendStatusFromPane clears the line.
  }, 600);
  submitText(text);
});

// Any direct edit takes us out of history-recall mode (so Up/Down doesn't keep
// stomping on the user's typing).
sendInput.addEventListener("input", () => {
  if (historyIndex !== null) {
    historyIndex = null;
    liveDraft = sendInput.value;
    if (sendStatus.classList.contains("history")) setSendStatus("", "");
  }
});

function submitText(text) {
  // Trim leading/trailing newlines (incl. \r in case the browser produces
  // CRLF). Trailing newlines in paste content can collide with the explicit
  // Enter and produce a no-op submit on some TUIs.
  text = text.replace(/^[\r\n]+|[\r\n]+$/g, "");
  if (text === "") {
    sendKeys(["Enter"]);
    return;
  }
  // One unified path: bracketed paste for the content, then an explicit Enter
  // as the submit key. The server inserts a small delay between the two so
  // the receiving TUI applies the paste before submit fires (Claude Code's
  // input was racing the Enter under the old branched path).
  sendToTmux({ paste: text, keys: ["Enter"] });
}

keyButtons.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-keys]");
  if (!btn) return;
  const keys = JSON.parse(btn.dataset.keys);
  sendKeys(keys);
  sendInput.focus();
});

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function poll() {
  if (editingTarget) return;  // user is mid-rename; don't blow away their input
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    lastWindows = data.windows;
    render(lastWindows);
    lastUpdate.textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    lastUpdate.textContent = `poll failed: ${e.message}`;
  }
}

wireGrid();
poll();
setInterval(poll, POLL_MS);
