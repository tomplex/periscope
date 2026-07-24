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
// `tracks` is the /api/state track registry (non-archived rows). It exists so
// EMPTY GOAL TRACKS render: live windows alone can't surface a track with no
// tabs, and a freshly created track must be visible to receive its first tab
// (drag / "+ New tab") — otherwise creation dead-ends. Goal tracks (id !== repo
// — repo-default catchalls have id == repo) are always in the tree; an empty
// repo-default track stays hidden (it's a lazy catchall, an empty one is noise).
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
export function mergeLiveAndPrefs(windows, _projects, _workspaces, prefs = {}, tracks = []) {
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

  // Empty goal tracks join the tree alongside live membership (see header).
  const goalIds = (tracks || [])
    .filter(t => t.id && t.id !== t.repo)
    .map(t => t.id);
  const goalSet = new Set(goalIds);

  // Top-level order: pref-first (kept iff still live OR a goal track), then
  // live-new, then new empty goal tracks appended.
  const liveTrackSet = new Set(liveTracks);
  const fromPref = prefTrackOrder.filter(t => liveTrackSet.has(t) || goalSet.has(t));
  const fromPrefSet = new Set(fromPref);
  const trackOrder = [
    ...fromPref,
    ...liveTracks.filter(t => !fromPrefSet.has(t)),
    ...goalIds.filter(t => !fromPrefSet.has(t) && !liveTrackSet.has(t)),
  ];

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

// Top-level track row label. The backend ships `track_name` on every window
// (track_label() — goal tracks carry the user name, repo-default tracks carry
// basename(repo)); the rail reads it off any window in the track. An EMPTY
// track has no window to read from — its name comes from the registry rows
// (`tracks`). Falls back to the track id's path basename (defensive).
export function trackLabel(trackId, windows, tracks = []) {
  for (const w of (windows || [])) {
    if (w.track_id === trackId && w.track_name) return w.track_name;
  }
  for (const t of (tracks || [])) {
    if (t.id === trackId && t.name) return t.name;
  }
  if (trackId === "loose") return "loose";
  const parts = String(trackId || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || trackId;
}

// "loose" | "repo" | "goal" for a track row — mirrors trackLabel's lookup
// order. The backend ships `track_kind` on every window; an EMPTY track has no
// window to read from, so it derives from the registry row (a repo-default
// track has id === repo). Unknown reads as "repo": the rail hides lifecycle
// actions on the catchalls, and hiding a menu is the safe direction.
export function trackKind(trackId, windows, tracks = []) {
  if (trackId === "loose") return "loose";
  for (const w of (windows || [])) {
    if (w.track_id === trackId && w.track_kind) return w.track_kind;
  }
  for (const t of (tracks || [])) {
    if (t.id === trackId) return t.repo === t.id ? "repo" : "goal";
  }
  return "repo";
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
