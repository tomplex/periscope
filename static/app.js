const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalPane = document.getElementById("modal-pane");
const modalFocus = document.getElementById("modal-focus");
const modalClose = document.getElementById("modal-close");
const sendInput = document.getElementById("send-input");
const keyButtons = document.getElementById("key-buttons");

const MODAL_POLL_MS = 1500;
let modalPollHandle = null;

let currentFilter = "all";
let lastWindows = [];
let activeTarget = null;

// Persisted UI state
const ORDER_KEY = "work-dashboard:sessionOrder";
const COLLAPSED_KEY = "work-dashboard:collapsedSessions";

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
  const [s, i] = activeTarget.split(":");
  await fetch(`/api/focus/${encodeURIComponent(s)}/${i}`, { method: "POST" });
});

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
  const recent = relTime(w.activity);
  if (recent) foot.push(recent);
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
  const lastActivity = (s) =>
    Math.max(0, ...(bySession.get(s) || []).map((w) => w.activity || 0));
  remaining.sort((a, b) => {
    const da = lastActivity(b) - lastActivity(a);
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

  grid.innerHTML = sessionOrder
    .map((s) => {
      // Within a session, sort by recent activity desc; index as tiebreak.
      const ws = bySession.get(s).slice().sort((a, b) => {
        const da = (b.activity || 0) - (a.activity || 0);
        if (da !== 0) return da;
        return a.index - b.index;
      });
      const total = windows.filter((w) => w.session === s).length;
      const shown = ws.length;
      const meta = shown === total ? `${total} windows` : `${shown}/${total} windows`;
      const collapsed = collapsedSessions.has(s) ? " collapsed" : "";
      const recent = Math.max(0, ...ws.map((w) => w.activity || 0));
      const recentLabel = recent ? relTime(recent) : "";
      return `
        <section class="session-group${collapsed}" data-session="${escapeHtml(s)}">
          <div class="session-header" draggable="true" data-session="${escapeHtml(s)}">
            <span class="chevron">▾</span>
            <h2>${escapeHtml(s)}</h2>
            <span class="session-meta">${meta}${recentLabel ? ` · ${recentLabel}` : ""}</span>
            ${sessionPill(bySession.get(s))}
          </div>
          <div class="cards">
            ${ws.map(renderCard).join("")}
          </div>
        </section>
      `;
    })
    .join("");

  wireCards();
  wireSessionHeaders();

  // Counts in header
  const total = windows.length;
  const working = windows.filter((w) => w.state === "working").length;
  const waiting = windows.filter((w) => w.state === "waiting").length;
  counts.textContent = `${total} windows · ${working} working · ${waiting} waiting`;
}

function wireCards() {
  grid.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", () => openModal(el.dataset.target));
  });
}

function wireSessionHeaders() {
  grid.querySelectorAll(".session-header").forEach((header) => {
    const session = header.dataset.session;

    // Collapse on click (but not while dragging)
    header.addEventListener("click", (e) => {
      if (header.classList.contains("dragging")) return;
      if (collapsedSessions.has(session)) collapsedSessions.delete(session);
      else collapsedSessions.add(session);
      saveCollapsed(collapsedSessions);
      header.closest(".session-group").classList.toggle("collapsed");
    });

    // Drag-and-drop reordering
    header.addEventListener("dragstart", (e) => {
      header.classList.add("dragging");
      e.dataTransfer.setData("text/plain", session);
      e.dataTransfer.effectAllowed = "move";
    });

    header.addEventListener("dragend", () => {
      header.classList.remove("dragging");
      grid.querySelectorAll(".session-header").forEach((h) => {
        h.classList.remove("drag-over-top", "drag-over-bottom");
      });
    });

    header.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const rect = header.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      header.classList.toggle("drag-over-top", before);
      header.classList.toggle("drag-over-bottom", !before);
    });

    header.addEventListener("dragleave", () => {
      header.classList.remove("drag-over-top", "drag-over-bottom");
    });

    header.addEventListener("drop", (e) => {
      e.preventDefault();
      const src = e.dataTransfer.getData("text/plain");
      const dst = session;
      if (src === dst) return;
      const rect = header.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      reorderSessions(src, dst, before);
    });
  });
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
  modalPane.textContent = "loading...";
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  await refreshModalPane({ forceScroll: true });
  sendInput.value = "";
  sendInput.focus();
  modalPollHandle = setInterval(() => refreshModalPane(), MODAL_POLL_MS);
}

async function refreshModalPane({ forceScroll = false } = {}) {
  if (!activeTarget) return;
  const [s, i] = activeTarget.split(":");
  try {
    const res = await fetch(
      `/api/pane/${encodeURIComponent(s)}/${i}?lines=200`
    );
    const data = await res.json();
    const wasAtBottom =
      modalPane.scrollHeight - modalPane.scrollTop - modalPane.clientHeight < 24;
    modalPane.innerHTML = ansiToHtml(data.content);
    if (forceScroll || wasAtBottom) {
      modalPane.scrollTop = modalPane.scrollHeight;
    }
  } catch (e) {
    // swallow — next tick will retry
  }
}

async function sendToTmux(payload) {
  if (!activeTarget) return;
  const [s, i] = activeTarget.split(":");
  await fetch(`/api/send/${encodeURIComponent(s)}/${i}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  // Quick refresh so the user sees it land before the next poll tick.
  setTimeout(() => refreshModalPane(), 80);
}

function sendKeys(keys) {
  return sendToTmux({ keys });
}

// --- ANSI -> HTML ---------------------------------------------------------
// Supports SGR codes: reset, bold/dim/italic/underline, basic 16 colors,
// 256-color (38;5;N / 48;5;N), and truecolor (38;2;R;G;B / 48;2;R;G;B).

const ANSI_RE = /\x1b\[([\d;]*)m/g;

const ANSI_16 = [
  "#1d1f21", "#cc6666", "#b5bd68", "#f0c674",
  "#81a2be", "#b294bb", "#8abeb7", "#c5c8c6",
  "#969896", "#ff7373", "#c8e094", "#ffd47b",
  "#9ec5fe", "#d8b6db", "#a8e0d8", "#ffffff",
];

function ansi256(n) {
  if (n < 16) return ANSI_16[n];
  if (n < 232) {
    n -= 16;
    const r = Math.floor(n / 36);
    const g = Math.floor((n % 36) / 6);
    const b = n % 6;
    const v = (x) => (x === 0 ? 0 : 55 + x * 40);
    return `rgb(${v(r)},${v(g)},${v(b)})`;
  }
  const v = 8 + (n - 232) * 10;
  return `rgb(${v},${v},${v})`;
}

function applyCodes(s, style) {
  if (s === "" || s === "0") {
    for (const k of Object.keys(style)) delete style[k];
    return;
  }
  const codes = s.split(";").map((x) => parseInt(x, 10));
  let i = 0;
  while (i < codes.length) {
    const c = codes[i];
    if (Number.isNaN(c)) { i++; continue; }
    if (c === 0) {
      for (const k of Object.keys(style)) delete style[k];
    } else if (c === 1) style.bold = true;
    else if (c === 2) style.dim = true;
    else if (c === 3) style.italic = true;
    else if (c === 4) style.underline = true;
    else if (c === 22) { delete style.bold; delete style.dim; }
    else if (c === 23) delete style.italic;
    else if (c === 24) delete style.underline;
    else if (c === 39) delete style.fg;
    else if (c === 49) delete style.bg;
    else if (c >= 30 && c <= 37) style.fg = ANSI_16[c - 30];
    else if (c >= 40 && c <= 47) style.bg = ANSI_16[c - 40];
    else if (c >= 90 && c <= 97) style.fg = ANSI_16[c - 90 + 8];
    else if (c >= 100 && c <= 107) style.bg = ANSI_16[c - 100 + 8];
    else if (c === 38 || c === 48) {
      const target = c === 38 ? "fg" : "bg";
      if (codes[i + 1] === 5 && codes[i + 2] != null) {
        style[target] = ansi256(codes[i + 2]);
        i += 2;
      } else if (codes[i + 1] === 2 && codes[i + 4] != null) {
        style[target] = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`;
        i += 4;
      }
    }
    i++;
  }
}

function styleToCss(s) {
  const css = [];
  if (s.fg) css.push(`color:${s.fg}`);
  if (s.bg) css.push(`background:${s.bg}`);
  if (s.bold) css.push("font-weight:600");
  if (s.italic) css.push("font-style:italic");
  if (s.underline) css.push("text-decoration:underline");
  if (s.dim) css.push("opacity:0.65");
  return css.join(";");
}

function ansiToHtml(text) {
  let out = "";
  let last = 0;
  const style = {};
  let m;
  ANSI_RE.lastIndex = 0;
  while ((m = ANSI_RE.exec(text)) !== null) {
    const chunk = text.slice(last, m.index);
    if (chunk) {
      const css = styleToCss(style);
      out += css
        ? `<span style="${css}">${escapeHtml(chunk)}</span>`
        : escapeHtml(chunk);
    }
    last = m.index + m[0].length;
    applyCodes(m[1], style);
  }
  const tail = text.slice(last);
  if (tail) {
    const css = styleToCss(style);
    out += css
      ? `<span style="${css}">${escapeHtml(tail)}</span>`
      : escapeHtml(tail);
  }
  return out;
}

function closeModal() {
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalPollHandle) {
    clearInterval(modalPollHandle);
    modalPollHandle = null;
  }
  activeTarget = null;
}

sendInput.addEventListener("keydown", (e) => {
  // Enter submits; Shift+Enter inserts a newline in the textarea (default).
  // Cmd/Ctrl+Enter also submits — natural for power users.
  const isSubmit =
    e.key === "Enter" && !e.shiftKey;
  if (!isSubmit) return;
  e.preventDefault();
  const text = sendInput.value;
  sendInput.value = "";
  submitText(text);
});

function submitText(text) {
  if (text === "") {
    sendKeys(["Enter"]);
    return;
  }
  if (text.includes("\n")) {
    // Multi-line: bracketed paste delivers embedded newlines intact;
    // send-keys would silently strip them. Final Enter submits.
    sendToTmux({ paste: text, keys: ["Enter"] });
  } else {
    sendKeys([text, "Enter"]);
  }
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

poll();
setInterval(poll, POLL_MS);
