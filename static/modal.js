// Modal lifecycle: open/close, header refresh, inline rename, auto-rename,
// image-paste forwarding.
//
// Imports `poll` from grid.js to refresh card data after a rename/auto-rename.
// This is a circular import (grid.js imports openModal from here) — it works
// because `poll` is a function declaration (hoisted) and is only called inside
// event handlers, never at module top level.

import { state } from './state.js';
import { escapeHtml, targetQuery, apiCall, relTime } from './util.js';
import { startLiveTerminal, stopLiveTerminal, writeTerminalLine } from './terminal.js';
import { poll } from './grid.js';

const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalXtermEl = document.getElementById("modal-xterm");
const modalFocus = document.getElementById("modal-focus");
const modalClose = document.getElementById("modal-close");
const modalSubtitle = document.getElementById("modal-subtitle");
const modalAutoRename = document.getElementById("modal-auto-rename");
const modalSide = document.getElementById("modal-side");

const MODAL_POLL_MS = 1500;
let modalPollHandle = null;

export function openModal(target) {
  state.activeTarget = target;
  modalTitle.textContent = target;
  modalSubtitle.innerHTML = "";
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  startLiveTerminal(target, { onCloseRequested: closeModal });
  // Header poll keeps the subtitle/brief/spinner fresh; the terminal body
  // itself streams live via the WebSocket, no polling needed.
  refreshModalHeader();
  modalPollHandle = setInterval(refreshModalHeader, MODAL_POLL_MS);
}

export function closeModal() {
  stopLiveTerminal();
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalPollHandle) {
    clearInterval(modalPollHandle);
    modalPollHandle = null;
  }
  if (modalSide) modalSide.innerHTML = "";
  state.modalRenaming = false;
  state.activeTarget = null;
}

async function refreshModalHeader() {
  // /api/pane is now used only for parsed status fields (branch, PR, recap,
  // spinner). The terminal content itself streams live via WebSocket and
  // doesn't need this poll. lines=80 is enough buffer for the parser to find
  // the status block and most recent recap.
  if (!state.activeTarget) return;
  if (state.modalRenaming) return;  // don't clobber the in-flight rename input
  try {
    const res = await fetch(`/api/pane?${targetQuery(state.activeTarget)}&lines=80`);
    if (!res.ok) return;
    const data = await res.json();
    updateModalHeader(data);
  } catch (_) {
    // Transient — next tick will retry
  }
}

function updateModalHeader(data) {
  // Title: window name (prominent), then session and cwd in dim text.
  // tmux window index is intentionally omitted — not useful for orientation.
  const name = data.name || data.target;
  const titleParts = [`<span class="modal-name">${escapeHtml(name)}</span>`];
  if (data.session) {
    titleParts.push(`<span class="modal-session">${escapeHtml(data.session)}</span>`);
  }
  if (data.cwd) {
    titleParts.push(`<span class="modal-cwd mono">${escapeHtml(data.cwd)}</span>`);
  }
  modalTitle.innerHTML = titleParts.join("");

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
  if (data.state === "needs-input") {
    parts.push(
      `<span class="spinner-tag" style="color: var(--s-needs); font-weight: 600;">⚠ needs input</span>`
    );
  } else if (data.spinner) {
    parts.push(
      `<span class="spinner-tag">✻ ${escapeHtml(data.spinner.toLowerCase())}…</span>`
    );
  } else if (data.pending_input) {
    parts.push(
      `<span class="spinner-tag" style="color: var(--fg-3); font-style: normal;">↗ pending</span>`
    );
  }
  modalSubtitle.innerHTML = parts.join(`<span class="sep">·</span> `);
  renderModalSidebar(data);
}

// ── Sidebar: Linked (PR + Linear placeholder) + Activity timeline. ───
// Data rides on the existing 1.5s /api/pane poll — no extra request.
function renderModalSidebar(data) {
  if (!modalSide) return;
  modalSide.innerHTML = `
    <section class="modal-side-section">
      <h4>Linked</h4>
      ${renderPRCard(data)}
      ${renderLinearPlaceholder()}
    </section>
    <section class="modal-side-section modal-side-activity">
      <h4>Activity</h4>
      ${renderActivityTimeline(data.activity)}
    </section>
  `;
}

function avatarChars(handle) {
  if (!handle) return "?";
  // GitHub usernames carry dashes/underscores; strip and take the first two
  // letters for the avatar bubble (Inter, 2-char max per design).
  const letters = handle.replace(/[^A-Za-z0-9]/g, "");
  return (letters.slice(0, 2) || handle.slice(0, 1)).toUpperCase();
}

function renderPRCard(data) {
  if (!data.pr) {
    return `<button class="modal-side-link-btn" type="button" disabled title="link a PR — coming soon">+ link pull request</button>`;
  }
  const ciState = data.ci === "✓" ? "passing"
    : data.ci === "✗" ? "failing"
    : data.ci === "⟳" ? "running"
    : "—";
  const ciClass = data.ci === "✓" ? "ci-passing"
    : data.ci === "✗" ? "ci-failing"
    : data.ci === "⟳" ? "ci-running"
    : "";
  const draftPill = data.pr_draft
    ? `<span class="pr-mini pr-mini-draft">draft</span>`
    : `<span class="pr-mini pr-mini-open">open</span>`;
  const reviewers = (data.pr_reviewers || [])
    .map((r) => `<span class="modal-avatar" title="${escapeHtml(r)}">${escapeHtml(avatarChars(r))}</span>`)
    .join("");
  const title = escapeHtml(data.pr_title || "");
  const url = `https://github.com/faradayio/fdy/pull/${data.pr}`;
  const adds = data.pr_additions || 0;
  const dels = data.pr_deletions || 0;
  return `
    <div class="modal-card-inset">
      <div class="pr-head">
        <a class="pr-num" href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${data.pr}</a>
        <span class="pr-title" title="${title}">${title}</span>
      </div>
      <div class="pr-meta">
        ${draftPill}
        <span class="pr-diff"><span class="diff-plus">+${adds}</span> <span class="diff-minus">−${dels}</span></span>
        <span class="pr-ci ${ciClass}"><span class="ci-dot"></span>ci ${ciState}</span>
        ${reviewers ? `<span class="pr-reviewers">${reviewers}</span>` : ""}
      </div>
    </div>
  `;
}

function renderLinearPlaceholder() {
  // Linear connector is not wired yet (deferred to Phase 3.1 / a future
  // skill). The disabled button keeps the visual slot — it'll become live
  // when the connector lands without modal-layout churn.
  return `<button class="modal-side-link-btn" type="button" disabled title="Linear integration coming soon">+ link Linear ticket</button>`;
}

function timelineColor(kind, evState) {
  if (kind === "commit") return "var(--s-shell)";
  if (kind === "ci") {
    if (evState === "failed") return "var(--s-danger)";
    if (evState === "running") return "var(--s-working)";
    return "var(--s-success)";
  }
  if (kind === "open") return "var(--fg-3)";
  return "var(--fg-3)";
}

function timelineLabel(kind, evState) {
  if (kind === "commit") return "commit";
  if (kind === "ci") return evState ? `ci ${evState}` : "ci";
  if (kind === "open") return "opened";
  return kind;
}

function renderActivityTimeline(events) {
  if (!events || events.length === 0) {
    return `<div class="timeline-empty">no recent activity</div>`;
  }
  return `
    <ol class="timeline">
      ${events.map((e) => `
        <li class="timeline-row" data-kind="${escapeHtml(e.kind)}">
          <span class="timeline-dot" style="background:${timelineColor(e.kind, e.state)}"></span>
          <div class="timeline-body">
            <div class="timeline-text">${escapeHtml(e.text || "")}</div>
            <div class="timeline-when">${escapeHtml(timelineLabel(e.kind, e.state))} · ${escapeHtml(relTime(e.at))} ago</div>
          </div>
        </li>
      `).join("")}
    </ol>
  `;
}

function startModalRename() {
  if (!state.activeTarget || state.modalRenaming) return;
  const nameSpan = modalTitle.querySelector(".modal-name");
  if (!nameSpan) return;
  const currentName = nameSpan.textContent;
  state.modalRenaming = true;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "rename-input modal-rename-input";
  nameSpan.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    state.modalRenaming = false;
    if (save && newName && newName !== currentName && state.activeTarget) {
      await fetch(`/api/rename?${targetQuery(state.activeTarget)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
    }
    refreshModalHeader();
    poll();  // also refresh cards on the grid
  };

  // stopPropagation so Esc/Enter don't escape to the document handler
  // (which would close the modal) or the xterm terminal.
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

async function handleModalAutoRename() {
  if (!state.activeTarget || modalAutoRename.dataset.busy) return;
  modalAutoRename.dataset.busy = "1";
  const orig = modalAutoRename.textContent;
  modalAutoRename.textContent = "✨ thinking…";
  modalAutoRename.disabled = true;
  try {
    const data = await apiCall(
      "auto-rename window",
      `/api/auto-rename-window?${targetQuery(state.activeTarget)}`,
      { method: "POST" }
    );
    if (data) {
      refreshModalHeader();
      poll();
    }
  } finally {
    modalAutoRename.textContent = orig;
    modalAutoRename.disabled = false;
    delete modalAutoRename.dataset.busy;
  }
}

export function initModal() {
  modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  modalFocus.addEventListener("click", async () => {
    if (!state.activeTarget) return;
    await fetch(`/api/focus?${targetQuery(state.activeTarget)}`, { method: "POST" });
  });

  // Double-click the window name in the modal header to rename it. The
  // `.modal-name` span is rebuilt every poll by updateModalHeader, so delegate
  // from the persistent <h2> instead of attaching per-render.
  modalTitle.addEventListener("dblclick", (e) => {
    if (!e.target.closest(".modal-name")) return;
    e.stopPropagation();
    startModalRename();
  });

  modalAutoRename.addEventListener("click", (e) => {
    e.stopPropagation();
    handleModalAutoRename();
  });

  // Image paste: when the user pastes a screenshot (or any image) into the
  // modal, upload the bytes to the server, which writes a temp file and
  // bracketed-pastes "@/tmp/foo.png " into the pane so Claude Code reads it as
  // a file reference. Text pastes are ignored here and fall through to xterm's
  // own paste handling. Capture phase so we see the event before xterm's
  // hidden textarea consumes it.
  modalXtermEl.addEventListener("paste", async (e) => {
    if (!state.activeTarget) return;
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      e.stopPropagation();
      try {
        const res = await fetch(`/api/paste-image?${targetQuery(state.activeTarget)}`, {
          method: "POST",
          headers: { "Content-Type": blob.type || "image/png" },
          body: blob,
        });
        const data = await res.json();
        if (!data.ok) {
          writeTerminalLine(`\r\n\x1b[31m[periscope: image paste failed: ${data.error}]\x1b[0m`);
        }
      } catch (err) {
        writeTerminalLine(`\r\n\x1b[31m[periscope: image paste error: ${err.message}]\x1b[0m`);
      }
      return;
    }
  }, true);
}
