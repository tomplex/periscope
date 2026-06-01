// Review PR modal. Repo + PR number → POST /api/projects/pr-review.

import { escapeHtml } from './util.js';
import { createModalShell } from './modal-shell.js';
import { addWorktreeToRail } from './prefs.js';
import { state } from './state.js';

const modal = document.getElementById("review-pr-modal");
const closeBtn = document.getElementById("review-pr-modal-close");
const cancelBtn = document.getElementById("review-pr-cancel");
const form = document.getElementById("review-pr-form");
const repoInput = document.getElementById("review-pr-repo");
const prInput = document.getElementById("review-pr-number");
const nameInput = document.getElementById("review-pr-name");
const reposListEl = document.getElementById("review-pr-repos");
const errorEl = document.getElementById("review-pr-error");
const submitBtn = document.getElementById("review-pr-submit");

const shell = createModalShell({ modal, bodyClass: "review-pr-modal-open", errorEl });
export const closeReviewPRModal = shell.close;

let cached = { repos: [] };

function renderRepoOptions() {
  reposListEl.innerHTML = cached.repos
    .map((r) => `<option value="${escapeHtml(r)}">`)
    .join("");
}

async function refresh() {
  const data = await shell.request("load repos", "/api/projects/discoverable");
  if (!data) return;
  cached = data;
  renderRepoOptions();
}

export async function openReviewPRModal() {
  if (!shell.open()) return;
  repoInput.value = "";
  prInput.value = "";
  nameInput.value = "";
  await refresh();
  repoInput.focus();
}

async function handleSubmit(e) {
  e.preventDefault();
  shell.clearError();
  const repo = repoInput.value.trim();
  const pr = parseInt(prInput.value, 10);
  const name = nameInput.value.trim();
  if (!repo) {
    shell.showError("repo is required");
    return;
  }
  if (!pr || pr <= 0) {
    shell.showError("PR number must be a positive integer");
    return;
  }
  submitBtn.disabled = true;
  const result = await shell.request("start PR review", "/api/projects/pr-review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo, pr_number: pr, name: name || undefined }),
  });
  submitBtn.disabled = false;
  if (!result) return;
  closeReviewPRModal();
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

export function initReviewPRModal() {
  const openBtn = document.getElementById("review-pr-btn");
  if (openBtn) openBtn.addEventListener("click", openReviewPRModal);
  closeBtn.addEventListener("click", closeReviewPRModal);
  cancelBtn.addEventListener("click", closeReviewPRModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeReviewPRModal();
  });
  form.addEventListener("submit", handleSubmit);
}
