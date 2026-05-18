// Review PR modal. Repo + PR number → POST /api/projects/pr-review.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

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

let cached = { repos: [] };
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderRepoOptions() {
  reposListEl.innerHTML = cached.repos
    .map((r) => `<option value="${escapeHtml(r)}">`)
    .join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/projects/discoverable");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    cached = await res.json();
    renderRepoOptions();
  } catch (e) {
    showError(`failed to load repos: ${e.message}`);
  }
}

export async function openReviewPRModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  modal.classList.remove("hidden");
  document.body.classList.add("review-pr-modal-open");
  pushEscape(closeReviewPRModal);
  repoInput.value = "";
  prInput.value = "";
  nameInput.value = "";
  await refresh();
  repoInput.focus();
}

export function closeReviewPRModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("review-pr-modal-open");
  popEscape(closeReviewPRModal);
}

async function handleSubmit(e) {
  e.preventDefault();
  clearError();
  const repo = repoInput.value.trim();
  const pr = parseInt(prInput.value, 10);
  const name = nameInput.value.trim();
  if (!repo) {
    showError("repo is required");
    return;
  }
  if (!pr || pr <= 0) {
    showError("PR number must be a positive integer");
    return;
  }
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/projects/pr-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, pr_number: pr, name: name || undefined }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    closeReviewPRModal();
  } catch (e) {
    showError(`request failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
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
