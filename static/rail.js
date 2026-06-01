// Left rail for split view: derives the Repo → Worktree → Pane-children
// tree from prefs (curated membership/order) joined with /api/state
// (live status). Renders into #rail.
//
// rail.js only renders. Interactions (collapse, drag, select) are wired
// in later tasks but live in this file.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';
import { passesFilter } from './grid.js';

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

// Merge live /api/state windows with persisted ordering prefs to produce
// the actual rail tree. Live windows ARE the membership; prefs are
// ordering hints — entries in pref order come first (in their pref
// position), then new live entries append. Pref entries for repos /
// worktrees / panes that are no longer live are silently dropped from
// the rendered output (the pref itself is cleaned up by
// pruneDanglingEntries elsewhere; here we just don't render them).
// Synthetic repo key for non-worktree-backed sessions (bare shells,
// non-git cwds). Renders as a top-level "Other" group at the bottom of
// the rail with sessions as direct children (no review row, no branch).
export const OTHER_REPO_KEY = "__other__";

function mergeLiveAndPrefs(windows, prefRepoOrder, prefWtByRepo, prefPanesByWt) {
  const liveByRepo = {};       // repo_key → ordered list of session names (first-seen wins)
  const livePanesByWt = {};    // session name → ordered list of pane pids (first-seen)
  for (const w of (windows || [])) {
    const r = w.repo_key || OTHER_REPO_KEY;
    const s = w.session;
    if (!liveByRepo[r]) liveByRepo[r] = [];
    if (!liveByRepo[r].includes(s)) liveByRepo[r].push(s);
    if (!livePanesByWt[s]) livePanesByWt[s] = [];
    if (!livePanesByWt[s].includes(w.pid)) livePanesByWt[s].push(w.pid);
  }

  // Repo order: prefs first (filtered to live), then live-new appended.
  // "Other" always lands at the bottom regardless of pref order — it's a
  // catch-all bucket, not a peer to real repos.
  const liveRepoSet = new Set(Object.keys(liveByRepo));
  const realRepos = [...prefRepoOrder.filter(r => liveRepoSet.has(r) && r !== OTHER_REPO_KEY),
                     ...Object.keys(liveByRepo).filter(r => !prefRepoOrder.includes(r) && r !== OTHER_REPO_KEY)];
  const repoOrder = liveRepoSet.has(OTHER_REPO_KEY)
    ? [...realRepos, OTHER_REPO_KEY]
    : realRepos;

  // Worktree order per repo: same logic.
  const worktreesByRepo = {};
  for (const r of repoOrder) {
    const live = liveByRepo[r] || [];
    const liveSet = new Set(live);
    const pref = (prefWtByRepo[r] || []).filter(w => liveSet.has(w));
    const prefSet = new Set(pref);
    worktreesByRepo[r] = [...pref, ...live.filter(w => !prefSet.has(w))];
  }

  // Pane-children order per worktree: prefs first (filtered), then new
  // live pids. The "review" sentinel is auto-added for git-backed worktrees
  // only — non-worktree sessions under "Other" have no review row.
  const panesByWorktree = {};
  for (const r of repoOrder) {
    const isOther = r === OTHER_REPO_KEY;
    for (const w of worktreesByRepo[r]) {
      const live = livePanesByWt[w] || [];
      const liveSet = new Set(live);
      const pref = prefPanesByWt[w] || [];
      const prefKept = pref.filter(c => (c === "review" && !isOther) || liveSet.has(c));
      const prefSet = new Set(prefKept);
      const merged = [...prefKept, ...live.filter(p => !prefSet.has(p))];
      if (!isOther && !merged.includes("review")) merged.push("review");
      panesByWorktree[w] = merged;
    }
  }

  return { repoOrder, worktreesByRepo, panesByWorktree };
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

// Two-level filter: a row is shown (full opacity) if it matches the
// filter, OR if any of its descendants does. Non-matching rows that
// have no matching descendants are grayed in place.
//
// Reuses grid.js's passesFilter — single source of truth for what each
// filter value means. Don't reinvent the per-pane rule here; the rail's
// novelty is only the parent-rollup wrapping (worktree/repo).

function paneMatchesFilter(w) {
  return passesFilter(w);
}

function worktreeMatchesFilter(worktreeKey, byWorktree) {
  const windows = byWorktree[worktreeKey] || [];
  return windows.some(w => passesFilter(w));
}

function repoMatchesFilter(repoKey, byWorktree, worktreesByRepo) {
  const wts = worktreesByRepo[repoKey] || [];
  return wts.some(wt => worktreeMatchesFilter(wt, byWorktree));
}

// Look up the human-readable repo label for a repo_key. We pull it off
// any window whose `repo_key` matches; falls back to basename of the
// repo_key path. The synthetic OTHER_REPO_KEY renders as "Other".
function repoLabelFor(repoKey, windows) {
  if (repoKey === OTHER_REPO_KEY) return "Other";
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
  const dim = paneMatchesFilter(w) ? "" : " rail-dim";
  // ✻ for Claude panes (Claude's splash/thinking glyph), $ for everything else.
  const icon = w.is_claude
    ? `<span class="rail-icon icon-claude">✻</span>`
    : `<span class="rail-icon icon-shell">$</span>`;
  return `
    <div class="rail-row child-row${sel}${dim}" data-row="pane" data-pid="${escapeHtml(w.pid)}" data-key="${escapeHtml(k)}" draggable="true">
      ${icon}
      <span class="rail-label">${escapeHtml(w.name || (w.is_claude ? "claude" : "shell"))}</span>
      <span class="${statusDotClass(w.state)}"></span>
    </div>`;
}

function reviewRow(worktreeKey, lgtmLive, selectedKey) {
  const k = `review:${worktreeKey}`;
  const sel = k === selectedKey ? " selected" : "";
  const empty = lgtmLive ? "" : " review-empty";
  // ◉ for live LGTM sessions, ○ for "start →" CTAs — monochrome circle pair
  // reads cleaner than the colored eye emoji and matches periscope's glyph style.
  const icon = lgtmLive
    ? `<span class="rail-icon icon-review">◉</span>`
    : `<span class="rail-icon icon-review-empty">○</span>`;
  return `
    <div class="rail-row child-row${sel}${empty}" data-row="review" data-worktree="${escapeHtml(worktreeKey)}" data-key="${escapeHtml(k)}" draggable="true">
      ${icon}
      <span class="rail-label">review${lgtmLive ? "" : " <em>start →</em>"}</span>
    </div>`;
}

function newTabRow(worktreeKey) {
  return `
    <div class="rail-row child-row newtab-row last-in-worktree" data-row="newtab" data-worktree="${escapeHtml(worktreeKey)}">
      <span class="rail-icon">+</span>
      <span class="rail-label">New tab</span>
    </div>`;
}

function worktreeRow(worktreeKey, children, collapsed, rolledUp, label, byWorktree, repoKey) {
  const chev = collapsed ? "▸" : "▾";
  const childCountChip = collapsed && children.length > 0
    ? `<span class="rail-count">${children.length}</span>`
    : "";
  const body = collapsed ? "" : children.join("");
  const dim = worktreeMatchesFilter(worktreeKey, byWorktree) ? "" : " rail-dim";
  // Non-worktree sessions (under "Other") aren't branches — show ▸ instead of ⎇.
  const isOther = repoKey === OTHER_REPO_KEY;
  const icon = isOther
    ? `<span class="rail-icon icon-shell">›</span>`
    : `<span class="rail-icon icon-worktree">⎇</span>`;
  return `
    <div class="rail-row wt-row${dim}" data-row="worktree" data-key="${escapeHtml(`wt:${worktreeKey}`)}" draggable="true">
      <span class="rail-chev">${chev}</span>
      ${icon}
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      ${childCountChip}
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

function repoRow(repoKey, label, worktreeBlocks, collapsed, rolledUp, byWorktree, worktreesByRepo) {
  const chev = collapsed ? "▸" : "▾";
  const body = collapsed ? "" : worktreeBlocks.join("");
  const dim = repoMatchesFilter(repoKey, byWorktree, worktreesByRepo) ? "" : " rail-dim";
  const isOther = repoKey === OTHER_REPO_KEY;
  // "Other" is always pinned at the bottom — drag is meaningless. Omit
  // draggable so dragstart never fires on it.
  const dragAttr = isOther ? "" : ` draggable="true"`;
  const icon = isOther
    ? `<span class="rail-icon icon-other">◇</span>`
    : `<span class="rail-icon icon-repo">◆</span>`;
  return `
    <div class="rail-row repo-row${dim}" data-row="repo" data-key="${escapeHtml(`repo:${repoKey}`)}"${dragAttr}>
      <span class="rail-chev">${chev}</span>
      ${icon}
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

export function renderRail() {
  const el = railEl();
  if (!el) return;
  pruneDanglingEntries();
  attachRailListeners();

  const prefRepoOrder = prefs.getRepoOrder();
  const prefWorktreesByRepo = prefs.getWorktreesByRepo();
  const prefPanesByWorktree = prefs.getPanesByWorktree();
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

  // Auto-populate: the rail's membership is derived from live windows
  // that have a repo_key (worktree-backed). Prefs provide *ordering*
  // hints only — anything the user has drag-reordered keeps its place;
  // anything new lands at the end of its level. This replaces the
  // curated-entry model from the original spec — the friction of "+ open
  // for every session" wasn't worth it.
  const { repoOrder, worktreesByRepo, panesByWorktree } = mergeLiveAndPrefs(
    windows, prefRepoOrder, prefWorktreesByRepo, prefPanesByWorktree
  );

  if (repoOrder.length === 0) {
    el.innerHTML = `
      <div class="rail-head">
        <span>Projects</span>
      </div>
      <div class="rail-empty">
        No worktree-backed tmux sessions are open. Use <code>+ project</code> or <code>review PR</code> to start one.
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
      // Append + New tab affordance
      childMarkup.push(newTabRow(wtKey));
      const wtIsCollapsed = collapsed[`wt:${wtKey}`] === true;
      const rolledUp = maxSeverity(childStates);
      // Label: branch from any window in this worktree for git-backed
      // sessions; just the session name for "Other" entries (no branch).
      const label = repoKey === OTHER_REPO_KEY
        ? wtKey
        : ((wtWindows[0]?.branch) || wtKey);
      return worktreeRow(wtKey, childMarkup, wtIsCollapsed, rolledUp, label, byWorktree, repoKey);
    });
    // Repo rollup = max across worktree rollups.
    const allChildStates = (worktrees.flatMap(wt => (byWorktree[wt] || []).map(w => w.state || "shell")));
    const repoRolledUp = maxSeverity(allChildStates);
    return repoRow(repoKey, repoLabel, wtBlocks, wtCollapsed, repoRolledUp, byWorktree, worktreesByRepo);
  });

  el.innerHTML = `
    <div class="rail-head">
      <span>Projects</span>
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
    if (kind === "newtab") {
      const wt = row.dataset.worktree;
      const { openLauncher } = await import('./launcher-modal.js');
      openLauncher(wt);
      return;
    }
  });

  el.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".rail-row");
    if (!row) return;
    const kind = row.dataset.row;
    const key = row.dataset.key;
    state.railDragging = { kind, key, row };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", key);  // for cross-window compat — unused
    row.classList.add("dragging");
  });

  el.addEventListener("dragover", (e) => {
    const drag = state.railDragging;
    if (!drag) return;
    const row = e.target.closest(".rail-row");
    if (!row || row === drag.row) return;
    // Reject cross-level drops; allow pane <-> review interchange (they're siblings).
    const same = row.dataset.row === drag.kind;
    const paneReviewMix = (drag.kind === "pane" && row.dataset.row === "review")
      || (drag.kind === "review" && row.dataset.row === "pane");
    if (!same && !paneReviewMix) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  });

  el.addEventListener("drop", async (e) => {
    e.preventDefault();
    const drag = state.railDragging;
    if (!drag) return;
    const targetRow = e.target.closest(".rail-row");
    if (!targetRow) { state.railDragging = null; return; }

    if (drag.kind === "repo") {
      await reorderRepos(drag.key, targetRow.dataset.key);
    } else if (drag.kind === "worktree") {
      await reorderWorktrees(drag.key, targetRow.dataset.key);
    } else {
      // pane / review: reorder within their worktree.
      await reorderChildren(drag.row, targetRow);
    }
    drag.row.classList.remove("dragging");
    state.railDragging = null;
    renderRail();
  });

  // Drop never fires when the user releases outside any valid target —
  // clean up the dragging class + state regardless.
  el.addEventListener("dragend", () => {
    const drag = state.railDragging;
    if (drag?.row) drag.row.classList.remove("dragging");
    state.railDragging = null;
  });
}

// All three reorder helpers seed their starting order from mergeLiveAndPrefs
// (the same function renderRail uses) — that's the rendered order the user
// sees and is dragging within. Plain prefs alone would miss auto-populated
// entries that haven't been touched yet.

function currentMergedOrder() {
  return mergeLiveAndPrefs(
    state.lastWindows,
    prefs.getRepoOrder(),
    prefs.getWorktreesByRepo(),
    prefs.getPanesByWorktree()
  );
}

async function reorderRepos(draggedKey, targetKey) {
  const dragged = draggedKey.replace(/^repo:/, "");
  const target = targetKey.replace(/^repo:/, "");
  // "Other" is always pinned to the bottom; can't reorder around it.
  if (dragged === OTHER_REPO_KEY || target === OTHER_REPO_KEY) return;
  const { repoOrder } = currentMergedOrder();
  const order = repoOrder.filter(r => r !== OTHER_REPO_KEY);
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  order.splice(from, 1);
  order.splice(to, 0, dragged);
  await prefs.setRepoOrder(order);
}

async function reorderWorktrees(draggedKey, targetKey) {
  const dragged = draggedKey.replace(/^wt:/, "");
  const target = targetKey.replace(/^wt:/, "");
  const { worktreesByRepo } = currentMergedOrder();
  // Find which repo the dragged worktree currently belongs to in the
  // rendered tree; must match the target's repo.
  let repoKey = null;
  for (const [r, list] of Object.entries(worktreesByRepo)) {
    if (list.includes(dragged)) { repoKey = r; break; }
  }
  if (!repoKey) return;
  const list = [...worktreesByRepo[repoKey]];
  if (!list.includes(target)) return;  // cross-repo drag — reject
  const from = list.indexOf(dragged);
  const to = list.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  list.splice(from, 1);
  list.splice(to, 0, dragged);
  // Persist: keep other repos' prefs as-is, overwrite just this repo's list.
  const next = { ...prefs.getWorktreesByRepo(), [repoKey]: list };
  await prefs.setWorktreesByRepo(next);
}

async function reorderChildren(draggedRow, targetRow) {
  const draggedWt = closestWorktreeKey(draggedRow);
  const targetWt = closestWorktreeKey(targetRow);
  if (!draggedWt || draggedWt !== targetWt) return;

  const dragKey = childPrefKey(draggedRow);
  const targetKey = childPrefKey(targetRow);
  if (!dragKey || !targetKey) return;

  const { panesByWorktree } = currentMergedOrder();
  const list = [...(panesByWorktree[draggedWt] || [])];
  const from = list.indexOf(dragKey);
  const to = list.indexOf(targetKey);
  if (from < 0 || to < 0 || from === to) return;
  list.splice(from, 1);
  list.splice(to, 0, dragKey);
  const next = { ...prefs.getPanesByWorktree(), [draggedWt]: list };
  await prefs.setPanesByWorktree(next);
}

function closestWorktreeKey(row) {
  // Walk back through siblings to find the prior wt-row; its data-key is "wt:<key>".
  let n = row.previousElementSibling;
  while (n) {
    if (n.classList.contains("wt-row")) {
      return n.dataset.key.replace(/^wt:/, "");
    }
    n = n.previousElementSibling;
  }
  return null;
}

function childPrefKey(row) {
  if (row.dataset.row === "pane") return row.dataset.pid;
  if (row.dataset.row === "review") return "review";
  return null;
}

// Remove rail entries for sessions / panes that no longer exist in
// /api/state. Runs fire-and-forget — does NOT change renderRail's
// sync contract. If anything is pruned, the next poll re-renders with
// the updated prefs.
//
// patchUI is exported by prefs.js.

let lastPruneAt = 0;
function pruneDanglingEntries() {
  if (Date.now() - lastPruneAt < 5000) return;  // throttle to 5s
  lastPruneAt = Date.now();

  const live = state.lastWindows || [];
  const liveSessions = new Set(live.map(w => w.session));
  const livePids = new Set(live.map(w => w.pid));

  const wts = prefs.getWorktreesByRepo();
  const panes = prefs.getPanesByWorktree();
  const order = prefs.getRepoOrder();
  let changed = false;

  // Remove worktrees whose session is gone.
  for (const [repo, list] of Object.entries(wts)) {
    const kept = list.filter(wt => liveSessions.has(wt));
    if (kept.length !== list.length) {
      changed = true;
      if (kept.length === 0) {
        delete wts[repo];
        const idx = order.indexOf(repo);
        if (idx >= 0) order.splice(idx, 1);
      } else {
        wts[repo] = kept;
      }
    }
  }

  // Remove pane ids that aren't in livePids (keep "review" sentinels).
  for (const [wt, children] of Object.entries(panes)) {
    if (!liveSessions.has(wt)) { delete panes[wt]; changed = true; continue; }
    const kept = children.filter(c => c === "review" || livePids.has(c));
    if (kept.length !== children.length) { panes[wt] = kept; changed = true; }
  }

  if (changed) {
    // Fire and forget — the prefs write will be reflected by the next poll's render.
    prefs.patchUI({ repo_order: order, worktrees_by_repo: wts, panes_by_worktree: panes });
  }
}
