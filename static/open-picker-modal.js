// + open picker for split-view rail. Lists tmux sessions whose worktree
// isn't already in the rail, grouped by repo. Multi-select; submit calls
// prefs.addWorktreeToRail() once per selected session.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';

const $ = (id) => document.getElementById(id);

let selected = new Set();

export function openPicker() {
  selected.clear();
  $("open-picker-modal").classList.remove("hidden");
  renderList();
}

function closePicker() {
  $("open-picker-modal").classList.add("hidden");
}

function renderList() {
  const railed = new Set(
    Object.values(prefs.getWorktreesByRepo()).flat()
  );
  const grouped = {};  // repo_label → [{session, branch, repo_key}, ...]
  for (const w of (state.lastWindows || [])) {
    if (railed.has(w.session)) continue;
    if (!w.repo_key) continue;  // skip non-git sessions for v1
    const k = w.repo_label || w.repo_key;
    (grouped[k] = grouped[k] || []).push({
      session: w.session,
      branch: w.branch,
      repo_key: w.repo_key,
      pid: w.pid,
      has_review: true,  // worktree-backed → review row
    });
  }

  if (Object.keys(grouped).length === 0) {
    $("open-picker-list").innerHTML = `<div class="open-picker-empty">No sessions available to add — every git session is already railed.</div>`;
    updateSubmitButton();
    return;
  }

  $("open-picker-list").innerHTML = Object.entries(grouped).map(([label, sessions]) => `
    <div class="open-picker-repo">
      <div class="open-picker-repo-head">${escapeHtml(label)}</div>
      ${sessions.map(s => `
        <label class="open-picker-row">
          <input type="checkbox" data-session="${escapeHtml(s.session)}" data-repo-key="${escapeHtml(s.repo_key)}" data-pid="${escapeHtml(s.pid)}">
          <span>${escapeHtml(s.session)}</span>
          <span class="open-picker-branch">${escapeHtml(s.branch || "")}</span>
        </label>`).join("")}
    </div>`).join("");

  $("open-picker-list").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", () => {
      const key = cb.dataset.session;
      if (cb.checked) selected.add(key); else selected.delete(key);
      updateSubmitButton();
    });
  });
  updateSubmitButton();
}

function updateSubmitButton() {
  const btn = $("open-picker-submit");
  btn.textContent = `add (${selected.size})`;
  btn.disabled = selected.size === 0;
}

async function submit() {
  // Re-derive worktree info from the checked rows.
  const checks = Array.from($("open-picker-list").querySelectorAll("input[type=checkbox]:checked"));
  for (const cb of checks) {
    const session = cb.dataset.session;
    const repoKey = cb.dataset.repoKey;
    // Collect ALL panes for this session, not just the pid stored in the checkbox.
    const sessionPanes = (state.lastWindows || [])
      .filter(w => w.session === session)
      .map(w => w.pid);
    await prefs.addWorktreeToRail({
      repoKey,
      worktreeKey: session,
      paneIds: sessionPanes,
      hasReview: true,
    });
  }
  closePicker();
  // Trigger immediate render.
  const { renderRail } = await import('./rail.js');
  renderRail();
}

export function initOpenPicker() {
  $("open-picker-close").addEventListener("click", closePicker);
  $("open-picker-cancel").addEventListener("click", closePicker);
  $("open-picker-submit").addEventListener("click", submit);
}
