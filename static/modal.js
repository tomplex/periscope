// Modal lifecycle: open/close, header refresh, inline rename, auto-rename,
// image-paste forwarding.
//
// Imports `poll` from grid.js to refresh card data after a rename/auto-rename.
// This is a circular import (grid.js imports openModal from here) — it works
// because `poll` is a function declaration (hoisted) and is only called inside
// event handlers, never at module top level.

import { state } from './state.js';
import { escapeHtml, targetQuery, apiCall } from './util.js';
import { startLiveTerminal, stopLiveTerminal, writeTerminalLine } from './terminal.js';
import { poll } from './grid.js';

const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalXtermEl = document.getElementById("modal-xterm");
const modalFocus = document.getElementById("modal-focus");
const modalClose = document.getElementById("modal-close");
const modalSubtitle = document.getElementById("modal-subtitle");
const modalAutoRename = document.getElementById("modal-auto-rename");

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
      `<span class="spinner-tag" style="color: var(--needs-input); font-weight: 600;">⚠ needs input</span>`
    );
  } else if (data.spinner) {
    parts.push(
      `<span class="spinner-tag">✻ ${escapeHtml(data.spinner.toLowerCase())}…</span>`
    );
  } else if (data.pending_input) {
    parts.push(
      `<span class="spinner-tag" style="color: var(--fg-dim); font-style: normal;">↗ pending</span>`
    );
  }
  modalSubtitle.innerHTML = parts.join(`<span class="sep">·</span> `);
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
