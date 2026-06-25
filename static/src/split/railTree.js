// Pure tree-building for the split-view rail. Kept pure (no signals, no DOM)
// because it's consumed TWICE: once to render <Rail>, and once as
// `currentMergedOrder()`'s seed for the drag-reorder splices (so a drag
// operates on the order the user actually sees, not raw prefs).
//
// Membership is TRACK-ANCHORED (2026-06-25 spec): a window belongs to its
// track (`w.track_id`, resolved server-side — always present, the repo-default
// fallback guarantees a value). track_id is the SOLE grouping authority; the
// old repo/workspace/MAIN_KEY trichotomy collapsed into it. cd never moves a
// row; the cwd-derived repo_key/branch fields are display-only (chips).
//
// Mid-tier is DERIVED, not a pref: within a track, tabs group by `w.branch`.
// A track spanning ≥2 distinct branches emits branch sub-clusters; at ≤1
// branch it renders flat. There is no catchall group and no separate
// workspace tier — the no-tag case is handled backend-side (the resolution
// ladder always returns a home track).

// Retained only for `groupLabel`'s "dev" label and Rail.jsx's still-present
// imports (both rewritten in Task 13 when the catchall group is fully removed).
// No longer a grouping key — track_id is the sole authority.
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

// Empty-string fallback bucket for a window that arrives without a `branch`
// (non-git cwd, pre-resolution race). Kept a distinct key so it counts as its
// own branch in the distinct-count rather than colliding with a real branch.
const NO_BRANCH = "";

// Merge live /api/state windows with persisted ordering prefs to produce the
// rail tree. Live windows ARE the membership; prefs are ordering hints — pref
// entries come first (in pref position), then new live entries append; pref
// entries no longer live are silently dropped.
//
// `projects`/`workspaces` are accepted (and ignored for grouping) purely so
// existing callers don't break on the signature; labels still index projects.
//
// `prefs` is `{ trackOrder?, tabsByTrack?, branchOrderByTrack? }`:
//   trackOrder          — ordered track ids (replaces the old repo_order)
//   tabsByTrack         — { trackId: [pid, ...] } tab order within a track
//                         (replaces panes_by_worktree, now keyed by track id)
//   branchOrderByTrack  — { trackId: [branch, ...] } branch sub-cluster order
//
// Return shape:
//   trackOrder       — ordered live track ids (pref-first, live-new appended)
//   tabsByTrack      — { trackId: [pid, ...] } the FLAT all-tabs order for a
//                      track; always present, used directly when a track is
//                      single-branch (flat render)
//   branchesByTrack  — { trackId: [branch, ...] } the ordered distinct
//                      branches; [] (empty) means "single-branch → render
//                      flat from tabsByTrack"
//   tabsByBranch     — { trackId: { branch: [pid, ...] } } per-branch tab
//                      order; only populated for MULTI-branch tracks
export function mergeLiveAndPrefs(windows, _projects, _workspaces, prefs = {}) {
  const prefTrackOrder = prefs.trackOrder || [];
  const prefTabsByTrack = prefs.tabsByTrack || {};
  const prefBranchOrder = prefs.branchOrderByTrack || {};

  const liveTracks = [];                 // ordered track ids (first-seen)
  const liveTabsByTrack = {};            // trackId → [pid] (first-seen)
  const liveBranchesByTrack = {};        // trackId → [branch] (first-seen)
  const liveTabsByBranch = {};           // trackId → { branch: [pid] }
  for (const w of (windows || [])) {
    const t = w.track_id;
    if (!t) continue;  // backend guarantees a track_id; skip defensively
    const branch = w.branch || NO_BRANCH;
    if (!liveTabsByTrack[t]) {
      liveTracks.push(t);
      liveTabsByTrack[t] = [];
      liveBranchesByTrack[t] = [];
      liveTabsByBranch[t] = {};
    }
    if (!liveTabsByTrack[t].includes(w.pid)) liveTabsByTrack[t].push(w.pid);
    if (!liveBranchesByTrack[t].includes(branch)) liveBranchesByTrack[t].push(branch);
    if (!liveTabsByBranch[t][branch]) liveTabsByBranch[t][branch] = [];
    if (!liveTabsByBranch[t][branch].includes(w.pid)) liveTabsByBranch[t][branch].push(w.pid);
  }

  // Top-level order: pref-first (kept iff still live), then live-new appended.
  const liveTrackSet = new Set(liveTracks);
  const fromPref = prefTrackOrder.filter(t => liveTrackSet.has(t));
  const fromPrefSet = new Set(fromPref);
  const trackOrder = [...fromPref, ...liveTracks.filter(t => !fromPrefSet.has(t))];

  const tabsByTrack = {};
  const branchesByTrack = {};
  const tabsByBranch = {};
  for (const t of trackOrder) {
    // Flat tab order: pref-first (kept iff live), then new live pids.
    const liveTabs = liveTabsByTrack[t] || [];
    const liveTabSet = new Set(liveTabs);
    const prefTabs = (prefTabsByTrack[t] || []).filter(p => liveTabSet.has(p));
    const prefTabSet = new Set(prefTabs);
    tabsByTrack[t] = [...prefTabs, ...liveTabs.filter(p => !prefTabSet.has(p))];

    // Distinct branches: pref-first (kept iff live), then live-new branches.
    const liveBranches = liveBranchesByTrack[t] || [];
    const liveBranchSet = new Set(liveBranches);
    const prefBranches = (prefBranchOrder[t] || []).filter(b => liveBranchSet.has(b));
    const prefBranchSet = new Set(prefBranches);
    const branches = [...prefBranches, ...liveBranches.filter(b => !prefBranchSet.has(b))];

    if (branches.length >= 2) {
      branchesByTrack[t] = branches;
      tabsByBranch[t] = {};
      for (const b of branches) tabsByBranch[t][b] = liveTabsByBranch[t][b] || [];
    } else {
      branchesByTrack[t] = [];  // single-branch → flat render from tabsByTrack
    }
  }

  return { trackOrder, tabsByTrack, branchesByTrack, tabsByBranch };
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
