const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalPane = document.getElementById("modal-pane");
const modalFocus = document.getElementById("modal-focus");
const modalClose = document.getElementById("modal-close");

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
  // Apply user-saved order first; then auto-sort the rest by state priority.
  const saved = loadOrder();
  const present = new Set(allSessions);
  const ordered = saved.filter((s) => present.has(s));
  const remaining = allSessions.filter((s) => !ordered.includes(s));
  remaining.sort((a, b) => {
    const score = (s) => {
      const ws = bySession.get(s) || [];
      if (ws.some((w) => w.state === "waiting")) return 0;
      if (ws.some((w) => w.state === "working")) return 1;
      if (ws.some((w) => w.is_claude)) return 2;
      return 3;
    };
    const sa = score(a);
    const sb = score(b);
    if (sa !== sb) return sa - sb;
    return a.localeCompare(b);
  });
  return [...ordered, ...remaining];
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
      const ws = bySession.get(s).sort((a, b) => a.index - b.index);
      const total = windows.filter((w) => w.session === s).length;
      const shown = ws.length;
      const meta = shown === total ? `${total} windows` : `${shown}/${total} windows`;
      const collapsed = collapsedSessions.has(s) ? " collapsed" : "";
      return `
        <section class="session-group${collapsed}" data-session="${escapeHtml(s)}">
          <div class="session-header" draggable="true" data-session="${escapeHtml(s)}">
            <span class="chevron">▾</span>
            <h2>${escapeHtml(s)}</h2>
            <span class="session-meta">${meta}</span>
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
  const [s, i] = target.split(":");
  const res = await fetch(`/api/pane/${encodeURIComponent(s)}/${i}?lines=200`);
  const data = await res.json();
  modalPane.textContent = data.content;
  modalPane.scrollTop = modalPane.scrollHeight;
}

function closeModal() {
  modal.classList.add("hidden");
  activeTarget = null;
}

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
