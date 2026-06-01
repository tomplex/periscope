// Left rail for split view: derives the Repo → Worktree → Pane-children
// tree from prefs (curated membership/order) joined with /api/state
// (live status). Renders into #rail.
//
// rail.js only renders. Interactions (collapse, drag, select) are wired
// in later tasks but live in this file.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, prUrl, apiCall, targetQuery } from './util.js';
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
    <div class="rail-row child-row${sel}${dim}" data-row="pane" data-pid="${escapeHtml(w.pid)}" data-key="${escapeHtml(k)}" data-target="${escapeHtml(w.target || "")}" draggable="true">
      ${icon}
      <span class="rail-label">${escapeHtml(w.name || (w.is_claude ? "claude" : "shell"))}</span>
      <span class="${statusDotClass(w.state)}"></span>
      <button class="rail-close" data-action="close-pane" title="kill this tab">×</button>
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

// Compact per-worktree metadata strip: PR badge + CI glyph, Linear chip,
// git dirty indicator. Drawn from `wtWindows[0]` (all panes in a worktree
// share these — same branch, same repo). Returns "" when there's nothing
// to show, so the second line collapses entirely on bare worktrees.
function worktreeMetaLine(wtWindows) {
  const w = wtWindows[0];
  if (!w) return "";
  const parts = [];

  if (w.pr) {
    const href = prUrl(w.repo_slug, w.pr);
    const ciGlyph = w.ci ? `<span class="wt-meta-ci wt-meta-ci-${ciClass(w.ci)}">${escapeHtml(w.ci)}</span>` : "";
    const inner = `#${w.pr}${ciGlyph ? " " + ciGlyph : ""}`;
    parts.push(href
      ? `<a class="wt-meta-chip wt-meta-pr" href="${href}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="PR #${w.pr}">${inner}</a>`
      : `<span class="wt-meta-chip wt-meta-pr" title="PR #${w.pr}">${inner}</span>`);
  }

  if (w.linked_linear) {
    const lid = escapeHtml(w.linked_linear);
    const ltitle = w.linked_linear_title ? `: ${escapeHtml(w.linked_linear_title)}` : "";
    const lstatus = w.linked_linear_status ? ` [${escapeHtml(w.linked_linear_status)}]` : "";
    parts.push(
      `<a class="wt-meta-chip wt-meta-linear" href="https://linear.app/issue/${lid}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Linear ${lid}${ltitle}${lstatus}">${lid}</a>`
    );
  }

  // Show git-dirty when there are uncommitted changes (`git` is "clean" or
  // "+N -M" or includes "*" for ahead-of-upstream). Surfaced as a tiny
  // chip; absent when clean+pushed.
  if (w.git && w.git !== "clean") {
    parts.push(`<span class="wt-meta-chip wt-meta-git" title="git status">${escapeHtml(w.git)}</span>`);
  }

  return parts.length ? `<div class="wt-meta">${parts.join("")}</div>` : "";
}

function ciClass(ci) {
  if (ci === "✓") return "ok";
  if (ci === "✗") return "bad";
  if (ci === "⟳") return "running";
  return "neutral";
}

function worktreeRow(worktreeKey, children, collapsed, rolledUp, label, byWorktree, repoKey, wtWindows) {
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
  // Skip the metadata strip for "Other" — those sessions are bare shells
  // without PR/Linear/branch context.
  const meta = isOther ? "" : worktreeMetaLine(wtWindows);
  // Close button only on git-backed worktrees — closing an "Other"
  // session is per-pane (use the pane-row × instead).
  const closeBtn = isOther
    ? ""
    : `<button class="rail-close" data-action="close-worktree" title="kill this session">×</button>`;
  return `
    <div class="rail-row wt-row${dim}" data-row="worktree" data-key="${escapeHtml(`wt:${worktreeKey}`)}" data-session="${escapeHtml(worktreeKey)}" draggable="true">
      <span class="rail-chev">${chev}</span>
      ${icon}
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      ${childCountChip}
      <span class="${statusDotClass(rolledUp)}"></span>
      ${closeBtn}
    </div>
    ${meta}
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
  syncRailPrefs();
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
      return worktreeRow(wtKey, childMarkup, wtIsCollapsed, rolledUp, label, byWorktree, repoKey, wtWindows);
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

  // Double-click the label of a pane or worktree row → inline rename.
  // Mirrors the modal's existing dblclick-to-rename convention.
  el.addEventListener("dblclick", (e) => {
    const label = e.target.closest(".rail-label");
    if (!label) return;
    const row = label.closest(".rail-row");
    const kind = row?.dataset.row;
    if (kind !== "pane" && kind !== "worktree") return;
    e.preventDefault();
    e.stopPropagation();
    startInlineRename(row, label, kind);
  });

  el.addEventListener("click", async (e) => {
    // Close-button is a sibling of the row's click target — intercept
    // it BEFORE the row-level select fires.
    const closeBtn = e.target.closest("[data-action]");
    if (closeBtn) {
      e.stopPropagation();
      await handleRailAction(closeBtn);
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
    if (!row || row === drag.row) { clearDropTarget(); return; }
    if (!isValidDropTarget(drag, row)) { clearDropTarget(); return; }
    // Insert before/after target based on which half of the row the
    // pointer is in. Bottom half = insert AFTER (visualize as line at row's bottom).
    const rect = row.getBoundingClientRect();
    const insertAfter = (e.clientY - rect.top) > rect.height / 2;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDropTarget(row, insertAfter);
  });

  // Clear the indicator when the pointer leaves the rail entirely — the
  // dragover handler keeps it set as long as we're over a valid target.
  el.addEventListener("dragleave", (e) => {
    // dragleave fires for child elements too; only clear when leaving the
    // whole rail (relatedTarget is null or outside #rail).
    if (!e.relatedTarget || !el.contains(e.relatedTarget)) clearDropTarget();
  });

  el.addEventListener("drop", async (e) => {
    e.preventDefault();
    const drag = state.railDragging;
    if (!drag) { clearDropTarget(); return; }
    const targetRow = e.target.closest(".rail-row");
    if (!targetRow) { clearDropTarget(); state.railDragging = null; return; }

    const rect = targetRow.getBoundingClientRect();
    const insertAfter = (e.clientY - rect.top) > rect.height / 2;

    if (drag.kind === "repo") {
      await reorderRepos(drag.key, targetRow.dataset.key, insertAfter);
    } else if (drag.kind === "worktree") {
      await reorderWorktrees(drag.key, targetRow.dataset.key, insertAfter);
    } else {
      await reorderChildren(drag.row, targetRow, insertAfter);
    }
    drag.row.classList.remove("dragging");
    clearDropTarget();
    state.railDragging = null;
    renderRail();
  });

  // Drop never fires when the user releases outside any valid target —
  // clean up the dragging class + state regardless.
  el.addEventListener("dragend", () => {
    const drag = state.railDragging;
    if (drag?.row) drag.row.classList.remove("dragging");
    clearDropTarget();
    state.railDragging = null;
  });
}

// --- Drop-target indicator -------------------------------------------------
//
// Visual feedback during drag: a colored line at top (insert before) or
// bottom (insert after) of the row the pointer is over. Drives both the
// visual hint and the actual splice position on drop.

let dropTargetRow = null;
function setDropTarget(row, insertAfter) {
  if (dropTargetRow === row && row.dataset.dropPos === (insertAfter ? "after" : "before")) return;
  clearDropTarget();
  row.classList.add("drop-target");
  row.dataset.dropPos = insertAfter ? "after" : "before";
  dropTargetRow = row;
}
function clearDropTarget() {
  if (!dropTargetRow) return;
  dropTargetRow.classList.remove("drop-target");
  delete dropTargetRow.dataset.dropPos;
  dropTargetRow = null;
}

// Same-kind, same-parent rule:
//   repo → any other repo (except Other, which is pinned)
//   worktree → another worktree in the same repo
//   pane/review → another pane/review in the same worktree
function isValidDropTarget(drag, row) {
  const targetKind = row.dataset.row;
  if (drag.kind === "repo") {
    if (targetKind !== "repo") return false;
    if (row.dataset.key === `repo:${OTHER_REPO_KEY}`) return false;
    if (drag.key === `repo:${OTHER_REPO_KEY}`) return false;
    return true;
  }
  if (drag.kind === "worktree") {
    if (targetKind !== "worktree") return false;
    return closestRepoKey(drag.row) === closestRepoKey(row);
  }
  // pane / review
  if (targetKind !== "pane" && targetKind !== "review") return false;
  return closestWorktreeKey(drag.row) === closestWorktreeKey(row);
}

// Inline rename: swap the label for an <input>, focus + select all,
// commit on Enter / blur, cancel on Escape. For pane rows we POST to
// /api/rename (renames the tmux window); for worktree rows we POST to
// /api/session/rename (renames the tmux session — every pane's
// `session` field changes on the next /api/state poll, and prefs that
// key on session name get reconciled by syncRailPrefs).
function startInlineRename(row, labelEl, kind) {
  // Strip any rich child markup (e.g. the `<b>` wrapper on worktree
  // labels) and use plain text as the editable value.
  const current = labelEl.textContent.trim();
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rail-rename-input";
  input.value = current;
  input.spellcheck = false;
  // Stash original innerHTML so cancel can restore the bold-wrapped
  // worktree label without re-rendering the whole rail.
  const original = labelEl.innerHTML;
  labelEl.innerHTML = "";
  labelEl.appendChild(input);
  input.focus();
  input.select();

  let settled = false;
  const cancel = () => {
    if (settled) return;
    settled = true;
    labelEl.innerHTML = original;
  };
  const commit = async () => {
    if (settled) return;
    settled = true;
    const next = input.value.trim();
    if (!next || next === current) { labelEl.innerHTML = original; return; }
    if (kind === "pane") {
      const target = row.dataset.target;
      if (!target) { labelEl.innerHTML = original; return; }
      await apiCall("rename tab", `/api/rename?${targetQuery(target)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: next }),
      });
    } else {
      const session = row.dataset.session;
      if (!session) { labelEl.innerHTML = original; return; }
      await apiCall("rename session", `/api/session/rename?session=${encodeURIComponent(session)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: next }),
      });
    }
    // Force a fresh /api/state poll to pick up the new name immediately
    // instead of waiting up to 3s for the regular tick.
    renderRail();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  input.addEventListener("blur", () => { commit(); });
}

// Close-action handler. `data-action` is "close-pane" (kills the tmux
// window for a single pane) or "close-worktree" (kills the entire tmux
// session — every pane in the worktree). Both prompt for confirmation;
// killing the worktree is destructive enough to warrant a deliberate
// extra click. The worktree directory on disk is NOT removed — that's
// the existing cleanup flow's job.
async function handleRailAction(btn) {
  const row = btn.closest(".rail-row");
  if (!row) return;
  const action = btn.dataset.action;
  if (action === "close-pane") {
    const target = row.dataset.target;
    const label = row.querySelector(".rail-label")?.textContent || "tab";
    if (!window.confirm(`Close tab "${label}"?\n\nThis kills its tmux window.`)) return;
    await apiCall("close tab", `/api/window?${targetQuery(target)}`, { method: "DELETE" });
    // /api/state polls will reflect the deletion on the next tick (~3s);
    // proactively re-render so the row disappears immediately.
    renderRail();
    return;
  }
  if (action === "close-worktree") {
    const session = row.dataset.session;
    if (!window.confirm(`Close session "${session}"?\n\nThis kills every tmux window in this worktree.\nThe worktree directory on disk is not removed.`)) return;
    await apiCall("close session", `/api/session?session=${encodeURIComponent(session)}`, { method: "DELETE" });
    renderRail();
    return;
  }
}

function closestRepoKey(row) {
  // Walk back through DOM siblings to find the closest preceding repo-row.
  let n = row.previousElementSibling;
  while (n) {
    if (n.classList?.contains("repo-row")) {
      return n.dataset.key.replace(/^repo:/, "");
    }
    n = n.previousElementSibling;
  }
  return null;
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

// Splice helper used by all reorder functions. Moves `from`-index entry
// to land either before or after the target index, keeping the list's
// length invariant.
function spliceMove(list, fromIdx, toIdx, insertAfter) {
  if (fromIdx < 0 || toIdx < 0) return list;
  const [val] = list.splice(fromIdx, 1);
  // After removal, the target index shifts down by 1 if the removed
  // entry was earlier in the list.
  let landing = toIdx - (fromIdx < toIdx ? 1 : 0);
  if (insertAfter) landing += 1;
  list.splice(landing, 0, val);
  return list;
}

async function reorderRepos(draggedKey, targetKey, insertAfter = false) {
  const dragged = draggedKey.replace(/^repo:/, "");
  const target = targetKey.replace(/^repo:/, "");
  // "Other" is always pinned to the bottom; can't reorder around it.
  if (dragged === OTHER_REPO_KEY || target === OTHER_REPO_KEY) return;
  const { repoOrder } = currentMergedOrder();
  const order = repoOrder.filter(r => r !== OTHER_REPO_KEY);
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0) return;
  spliceMove(order, from, to, insertAfter);
  await prefs.setRepoOrder(order);
}

async function reorderWorktrees(draggedKey, targetKey, insertAfter = false) {
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
  if (from < 0 || to < 0) return;
  spliceMove(list, from, to, insertAfter);
  const next = { ...prefs.getWorktreesByRepo(), [repoKey]: list };
  await prefs.setWorktreesByRepo(next);
}

async function reorderChildren(draggedRow, targetRow, insertAfter = false) {
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
  if (from < 0 || to < 0) return;
  spliceMove(list, from, to, insertAfter);
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

// Reconcile prefs with live state so ordering is sticky across restarts
// regardless of whether the user has explicitly drag-reordered anything.
//
// Two directions:
//   - Prune: remove pref entries whose live session/pid is gone.
//   - Extend: persist any NEW live entries at the position the merge
//     gave them. Without this, repo_order/etc stay empty until the user
//     drags, and the live-order from /api/state (which can shuffle) is
//     the perceived order on every reload.
//
// Throttled to 5s to avoid spamming /api/prefs on every poll.

let lastPruneAt = 0;
function syncRailPrefs() {
  if (Date.now() - lastPruneAt < 5000) return;
  lastPruneAt = Date.now();

  const live = state.lastWindows || [];
  if (live.length === 0) return;   // /api/state hasn't loaded yet — don't write empty

  const merged = currentMergedOrder();
  const prefRepoOrder = prefs.getRepoOrder();
  const prefWtByRepo = prefs.getWorktreesByRepo();
  const prefPanesByWt = prefs.getPanesByWorktree();

  // The merged result already strips dead entries (mergeLiveAndPrefs
  // filters pref by liveSet) and appends new live ones. Stripping the
  // synthetic OTHER bucket before persisting — that's derived, not user
  // intent. Repo order excludes Other (always pinned at render-time).
  const nextRepoOrder = merged.repoOrder.filter(r => r !== OTHER_REPO_KEY);
  const nextWtByRepo = { ...merged.worktreesByRepo };
  delete nextWtByRepo[OTHER_REPO_KEY];
  // panesByWorktree: only persist worktrees that are git-backed (skip
  // Other's children — they're transient and don't need ordering).
  const nextPanesByWt = {};
  for (const r of nextRepoOrder) {
    for (const wt of (nextWtByRepo[r] || [])) {
      nextPanesByWt[wt] = merged.panesByWorktree[wt] || [];
    }
  }

  // Cheap deep-equal via JSON. Short-circuits the write when nothing
  // actually changed since the last sync.
  if (
    JSON.stringify(nextRepoOrder) === JSON.stringify(prefRepoOrder) &&
    JSON.stringify(nextWtByRepo) === JSON.stringify(prefWtByRepo) &&
    JSON.stringify(nextPanesByWt) === JSON.stringify(prefPanesByWt)
  ) return;

  prefs.patchUI({
    repo_order: nextRepoOrder,
    worktrees_by_repo: nextWtByRepo,
    panes_by_worktree: nextPanesByWt,
  });
}
