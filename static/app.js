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

function render(windows) {
  const filtered = windows.filter(passesFilter);
  const bySession = new Map();
  for (const w of filtered) {
    if (!bySession.has(w.session)) bySession.set(w.session, []);
    bySession.get(w.session).push(w);
  }

  // Sort sessions: those with any "waiting" or "working" first, then by name
  const sessionOrder = [...bySession.keys()].sort((a, b) => {
    const score = (s) => {
      const ws = bySession.get(s);
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

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state">no windows match the current filter</div>`;
    return;
  }

  grid.innerHTML = sessionOrder
    .map((s) => {
      const ws = bySession.get(s).sort((a, b) => a.index - b.index);
      const total = windows.filter((w) => w.session === s).length;
      const shown = ws.length;
      const meta = shown === total ? `${total} windows` : `${shown}/${total} windows`;
      return `
        <section class="session-group">
          <div class="session-header">
            <h2>${escapeHtml(s)}</h2>
            <span class="session-meta">${meta}</span>
          </div>
          <div class="cards">
            ${ws.map(renderCard).join("")}
          </div>
        </section>
      `;
    })
    .join("");

  // Wire card clicks
  grid.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", () => openModal(el.dataset.target));
  });

  // Counts in header
  const total = windows.length;
  const working = windows.filter((w) => w.state === "working").length;
  const waiting = windows.filter((w) => w.state === "waiting").length;
  counts.textContent = `${total} windows · ${working} working · ${waiting} waiting`;
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
