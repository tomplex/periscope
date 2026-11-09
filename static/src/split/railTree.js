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
// Mid-tier is DERIVED, not a pref: within a track that spans ≥2 distinct
// branches, a branch that two or more tabs share becomes a sub-cluster. With
// one worktree per branch most branches hold a single tab, and a header per
// tab doubled the rail's height, so those tabs stay flat and show their
// branch themselves. There is no catchall group and no separate workspace
// tier — the no-tag case is handled backend-side (the resolution ladder
// always returns a home track).

// Empty-string fallback bucket for a window that arrives without a `branch`
// (non-git cwd, pre-resolution race). Kept a distinct key so it counts as its
// own branch in the distinct-count rather than colliding with a real branch,
// but it never clusters: "(no branch)" says nothing the tabs share.
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
// `prefs` is `{ trackOrder?, tabsByTrack? }`:
//   trackOrder   — ordered track ids (replaces the old repo_order)
//   tabsByTrack  — { trackId: [pid, ...] } tab order within a track
//                  (replaces panes_by_worktree, now keyed by track id)
//
// Return shape:
//   trackOrder   — ordered live track ids (pref-first, live-new appended)
//   tabsByTrack  — { trackId: [pid, ...] } the FLAT all-tabs order for a
//                  track; always present (drag reorders splice this)
//   rowsByTrack  — { trackId: [{ pid } | { branch, pids }] } what the
//                  track's card lists, in tab order. A shared branch is one
//                  { branch, pids } cluster, placed where its first tab sits;
//                  every other tab is its own { pid }. A single-branch track
//                  never clusters (its header already says where it is), and
//                  neither does the no-branch bucket.
export function mergeLiveAndPrefs(windows, _projects, _workspaces, prefs = {}, tracks = []) {
  const prefTrackOrder = prefs.trackOrder || [];
  const prefTabsByTrack = prefs.tabsByTrack || {};

  const liveTracks = [];                 // ordered track ids (first-seen)
  const liveTabsByTrack = {};            // trackId → [pid] (first-seen)
  const branchOf = {};                   // pid → branch (buckets the merged order)
  for (const w of (windows || [])) {
    const t = w.track_id;
    if (!t) continue;  // backend guarantees a track_id; skip defensively
    if (!liveTabsByTrack[t]) {
      liveTracks.push(t);
      liveTabsByTrack[t] = [];
    }
    if (!liveTabsByTrack[t].includes(w.pid)) liveTabsByTrack[t].push(w.pid);
    branchOf[w.pid] = w.branch || NO_BRANCH;
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
  const rowsByTrack = {};
  for (const t of trackOrder) {
    // Flat tab order: pref-first (kept iff live), then new live pids.
    const liveTabs = liveTabsByTrack[t] || [];
    const liveTabSet = new Set(liveTabs);
    const prefTabs = (prefTabsByTrack[t] || []).filter(p => liveTabSet.has(p));
    const prefTabSet = new Set(prefTabs);
    tabsByTrack[t] = [...prefTabs, ...liveTabs.filter(p => !prefTabSet.has(p))];

    // Clusters are built from the MERGED flat order, not the raw live order,
    // so a drag inside a cluster keeps the pref it just wrote instead of
    // snapping back on the next poll.
    const count = {};
    for (const pid of tabsByTrack[t]) count[branchOf[pid]] = (count[branchOf[pid]] || 0) + 1;
    const multiBranch = Object.keys(count).length >= 2;
    const clusterOf = {};
    rowsByTrack[t] = [];
    for (const pid of tabsByTrack[t]) {
      const b = branchOf[pid];
      if (!multiBranch || b === NO_BRANCH || count[b] < 2) {
        rowsByTrack[t].push({ pid });
        continue;
      }
      if (!clusterOf[b]) {
        clusterOf[b] = { branch: b, pids: [] };
        rowsByTrack[t].push(clusterOf[b]);
      }
      clusterOf[b].pids.push(pid);
    }
  }

  return { trackOrder, tabsByTrack, rowsByTrack };
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

// Chip text for a pane row, or null. The chip exists to flag DIVERGENCE —
// a pane whose cwd is not its track's own worktree — so at-pin panes never
// chip (the track label above them already says where they are). Built from
// aff.kind + the window's OWN git/cwd fields — aff.label is only trusted for
// the sibling case (off-repo's label is basename(cwd)).
export function paneChip(w) {
  const aff = w.worktree_affiliation || {};
  if (aff.kind === "sibling") return aff.label || null;
  if (aff.kind === "no-repo" || aff.kind === "off-repo") {
    if (w.repo_key && w.repo_label) {
      return w.branch ? `${w.repo_label}/${w.branch}` : w.repo_label;
    }
    if (w.cwd) return tildify(w.cwd);
  }
  return null;
}

// Where each review row renders in the rail: the FIRST track (in trackOrder)
// holding a live window on the row's (repo, branch). Keyed on repo+branch,
// not track — a repo-default track holds every branch of the repo. Rows with
// no walkthrough yet are not rail rows (the queue lists them).
// Returns { [trackId]: { [branch]: row[] } }.
export function reviewRowsByTrack(trackOrder, tabsByTrack, windowsByPid, rows) {
  const out = {};
  for (const row of rows || []) {
    if (!row.walkthrough) continue;
    for (const t of trackOrder) {
      const hit = (tabsByTrack[t] || []).map((pid) => windowsByPid[pid])
        .find((w) => w && w.repo_label === row.repo && (w.branch || NO_BRANCH) === row.branch);
      if (!hit) continue;
      if (!out[t]) out[t] = {};
      if (!out[t][row.branch]) out[t][row.branch] = [];
      out[t][row.branch].push(row);
      break;
    }
  }
  return out;
}
