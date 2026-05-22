// Cleanup modal. Loads candidates from /api/cleanup/candidates, renders
// a checklist with signal badges, submits selected to /api/cleanup/archive.

import { escapeHtml } from './util.js';
import { createModalShell } from './modal-shell.js';

const modal = document.getElementById("cleanup-modal");
const closeBtn = document.getElementById("cleanup-modal-close");
const cancelBtn = document.getElementById("cleanup-cancel");
const submitBtn = document.getElementById("cleanup-submit");
const listEl = document.getElementById("cleanup-modal-list");
const errorEl = document.getElementById("cleanup-modal-error");
const deleteBranchesBox = document.getElementById("cleanup-delete-branches");

const shell = createModalShell({ modal, bodyClass: "cleanup-modal-open", errorEl });
export const closeCleanupModal = shell.close;

let candidates = [];

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
  const data = await shell.request("load candidates", "/api/cleanup/candidates");
  if (!data) {
    listEl.innerHTML = "";
    return;
  }
  candidates = data.candidates;
  if (candidates.length === 0) {
    listEl.innerHTML = `<div class="cleanup-empty">No cleanup candidates. 🎉</div>`;
  } else {
    listEl.innerHTML = candidates.map(renderRow).join("");
  }
  updateSubmitCount();
}

export async function openCleanupModal() {
  if (!shell.open()) return;
  deleteBranchesBox.checked = false;
  await refresh();
}

async function handleSubmit() {
  shell.clearError();
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
  const result = await shell.request("archive", "/api/cleanup/archive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidates: selected }),
  });
  submitBtn.disabled = false;
  if (!result) return;
  if (result.failed && result.failed.length > 0) {
    // Some failed; show error, refresh to show what's left.
    shell.showError(
      `Archived ${result.archived.length}, ${result.failed.length} failed: ` +
      result.failed.map((f) => `${f.pinned_dir.split("/").pop()}: ${f.error}`).join("; ")
    );
    await refresh();
    return;
  }
  closeCleanupModal();
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
