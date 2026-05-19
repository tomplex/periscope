// Settings modal: GET /api/settings on open, PATCH /api/settings on save.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("settings-modal");
const closeBtn = document.getElementById("settings-modal-close");
const cancelBtn = document.getElementById("settings-cancel");
const form = document.getElementById("settings-form");
const idleInput = document.getElementById("settings-cleanup-idle-days");
const defaultSelect = document.getElementById("settings-worktree-default");
const overridesListEl = document.getElementById("settings-overrides-list");
const errorEl = document.getElementById("settings-modal-error");
const submitBtn = document.getElementById("settings-submit");

let currentSettings = {};
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderOverrides(overrides) {
  const rows = Object.entries(overrides || {});
  if (rows.length === 0) {
    overridesListEl.innerHTML = `<div class="settings-overrides-empty">(none yet)</div>`;
    return;
  }
  overridesListEl.innerHTML = rows
    .map(([repo, layout]) => `
      <div class="settings-overrides-row" data-repo="${escapeHtml(repo)}">
        <span class="settings-overrides-repo">${escapeHtml(repo)}</span>
        <select class="settings-overrides-layout">
          <option value="sibling"${layout === "sibling" ? " selected" : ""}>sibling</option>
          <option value="inline"${layout === "inline" ? " selected" : ""}>inline</option>
        </select>
        <button type="button" class="settings-overrides-remove" title="remove override">×</button>
      </div>
    `)
    .join("");
}

async function refresh() {
  clearError();
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    currentSettings = body.settings || {};
    idleInput.value = currentSettings.cleanup_idle_days ?? 14;
    defaultSelect.value = currentSettings.worktree_layout_default ?? "sibling";
    renderOverrides(currentSettings.worktree_layout_overrides || {});
  } catch (e) {
    showError(`failed to load settings: ${e.message}`);
  }
}

export async function openSettingsModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  modal.classList.remove("hidden");
  document.body.classList.add("settings-modal-open");
  pushEscape(closeSettingsModal);
  await refresh();
}

export function closeSettingsModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("settings-modal-open");
  popEscape(closeSettingsModal);
}

function collectOverrides() {
  const out = {};
  overridesListEl.querySelectorAll(".settings-overrides-row").forEach((row) => {
    const repo = row.dataset.repo;
    const layout = row.querySelector(".settings-overrides-layout").value;
    if (repo && layout) out[repo] = layout;
  });
  return out;
}

async function handleSubmit(e) {
  e.preventDefault();
  clearError();
  const idle = parseInt(idleInput.value, 10);
  if (!idle || idle < 1) {
    showError("cleanup idle days must be a positive integer");
    return;
  }
  const patch = {
    cleanup_idle_days: idle,
    worktree_layout_default: defaultSelect.value,
    worktree_layout_overrides: collectOverrides(),
  };
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    closeSettingsModal();
  } catch (e) {
    showError(`save failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initSettingsModal() {
  const openBtn = document.getElementById("settings-btn");
  if (openBtn) openBtn.addEventListener("click", openSettingsModal);
  closeBtn.addEventListener("click", closeSettingsModal);
  cancelBtn.addEventListener("click", closeSettingsModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeSettingsModal();
  });
  overridesListEl.addEventListener("click", (e) => {
    const remove = e.target.closest(".settings-overrides-remove");
    if (remove) {
      const row = remove.closest(".settings-overrides-row");
      if (row) row.remove();
    }
  });
  form.addEventListener("submit", handleSubmit);
}
