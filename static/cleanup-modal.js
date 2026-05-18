// Cleanup modal. Loads candidates from /api/cleanup/candidates, renders
// a checklist with signal badges, submits selected to /api/cleanup/archive.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("cleanup-modal");
const closeBtn = document.getElementById("cleanup-modal-close");
const cancelBtn = document.getElementById("cleanup-cancel");
const submitBtn = document.getElementById("cleanup-submit");
const listEl = document.getElementById("cleanup-modal-list");
const errorEl = document.getElementById("cleanup-modal-error");
const deleteBranchesBox = document.getElementById("cleanup-delete-branches");

let candidates = [];
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderRow(c, i) {
  const name = c.project_name || `(untracked: ${escapeHtml(c.pinned_dir.split("/").pop())})`;
  const branch = escapeHtml(c.branch);
  const badges = c.signals
    .map((s) => `<span class="cleanup-badge cleanup-badge-${s.kind}">${escapeHtml(s.label)}</span>`)
    .join(" ");
  const dirtyLabel = c.dirty ? `<span class="cleanup-dirty">⚠ dirty</span>` : "";
  // Dirty worktrees: rendered NOT checked. Healthy candidates: checked.
  const checked = c.dirty ? "" : "checked";
  const rowClass = c.dirty ? "cleanup-row cleanup-row-dirty" : "cleanup-row";
  return `
    <label class="${rowClass}" data-i="${i}">
      <input type="checkbox" class="cleanup-row-check" ${checked}>
      <div class="cleanup-row-body">
        <div class="cleanup-row-title">${escapeHtml(name)}</div>
        <div class="cleanup-row-meta">${branch} ${dirtyLabel}</div>
        <div class="cleanup-row-signals">${badges}</div>
      </div>
    </label>
  `;
}

function updateSubmitCount() {
  const checked = listEl.querySelectorAll(".cleanup-row-check:checked").length;
  submitBtn.textContent = `archive selected (${checked})`;
  submitBtn.disabled = checked === 0;
}

async function refresh() {
  listEl.innerHTML = `<div class="cleanup-loading">Walking worktrees…</div>`;
  try {
    const res = await fetch("/api/cleanup/candidates");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    candidates = data.candidates;
    if (candidates.length === 0) {
      listEl.innerHTML = `<div class="cleanup-empty">No cleanup candidates. 🎉</div>`;
    } else {
      listEl.innerHTML = candidates.map(renderRow).join("");
    }
    updateSubmitCount();
  } catch (e) {
    showError(`failed to load candidates: ${e.message}`);
    listEl.innerHTML = "";
  }
}

export async function openCleanupModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  deleteBranchesBox.checked = false;
  modal.classList.remove("hidden");
  document.body.classList.add("cleanup-modal-open");
  pushEscape(closeCleanupModal);
  await refresh();
}

export function closeCleanupModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("cleanup-modal-open");
  popEscape(closeCleanupModal);
}

async function handleSubmit() {
  clearError();
  const selected = [];
  const deleteBranches = deleteBranchesBox.checked;
  listEl.querySelectorAll(".cleanup-row").forEach((row) => {
    const check = row.querySelector(".cleanup-row-check");
    if (check && check.checked) {
      const i = parseInt(row.dataset.i, 10);
      const c = candidates[i];
      if (c) {
        selected.push({ pinned_dir: c.pinned_dir, delete_branch: deleteBranches });
      }
    }
  });
  if (selected.length === 0) return;
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/cleanup/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates: selected }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    const result = await res.json();
    if (result.failed && result.failed.length > 0) {
      // Some failed; show error, refresh to show what's left.
      showError(
        `Archived ${result.archived.length}, ${result.failed.length} failed: ` +
        result.failed.map((f) => `${f.pinned_dir.split("/").pop()}: ${f.error}`).join("; ")
      );
      await refresh();
      return;
    }
    closeCleanupModal();
  } catch (e) {
    showError(`request failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initCleanupModal() {
  const openBtn = document.getElementById("cleanup-btn");
  if (openBtn) openBtn.addEventListener("click", openCleanupModal);
  closeBtn.addEventListener("click", closeCleanupModal);
  cancelBtn.addEventListener("click", closeCleanupModal);
  submitBtn.addEventListener("click", handleSubmit);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeCleanupModal();
  });
  listEl.addEventListener("change", (e) => {
    if (e.target.classList.contains("cleanup-row-check")) {
      updateSubmitCount();
    }
  });
}
