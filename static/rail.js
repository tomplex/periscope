// Left rail for split view: derives the Repo → Worktree → Pane-children
// tree from prefs (curated membership/order) joined with /api/state
// (live status). Renders into #rail.
//
// rail.js only renders. Interactions (collapse, drag, select) are wired
// in later tasks but live in this file.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';

const railEl = () => document.getElementById("rail");

// Severity ranking for status rollup: higher index = higher priority.
const SEVERITY = ["shell", "idle", "done", "working", "needs-input"];

function maxSeverity(states) {
  let best = -1;
  for (const s of states) {
    const i = SEVERITY.indexOf(s);
    if (i > best) best = i;
  }
  return best >= 0 ? SEVERITY[best] : "shell";
}

// Build a quick { worktreeKey: [windowObj, ...] } map from /api/state.
// state.lastWindows is the most recent /api/state windows array,
// written to by grid.js's poll() at `state.lastWindows = data.windows`.
function indexWindowsByWorktree(windows) {
  const out = {};
  for (const w of (windows || [])) {
    const key = w.session;  // worktree_key = session name
    (out[key] = out[key] || []).push(w);
  }
  return out;
}

// Look up the human-readable repo label for a repo_key. We pull it off
// any window whose `repo_key` matches; falls back to basename of the
// repo_key path.
function repoLabelFor(repoKey, windows) {
  for (const w of (windows || [])) {
    if (w.repo_key === repoKey && w.repo_label) return w.repo_label;
  }
  const parts = String(repoKey || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || repoKey;
}

function statusDotClass(s) {
  if (s === "needs-input") return "dot dot-alert dot-pulse";
  if (s === "working") return "dot dot-green";
  if (s === "done") return "dot dot-blue";
  if (s === "idle") return "dot dot-grey";
  return "dot dot-none";
}

function paneRow(w, selectedKey) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey ? " selected" : "";
  return `
    <div class="rail-row child-row${sel}" data-row="pane" data-pid="${escapeHtml(w.pid)}" data-key="${escapeHtml(k)}">
      <span class="rail-conn">├</span>
      <span class="rail-icon icon-pane">✦</span>
      <span class="rail-label">${escapeHtml(w.name || "claude")}</span>
      <span class="${statusDotClass(w.state)}"></span>
    </div>`;
}

function reviewRow(worktreeKey, lgtmLive, selectedKey) {
  const k = `review:${worktreeKey}`;
  const sel = k === selectedKey ? " selected" : "";
  const empty = lgtmLive ? "" : " review-empty";
  return `
    <div class="rail-row child-row${sel}${empty}" data-row="review" data-worktree="${escapeHtml(worktreeKey)}" data-key="${escapeHtml(k)}">
      <span class="rail-conn">├</span>
      <span class="rail-icon icon-review">👁</span>
      <span class="rail-label">review${lgtmLive ? "" : " <em>start →</em>"}</span>
    </div>`;
}

function worktreeRow(worktreeKey, children, collapsed, rolledUp, label) {
  const chev = collapsed ? "▸" : "▾";
  const childCountChip = collapsed && children.length > 0
    ? `<span class="rail-count">${children.length}</span>`
    : "";
  const body = collapsed ? "" : children.join("");
  return `
    <div class="rail-row wt-row" data-row="worktree" data-key="${escapeHtml(`wt:${worktreeKey}`)}">
      <span class="rail-chev">${chev}</span>
      <span class="rail-icon icon-worktree">⎇</span>
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      ${childCountChip}
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

function repoRow(repoKey, label, worktreeBlocks, collapsed, rolledUp) {
  const chev = collapsed ? "▸" : "▾";
  const body = collapsed ? "" : worktreeBlocks.join("");
  return `
    <div class="rail-row repo-row" data-row="repo" data-key="${escapeHtml(`repo:${repoKey}`)}">
      <span class="rail-chev">${chev}</span>
      <span class="rail-icon icon-repo">📚</span>
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

export function renderRail() {
  const el = railEl();
  if (!el) return;
  attachRailListeners();

  const repoOrder = prefs.getRepoOrder();
  const worktreesByRepo = prefs.getWorktreesByRepo();
  const panesByWorktree = prefs.getPanesByWorktree();
  const collapsed = prefs.getRailCollapsed();
  const selectedKey = (() => {
    const sel = prefs.getLastSelected();
    if (!sel) return null;
    if (sel.kind === "pane") return `pane:${sel.pid}`;
    if (sel.kind === "review") return `review:${sel.worktree}`;
    return null;
  })();
  const windows = state.lastWindows;
  const byWorktree = indexWindowsByWorktree(windows);

  if (repoOrder.length === 0) {
    el.innerHTML = `
      <div class="rail-head">
        <span>Projects</span>
        <button class="rail-add" id="rail-add">+</button>
      </div>
      <div class="rail-empty">
        Empty. Use <code>+ project</code>, <code>review PR</code>, or <code>+ open</code> to add a worktree.
      </div>`;
    return;
  }

  const blocks = repoOrder.map(repoKey => {
    const repoLabel = repoLabelFor(repoKey, windows);
    const worktrees = worktreesByRepo[repoKey] || [];
    const wtCollapsed = collapsed[`repo:${repoKey}`] === true;
    const wtBlocks = worktrees.map(wtKey => {
      const childOrder = panesByWorktree[wtKey] || [];
      const wtWindows = byWorktree[wtKey] || [];
      // Resolve the live window for each pane child.
      const windowsByPid = Object.fromEntries(wtWindows.map(w => [w.pid, w]));
      const childMarkup = [];
      const childStates = [];
      for (const child of childOrder) {
        if (child === "review") {
          const lgtmLive = wtWindows.some(w => w.lgtm && w.lgtm.slug);
          childMarkup.push(reviewRow(wtKey, lgtmLive, selectedKey));
          // Review row doesn't roll up into the worktree dot.
        } else {
          const w = windowsByPid[child];
          if (!w) continue;  // pane gone; skip silently (Phase 11 prunes)
          childMarkup.push(paneRow(w, selectedKey));
          childStates.push(w.state || "shell");
        }
      }
      const wtIsCollapsed = collapsed[`wt:${wtKey}`] === true;
      const rolledUp = maxSeverity(childStates);
      // Label: branch from any window in this worktree.
      const label = (wtWindows[0]?.branch) || wtKey;
      return worktreeRow(wtKey, childMarkup, wtIsCollapsed, rolledUp, label);
    });
    // Repo rollup = max across worktree rollups.
    const allChildStates = (worktrees.flatMap(wt => (byWorktree[wt] || []).map(w => w.state || "shell")));
    const repoRolledUp = maxSeverity(allChildStates);
    return repoRow(repoKey, repoLabel, wtBlocks, wtCollapsed, repoRolledUp);
  });

  el.innerHTML = `
    <div class="rail-head">
      <span>Projects</span>
      <button class="rail-add" id="rail-add">+</button>
    </div>
    ${blocks.join("")}
  `;
}

// One click delegate on the rail container — re-attaches on each render
// is unnecessary because the listener lives on the static #rail element.
// The flag is module-scoped (not stored on the DOM node) so it follows
// the JS lifetime, not the DOM.
let listenersAttached = false;
function attachRailListeners() {
  if (listenersAttached) return;
  const el = railEl();
  if (!el) return;
  listenersAttached = true;

  el.addEventListener("click", async (e) => {
    // + open button (delegated; lives in rail-head)
    if (e.target.id === "rail-add") {
      const { openPicker } = await import('./open-picker-modal.js');
      openPicker();
      return;
    }

    const row = e.target.closest(".rail-row");
    if (!row) return;
    const kind = row.dataset.row;
    if (kind === "repo" || kind === "worktree") {
      // Toggle collapse — persisted.
      const key = row.dataset.key;
      const current = prefs.getRailCollapsed()[key] === true;
      await prefs.setRailCollapsedKey(key, !current);
      renderRail();
      return;
    }
    if (kind === "pane") {
      const pid = row.dataset.pid;
      await prefs.setLastSelected({ kind: "pane", pid });
      state.railSelected = `pane:${pid}`;
      const { selectPane } = await import('./detail.js');
      selectPane(pid);
      renderRail();
      return;
    }
    if (kind === "review") {
      const worktree = row.dataset.worktree;
      await prefs.setLastSelected({ kind: "review", worktree });
      state.railSelected = `review:${worktree}`;
      const { selectReview } = await import('./detail.js');
      selectReview(worktree);
      renderRail();
      return;
    }
  });
}
