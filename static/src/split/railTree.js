// Pure tree-building for the split-view rail. Ported verbatim from
// rail.js:mergeLiveAndPrefs (+ its severity rollup + repo-label helper) — no
// behavior change. Kept pure (no signals, no DOM) because it's consumed TWICE:
// once to render <Rail>, and once as `currentMergedOrder()`'s seed for the
// drag-reorder splices (so a drag operates on the order the user actually
// sees, not raw prefs which may be missing auto-populated entries).
//
// OTHER_REPO_KEY is pinned to the bottom at every one of the four points the
// rail enforces it: merge (here), isValidDropTarget, reorderRepos, and the
// repoRow drag-attr gate (in Rail.jsx).

// Synthetic repo key for non-worktree-backed sessions (bare shells, non-git
// cwds). Renders as a top-level "Other" group at the bottom with sessions as
// direct children (no review row, no branch).
export const OTHER_REPO_KEY = "__other__";

// Severity ranking for status rollup: higher index = higher priority.
const SEVERITY = ["shell", "idle", "done", "working", "needs-input"];

export function maxSeverity(states) {
  let best = -1;
  for (const s of states) {
    const i = SEVERITY.indexOf(s);
    if (i > best) best = i;
  }
  return best >= 0 ? SEVERITY[best] : "shell";
}

// Merge live /api/state windows with persisted ordering prefs to produce the
// actual rail tree. Live windows ARE the membership; prefs are ordering hints —
// entries in pref order come first (in their pref position), then new live
// entries append. Pref entries for repos/worktrees/panes that are no longer
// live are silently dropped from the rendered output.
export function mergeLiveAndPrefs(windows, prefRepoOrder, prefWtByRepo, prefPanesByWt) {
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

  // Pane-children order per worktree: prefs first (filtered), then new live
  // pids. The "review" sentinel is auto-added for git-backed worktrees only —
  // non-worktree sessions under "Other" have no review row.
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

// Build a quick { worktreeKey: [windowObj, ...] } map from /api/state windows.
export function indexWindowsByWorktree(windows) {
  const out = {};
  for (const w of (windows || [])) {
    const key = w.session;  // worktree_key = session name
    (out[key] = out[key] || []).push(w);
  }
  return out;
}

// Look up the human-readable repo label for a repo_key. Pulled off any window
// whose `repo_key` matches; falls back to basename of the repo_key path. The
// synthetic OTHER_REPO_KEY renders as "Other".
export function repoLabelFor(repoKey, windows) {
  if (repoKey === OTHER_REPO_KEY) return "Other";
  for (const w of (windows || [])) {
    if (w.repo_key === repoKey && w.repo_label) return w.repo_label;
  }
  const parts = String(repoKey || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || repoKey;
}
