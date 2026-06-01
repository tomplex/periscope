// New-project modal. Open/close + populate repo/branch pickers from
// /api/projects/discoverable, submit to /api/projects, close on success.

import { escapeHtml } from './util.js';
import { createModalShell } from './modal-shell.js';
import { addWorktreeToRail } from './prefs.js';
import { state } from './state.js';

const modal = document.getElementById("new-project-modal");
const closeBtn = document.getElementById("new-project-modal-close");
const cancelBtn = document.getElementById("new-project-cancel");
const form = document.getElementById("new-project-form");
const repoInput = document.getElementById("new-project-repo");
const branchInput = document.getElementById("new-project-branch");
const nameInput = document.getElementById("new-project-name");
const reposListEl = document.getElementById("new-project-repos");
const branchesListEl = document.getElementById("new-project-branches");
const errorEl = document.getElementById("new-project-error");
const submitBtn = document.getElementById("new-project-submit");

const shell = createModalShell({ modal, bodyClass: "new-project-modal-open", errorEl });
export const closeNewProjectModal = shell.close;

// In-memory cache of the last /api/projects/discoverable response.
// Keyed lookups: when the user changes repo, we filter the branch
// datalist to that repo's branches.
let cached = { repos: [], branches_by_repo: {} };

function renderRepoOptions() {
  reposListEl.innerHTML = cached.repos
    .map((r) => `<option value="${escapeHtml(r)}">`)
    .join("");
}

function renderBranchOptions() {
  const repo = repoInput.value.trim();
  const branches = cached.branches_by_repo[repo] || [];
  branchesListEl.innerHTML = branches
    .map((b) => `<option value="${escapeHtml(b)}">`)
    .join("");
}

async function refresh() {
  const data = await shell.request("load repos", "/api/projects/discoverable");
  if (!data) return;
  cached = data;
  renderRepoOptions();
  renderBranchOptions();
}

export async function openNewProjectModal() {
  if (!shell.open()) return;
  repoInput.value = "";
  branchInput.value = "";
  nameInput.value = "";
  // Populate datalists.
  await refresh();
  // Focus the repo input so keyboard-only users can start typing.
  repoInput.focus();
}

async function handleSubmit(e) {
  e.preventDefault();
  shell.clearError();
  const repo = repoInput.value.trim();
  const branch = branchInput.value.trim();
  const name = nameInput.value.trim();
  if (!repo || !branch) {
    shell.showError("repo and branch are required");
    return;
  }
  submitBtn.disabled = true;
  const result = await shell.request("create project", "/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo, branch, name: name || undefined }),
  });
  submitBtn.disabled = false;
  if (!result) return;
  if (result.warning) {
    // Non-fatal — still close, but log so the dev console shows it.
    console.warn("new-project warning:", result.warning);
  }
  closeNewProjectModal();
  // Rail auto-add: deferred until the next /api/state poll reflects the new
  // session's panes. grid.js polls every 3s; wait ~3.5s.
  if (result.tmux_session) {
    setTimeout(async () => {
      const sessionName = result.tmux_session;
      const wins = (state.lastWindows || []).filter(w => w.session === sessionName);
      if (wins.length === 0) return;  // race; user can + open later
      await addWorktreeToRail({
        repoKey: wins[0].repo_key || result.repo,
        worktreeKey: sessionName,
        paneIds: wins.map(w => w.pid),
        hasReview: true,
      });
    }, 3500);
  }
}

export function initNewProjectModal() {
  const openBtn = document.getElementById("new-project-btn");
  if (openBtn) openBtn.addEventListener("click", openNewProjectModal);
  closeBtn.addEventListener("click", closeNewProjectModal);
  cancelBtn.addEventListener("click", closeNewProjectModal);
  modal.addEventListener("click", (e) => {
    // Click on the overlay (not the card) closes.
    if (e.target === modal) closeNewProjectModal();
  });
  repoInput.addEventListener("change", renderBranchOptions);
  repoInput.addEventListener("input", renderBranchOptions);
  form.addEventListener("submit", handleSubmit);
}
