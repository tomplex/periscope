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
import { escapeHtml, apiCall, rewriteLgtmHost } from './util.js';
import { mountTerminal, unmountTerminal } from './terminal-mount.js';

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
  const ctx = (w.is_claude && w.context_pct != null)
    ? `${escapeHtml((w.model || "").replace(/\s*\(.*\)/, ""))} · ${w.context_pct}%`
    : "";
  return `
    <span><b>${escapeHtml(w.session || "")}</b></span>
    <span class="hsep">·</span>
    <span>${escapeHtml(w.branch || "")}</span>
    ${w.pr ? `<span class="hsep">·</span><span>#${escapeHtml(String(w.pr))} ${w.ci || ""}</span>` : ""}
    ${ctx ? `<span class="hsep">·</span><span>${ctx}</span>` : ""}
  `;
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
  if (!sameMount) {
    mountTerminal(
      $("detail-xterm"),
      w.target,
      { onPaste: null }  // image paste lives in modal; split view ships without it (future work)
    );
    currentMount = "pane";
    currentMountKey = pid;
  }
}

function renderSidePanel(w) {
  const recap = w.recap ? `<div class="side-section"><div class="side-label">Recap</div><div>${escapeHtml(w.recap)}</div></div>` : "";
  const last = w.last_line ? `<div class="side-section"><div class="side-label">Last line</div><div class="side-mono">${escapeHtml(w.last_line)}</div></div>` : "";
  return recap + last;
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
  currentMount = "empty";
  currentMountKey = null;
}

// Called by applyView() when leaving split view. Tears down xterm to
// stop polling the WebSocket while user is in another view.
export function detailTeardown() {
  if (currentMount === "pane") unmountTerminal();
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
