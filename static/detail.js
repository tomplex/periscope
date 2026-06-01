// Right-pane (#detail) rendering for split view. Four states:
//
//   - "pane"            — terminal + side metadata
//   - "review-live"     — LGTM iframe
//   - "review-empty"    — start CTA
//   - "empty"           — nothing selected
//
// detail.js owns the mount/unmount lifecycle of the xterm + iframe.
// Callers come from rail.js click handlers; the public API is
// selectPane / selectReview / showEmpty / refreshDetail / detailTeardown.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, apiCall, rewriteLgtmHost, prUrl } from './util.js';
import { mountTerminal, unmountTerminal } from './terminal-mount.js';
import { handleModalImagePaste } from './modal.js';

let currentMount = null;        // "pane" | "review" | "empty" | null
let currentMountKey = null;     // pid (when "pane") or worktreeKey (when "review")

function $(id) { return document.getElementById(id); }

function show(id) {
  for (const which of ["detail-empty", "detail-pane", "detail-review", "detail-review-start"]) {
    $(which).classList.toggle("hidden", which !== id);
  }
}

function lookupWindow(pid) {
  return (state.lastWindows || []).find(w => w.pid === pid) || null;
}

function lgtmSessionForWorktree(worktreeKey) {
  const w = (state.lastWindows || []).find(w => w.session === worktreeKey);
  return w?.lgtm?.slug ? w.lgtm : null;
}

function paneHeader(w) {
  const parts = [
    `<span><b>${escapeHtml(w.session || "")}</b></span>`,
  ];
  if (w.branch) {
    parts.push(`<span class="hsep">·</span><span>${escapeHtml(w.branch)}</span>`);
  }
  if (w.pr) {
    const href = prUrl(w.repo_slug, w.pr);
    const ciCls = w.ci === "✓" ? "ci-ok" : w.ci === "✗" ? "ci-bad" : w.ci === "⟳" ? "ci-running" : "";
    const ciSpan = w.ci ? ` <span class="header-ci ${ciCls}">${escapeHtml(w.ci)}</span>` : "";
    const inner = `#${escapeHtml(String(w.pr))}${ciSpan}`;
    const prLink = href
      ? `<a class="header-pr" href="${href}" target="_blank" rel="noopener">${inner}</a>`
      : `<span class="header-pr">${inner}</span>`;
    parts.push(`<span class="hsep">·</span>${prLink}`);
  }
  if (w.linked_linear) {
    const lid = escapeHtml(w.linked_linear);
    const ltitle = w.linked_linear_title ? `: ${escapeHtml(w.linked_linear_title)}` : "";
    const lstatus = w.linked_linear_status ? ` [${escapeHtml(w.linked_linear_status)}]` : "";
    parts.push(
      `<span class="hsep">·</span><a class="header-linear" href="https://linear.app/issue/${lid}" target="_blank" rel="noopener" title="Linear ${lid}${ltitle}${lstatus}">${lid}</a>`
    );
  }
  if (w.git && w.git !== "clean") {
    parts.push(`<span class="hsep">·</span><span class="header-git">${escapeHtml(w.git)}</span>`);
  }
  if (w.is_claude && w.model) {
    parts.push(`<span class="hsep">·</span><span>${escapeHtml(w.model.replace(/\s*\(.*\)/, ""))}</span>`);
  }
  if (w.is_claude && w.context_pct != null) {
    parts.push(`<span class="hsep">·</span><span>${w.context_pct}%</span>`);
  }
  if (w.api_error) {
    parts.push(`<span class="hsep">·</span><span class="header-api-error" title="last tool result was an API error">⚠ API error</span>`);
  }
  return parts.join("");
}

export function selectPane(pid) {
  const w = lookupWindow(pid);
  if (!w) {
    showEmpty();
    return;
  }
  // Same-pane no-op: header + side panel still refresh from latest /api/state,
  // but xterm doesn't re-mount (avoids WS churn on every poll's re-render).
  const sameMount = currentMount === "pane" && currentMountKey === pid;
  show("detail-pane");
  $("detail-pane-header").innerHTML = paneHeader(w);
  $("detail-side").innerHTML = renderSidePanel(w);
  // state.activeTarget drives the shared paste handler (and any future
  // active-pane-keyed actions). Set it on every selectPane call so it
  // tracks the current selection even when sameMount short-circuits.
  state.activeTarget = w.target;
  if (!sameMount) {
    mountTerminal(
      $("detail-xterm"),
      w.target,
      {
        onPaste: handleModalImagePaste,
        // onMdLink: future work — split view doesn't yet wire LGTM doc adds
      }
    );
    currentMount = "pane";
    currentMountKey = pid;
  }
}

function renderSidePanel(w) {
  const sections = [];
  if (w.pending_input) {
    sections.push(`<div class="side-section"><div class="side-label">Pending input</div><div class="side-pending"><span class="side-prompt">›</span>${escapeHtml(w.pending_input)}</div></div>`);
  }
  if (w.recap) {
    sections.push(`<div class="side-section"><div class="side-label">Recap</div><div>${escapeHtml(w.recap)}</div></div>`);
  }
  if (w.last_line) {
    sections.push(`<div class="side-section"><div class="side-label">Last line</div><div class="side-mono">${escapeHtml(w.last_line)}</div></div>`);
  }
  return sections.join("");
}

export function selectReview(worktreeKey) {
  const session = lgtmSessionForWorktree(worktreeKey);
  if (!session) {
    // No LGTM session — show start CTA.
    show("detail-review-start");
    $("detail-review-start").innerHTML = `
      <div class="review-start-card">
        <div class="review-start-title">No LGTM session for this worktree</div>
        <button class="review-start-btn" data-worktree="${escapeHtml(worktreeKey)}">Start review →</button>
      </div>`;
    $("detail-review-start").querySelector("button").addEventListener("click", async (e) => {
      const wt = e.currentTarget.dataset.worktree;
      const w = (state.lastWindows || []).find(x => x.session === wt);
      if (!w) return;
      // POST /api/lgtm/start with the worktree cwd.
      await apiCall("start review", "/api/lgtm/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd: w.cwd }),
      });
      // After start, switch to the iframe. Wait for the next /api/state
      // poll so state.lastWindows[w].lgtm is populated.
      setTimeout(() => selectReview(worktreeKey), 1500);
    });
    currentMount = "review";
    currentMountKey = worktreeKey;
    return;
  }
  // Same-review no-op: don't reassign iframe src (causes reload on every poll).
  const sameMount = currentMount === "review" && currentMountKey === worktreeKey;
  show("detail-review");
  $("detail-review-header").innerHTML = `<span><b>review</b></span><span class="hsep">·</span><span>${escapeHtml(worktreeKey)}</span>`;
  if (!sameMount) {
    // session.url is what cached_lgtm_state surfaces — same field modal.js
    // reads. Rewrite its host to match the parent page (Tauri compat).
    $("detail-review-iframe").src = rewriteLgtmHost(session.url);
    // Tear down xterm if it was mounted.
    if (currentMount === "pane") unmountTerminal();
    state.activeTarget = null;  // review mode owns the iframe, no pane target
    currentMount = "review";
    currentMountKey = worktreeKey;
  }
}

export function showEmpty() {
  show("detail-empty");
  $("detail-empty").innerHTML = `
    <div class="detail-empty-card">
      <p>Select a tab on the left, or <button id="detail-empty-add">+ open</button> to add one.</p>
    </div>`;
  if (currentMount === "pane") unmountTerminal();
  state.activeTarget = null;
  currentMount = "empty";
  currentMountKey = null;
}

// Called by applyView() when leaving split view. Tears down xterm to
// stop polling the WebSocket while user is in another view.
export function detailTeardown() {
  if (currentMount === "pane") unmountTerminal();
  state.activeTarget = null;
  currentMount = null;
  currentMountKey = null;
}

// Called on view switch into split. Restores last selection.
export function refreshDetail() {
  const sel = prefs.getLastSelected();
  if (!sel) {
    showEmpty();
    return;
  }
  if (sel.kind === "pane") selectPane(sel.pid);
  else if (sel.kind === "review") selectReview(sel.worktree);
  else showEmpty();
}
