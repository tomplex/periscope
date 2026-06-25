// Pure tree-building for the split-view rail. Kept pure (no signals, no DOM)
// because it's consumed TWICE: once to render <Rail>, and once as
// `currentMergedOrder()`'s seed for the drag-reorder splices (so a drag
// operates on the order the user actually sees, not raw prefs).
//
// Membership is SESSION-ANCHORED (2026-06-12 spec): a window belongs to its
// tmux session's project (`project_pinned_dir`, resolved server-side), and
// the project's `repo` field keys the top-level group. cd never moves a row;
// the cwd-derived repo_key/branch fields are display-only (chips).
//
// MAIN_KEY ("dev") is the catch-all group: __main__'s own session, folded
// unmanaged sessions, and no-row pins (archived projects / delete races).
// Dev renders as a FLAT pane list — worktreesByRepo[MAIN_KEY] is always []
// and panesByWorktree[MAIN_KEY] holds the unified pid order (a persisted
// pref key). Pinned to the bottom at five enforcement points: merge (here),
// isValidDropTarget, reorderRepos, the RepoRow drag-attr gate, and
// syncRailPrefs (Rail.jsx).

export const MAIN_KEY = "__main__";

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

// { pinned_dir: projectRow } from the /api/state projects payload.
export function indexProjects(projects) {
  const out = {};
  for (const p of (projects || [])) out[p.pinned_dir] = p;
  return out;
}

// { id: workspaceRow } from the /api/state workspaces payload.
export function indexWorkspaces(workspaces) {
  const out = {};
  for (const w of (workspaces || [])) out[w.id] = w;
  return out;
}

// Window → top-level group key. Folds to MAIN_KEY when the window has no
// project pin, is pinned to main, or its pin has no row in the payload
// (archived project or just-deleted race — projects_view filters archived).
// Null-repo projects key their own group by pinned_dir.
export function groupKeyForWindow(w, projectsByPin, workspacesById) {
  const wid = w.workspace_id;
  if (wid && workspacesById?.[wid]) return `ws:${wid}`;
  const pin = w.project_pinned_dir;
  if (!pin || pin === MAIN_KEY) return MAIN_KEY;
  const row = projectsByPin[pin];
  if (!row) return MAIN_KEY;
  return row.repo || pin;
}

// Merge live /api/state windows + projects with persisted ordering prefs to
// produce the rail tree. Live windows ARE the membership; prefs are ordering
// hints — pref entries come first (in pref position), then new live entries
// append. Pref entries no longer live are silently dropped.
//
// Return shape (unchanged from the cwd-keyed era, so drag descriptors,
// reorder splices, and syncRailPrefs keep working on key substitution):
//   repoOrder        — group keys, MAIN_KEY last iff dev has windows
//   worktreesByRepo  — group key → ordered session list ([] for MAIN_KEY)
//   panesByWorktree  — session → ordered child keys (pids + "review");
//                      panesByWorktree[MAIN_KEY] = flat dev pid order
export function mergeLiveAndPrefs(windows, projects, workspaces, prefRepoOrder, prefWtByRepo, prefPanesByWt) {
  const projectsByPin = indexProjects(projects);
  const workspacesById = indexWorkspaces(workspaces);
  const allWsKeys = (workspaces || []).map(w => `ws:${w.id}`);
  const liveByRepo = {};       // group key → ordered session list (first-seen)
  const livePanesByWt = {};    // session → ordered pane pids (first-seen)
  const liveDevPids = [];      // flat dev membership (cross-session)
  const livePanesByWs = {};    // ws:<id> → flat tagged pid order (cross-session)
  for (const w of (windows || [])) {
    const g = groupKeyForWindow(w, projectsByPin, workspacesById);
    if (g.startsWith("ws:")) {
      if (!livePanesByWs[g]) livePanesByWs[g] = [];
      if (!livePanesByWs[g].includes(w.pid)) livePanesByWs[g].push(w.pid);
      continue;
    }
    if (g === MAIN_KEY) {
      if (!liveDevPids.includes(w.pid)) liveDevPids.push(w.pid);
      continue;
    }
    const s = w.session;
    if (!liveByRepo[g]) liveByRepo[g] = [];
    if (!liveByRepo[g].includes(s)) liveByRepo[g].push(s);
    if (!livePanesByWt[s]) livePanesByWt[s] = [];
    if (!livePanesByWt[s].includes(w.pid)) livePanesByWt[s].push(w.pid);
  }

  // Top-level order: pref-first (kept iff a live repo OR a current ws key),
  // then live-new repos, then ws keys not yet in pref (parked / new). ws keys
  // are interleaved (NOT bottom-pinned). Dev (MAIN_KEY) is always last.
  const liveRepoSet = new Set(Object.keys(liveByRepo));
  const wsKeySet = new Set(allWsKeys);
  const keep = (k) => k !== MAIN_KEY && (liveRepoSet.has(k) || wsKeySet.has(k));
  const fromPref = prefRepoOrder.filter(keep);
  const fromPrefSet = new Set(fromPref);
  const newRepos = Object.keys(liveByRepo).filter(r => !fromPrefSet.has(r) && r !== MAIN_KEY);
  const newWs = allWsKeys.filter(k => !fromPrefSet.has(k));
  const realTop = [...fromPref, ...newRepos, ...newWs];
  const repoOrder = liveDevPids.length ? [...realTop, MAIN_KEY] : realTop;

  // Worktree (session) lists: ws:/dev are flat → [].
  const worktreesByRepo = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY || r.startsWith("ws:")) { worktreesByRepo[r] = []; continue; }
    const live = liveByRepo[r] || [];
    const liveSet = new Set(live);
    const pref = (prefWtByRepo[r] || []).filter(w => liveSet.has(w));
    const prefSet = new Set(pref);
    worktreesByRepo[r] = [...pref, ...live.filter(w => !prefSet.has(w))];
  }

  // Pane children per repo session (unchanged); ws:/dev handled flat below.
  const panesByWorktree = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY || r.startsWith("ws:")) continue;
    const own = projectsByPin[r];
    const hasReview = !(own && !own.repo);
    for (const w of worktreesByRepo[r]) {
      const live = livePanesByWt[w] || [];
      const liveSet = new Set(live);
      const pref = prefPanesByWt[w] || [];
      const prefKept = pref.filter(c => (c === "review" && hasReview) || liveSet.has(c));
      const prefSet = new Set(prefKept);
      const merged = [...prefKept, ...live.filter(p => !prefSet.has(p))];
      if (hasReview && !merged.includes("review")) merged.push("review");
      panesByWorktree[w] = merged;
    }
  }
  // Dev flat (unchanged).
  if (liveDevPids.length) {
    const liveSet = new Set(liveDevPids);
    const pref = (prefPanesByWt[MAIN_KEY] || []).filter(p => liveSet.has(p));
    const prefSet = new Set(pref);
    panesByWorktree[MAIN_KEY] = [...pref, ...liveDevPids.filter(p => !prefSet.has(p))];
  }
  // Workspace flat: ALWAYS build (parked → []), so every non-archived
  // workspace renders. pref-first pid order, then new live tagged pids.
  for (const k of allWsKeys) {
    const live = livePanesByWs[k] || [];
    const liveSet = new Set(live);
    const pref = (prefPanesByWt[k] || []).filter(p => liveSet.has(p));
    const prefSet = new Set(pref);
    panesByWorktree[k] = [...pref, ...live.filter(p => !prefSet.has(p))];
  }

  return { repoOrder, worktreesByRepo, panesByWorktree };
}

// Build a quick { worktreeKey: [windowObj, ...] } map from /api/state windows.
export function indexWindowsByWorktree(windows) {
  const out = {};
  for (const w of (windows || [])) {
    const key = w.session;  // worktree_key = session name
    out[key] = out[key] || [];
    out[key].push(w);
  }
  return out;
}

// Project-row label: stable (never cwd-derived — the first-pane branch
// churned on cd, the exact instability this design kills).
export function projectLabel(project, session) {
  return (project && (project.name || project.base_branch)) || session;
}

// Top-level group label: "dev" for MAIN_KEY; a null-repo project's own group
// uses its name; repo groups use the path basename.
export function groupLabel(groupKey, projectsByPin) {
  if (groupKey === MAIN_KEY) return "dev";
  const own = projectsByPin[groupKey];
  if (own && !own.repo && own.name) return own.name;
  const parts = String(groupKey || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || groupKey;
}

// ~-relative path for chips. Pure string transform (the frontend doesn't
// know $HOME): collapses a leading /Users/<u> or /home/<u>.
function tildify(p) {
  return String(p || "").replace(/^\/(?:Users|home)\/[^/]+/, "~");
}

// Chip text for a pane row, or null (at-pin / nothing to say). Built from
// aff.kind + the window's OWN git/cwd fields — aff.label is only trusted for
// the sibling case (off-repo's label is basename(cwd), and dev panes always
// get {kind: no-repo, label: null} because __main__ is unpinned).
export function paneChip(w, { isDev = false, sessionPrefix = null } = {}) {
  const aff = w.worktree_affiliation || {};
  let text = null;
  if (isDev || aff.kind === "no-repo" || aff.kind === "off-repo") {
    if (w.repo_key && w.repo_label) {
      text = w.branch ? `${w.repo_label}/${w.branch}` : w.repo_label;
    } else if (w.cwd) {
      text = tildify(w.cwd);
    }
  } else if (aff.kind === "sibling") {
    text = aff.label || null;
  }
  // at-pin (non-dev) never chips, whatever the git fields say.
  if (aff.kind === "at-pin" && !isDev) text = null;
  if (!text) return sessionPrefix || null;
  return sessionPrefix ? `${sessionPrefix}: ${text}` : text;
}
