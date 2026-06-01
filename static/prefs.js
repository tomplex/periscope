// Cache of /api/prefs + mutators. Frontend modules call into here instead
// of touching localStorage directly. See
// docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md.

import { apiCall } from './util.js';

// The cache mirrors the server's state.json shape. `loaded` flips to true
// only after a successful loadPrefs(); mutators refuse to write while false.
const cache = {
  loaded: false,
  ui: {},
  windows: {},
  commands: [],
};

export async function loadPrefs() {
  try {
    const res = await fetch("/api/prefs");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cache.ui = data.ui || {};
    cache.windows = data.windows || {};
    cache.commands = data.commands || [];
    cache.loaded = true;
    await migrateLocalStorage();
    return cache;
  } catch (err) {
    cache.loaded = false;
    return null;
  }
}

// ── UI prefs ────────────────────────────────────────────────────────────

export function getSessionOrder() {
  return cache.ui.session_order || [];
}

export function getCollapsed() {
  // grid.js consumes a Set — keep the existing call sites unchanged.
  return new Set(cache.ui.collapsed_sessions || []);
}

export function getView() {
  const v = cache.ui?.view;
  return (v === "stream" || v === "split") ? v : "grid";
}

export function getAlertsOpen() {
  return !!cache.ui.alerts_open;
}

export function getCommands() {
  return cache.commands || [];
}

export async function addCommand({ label, exec }) {
  if (!cache.loaded) return false;
  const data = await apiCall("add command", "/api/prefs/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function updateCommand(oldLabel, { label, exec }) {
  if (!cache.loaded) return false;
  const data = await apiCall("update command", `/api/prefs/commands/${encodeURIComponent(oldLabel)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function deleteCommand(label) {
  if (!cache.loaded) return false;
  const data = await apiCall("delete command", `/api/prefs/commands/${encodeURIComponent(label)}`, {
    method: "DELETE",
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function reorderCommands(labels) {
  if (!cache.loaded) return false;
  const data = await apiCall("reorder commands", "/api/prefs/commands", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ labels }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function patchUI(patch) {
  if (!cache.loaded) {
    // Try to load first; refuse the write if that still fails so we don't
    // clobber real server state with empty defaults.
    await loadPrefs();
    if (!cache.loaded) return false;
  }
  const previous = { ...cache.ui };
  cache.ui = { ...cache.ui, ...patch };  // eager local update
  const data = await apiCall("save prefs", "/api/prefs/ui", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!data) {
    cache.ui = previous;  // revert on failure
    return false;
  }
  cache.ui = data.ui;
  return true;
}

export function setSessionOrder(order) {
  return patchUI({ session_order: order });
}

export function setCollapsed(set) {
  return patchUI({ collapsed_sessions: [...set] });
}

export function setView(view) {
  return patchUI({ view });
}

export function setAlertsOpen(open) {
  return patchUI({ alerts_open: !!open });
}

// ── Window annotations ──────────────────────────────────────────────────

export function getAnnotation(pid) {
  if (!pid) return null;
  const entry = cache.windows[pid];
  if (!entry) return null;
  const notes = entry.notes || "";
  const tags = entry.tags || [];
  if (!notes && !tags.length) return null;
  return { notes, tags };
}

export function hasAnnotation(pid) {
  return getAnnotation(pid) !== null;
}

export async function setAnnotation(pid, { notes, tags }) {
  if (!cache.loaded) {
    await loadPrefs();
    if (!cache.loaded) return false;
  }
  const previous = cache.windows[pid];
  const entry = cache.windows[pid] || {};
  cache.windows[pid] = {
    ...entry,
    notes: notes ?? entry.notes,
    tags: tags ?? entry.tags,
  };
  const data = await apiCall("save annotation", `/api/prefs/windows/${encodeURIComponent(pid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes, tags }),
  });
  if (!data) {
    if (previous === undefined) delete cache.windows[pid];
    else cache.windows[pid] = previous;
    return false;
  }
  // Server returns the cleaned annotation (deduped tags, trimmed); use it.
  cache.windows[pid] = { ...(cache.windows[pid] || {}), ...data.annotation };
  return true;
}

export async function deleteAnnotation(pid) {
  if (!cache.loaded) return false;
  const previous = cache.windows[pid];
  if (cache.windows[pid]) {
    delete cache.windows[pid].notes;
    delete cache.windows[pid].tags;
  }
  const data = await apiCall("clear annotation", `/api/prefs/windows/${encodeURIComponent(pid)}`, {
    method: "DELETE",
  });
  if (!data) {
    cache.windows[pid] = previous;
    return false;
  }
  return true;
}

// ── One-time localStorage → server migration ─────────────────────────────

async function migrateLocalStorage() {
  const LEGACY = {
    "periscope:sessionOrder": "session_order",
    "periscope:collapsedSessions": "collapsed_sessions",
    "periscope:view": "view",
  };
  const haveAny = Object.keys(LEGACY).some(
    (k) => localStorage.getItem(k) !== null
  );
  if (!haveAny) return;

  const serverEmpty =
    !cache.ui.session_order &&
    !cache.ui.collapsed_sessions &&
    !cache.ui.view;
  if (serverEmpty) {
    const patch = {};
    for (const [k, field] of Object.entries(LEGACY)) {
      const raw = localStorage.getItem(k);
      if (raw === null) continue;
      try {
        // Asymmetry by design: state.js saved `view` as a bare string and
        // sessionOrder/collapsedSessions as JSON-encoded arrays. We mirror
        // that here so each value parses back to its original shape.
        patch[field] = field === "view" ? raw : JSON.parse(raw);
      } catch (_) {
        // unparseable legacy data — skip, the user can re-establish in UI
      }
    }
    if (Object.keys(patch).length) {
      const ok = await patchUI(patch);
      if (!ok) return;  // leave localStorage in place — try again next boot
    }
  }
  // Always delete legacy keys on a successful load. Once the server has
  // authoritative state the client copies are noise.
  for (const k of Object.keys(LEGACY)) localStorage.removeItem(k);
}

// --- Rail state (split view) -----------------------------------------------
// All five fields default to empty / null when the prefs blob hasn't seen
// them yet. Mutators write through the existing PATCH /api/prefs/ui endpoint.
// Note: `cache` is the module-private object declared at the top of this
// file; these getters read from it, the setters merge into it via patchUI().

export function getRepoOrder() {
  return [...(cache.ui?.repo_order || [])];
}

export function setRepoOrder(order) {
  return patchUI({ repo_order: order });
}

export function getWorktreesByRepo() {
  return { ...(cache.ui?.worktrees_by_repo || {}) };
}

export function setWorktreesByRepo(map) {
  return patchUI({ worktrees_by_repo: map });
}

export function getPanesByWorktree() {
  return { ...(cache.ui?.panes_by_worktree || {}) };
}

export function setPanesByWorktree(map) {
  return patchUI({ panes_by_worktree: map });
}

export function getRailCollapsed() {
  return { ...(cache.ui?.rail_collapsed || {}) };
}

export function setRailCollapsedKey(key, collapsed) {
  const next = getRailCollapsed();
  next[key] = collapsed;
  return patchUI({ rail_collapsed: next });
}

export function getLastSelected() {
  return cache.ui?.last_selected || null;
}

export function setLastSelected(sel) {
  return patchUI({ last_selected: sel });
}

// Add a worktree to the rail. If its repo isn't railed yet, append to
// repo_order. Idempotent — re-adding the same worktree is a no-op.
//
// Used by + project / + review PR / + open flows.
export async function addWorktreeToRail({ repoKey, worktreeKey, paneIds, hasReview }) {
  const order = getRepoOrder();
  const wts = getWorktreesByRepo();
  const panes = getPanesByWorktree();

  if (!order.includes(repoKey)) order.push(repoKey);
  const wtList = wts[repoKey] || [];
  if (!wtList.includes(worktreeKey)) wtList.push(worktreeKey);
  wts[repoKey] = wtList;

  if (!panes[worktreeKey]) {
    panes[worktreeKey] = [...paneIds];
    if (hasReview) panes[worktreeKey].push("review");
  }

  await patchUI({
    repo_order: order,
    worktrees_by_repo: wts,
    panes_by_worktree: panes,
  });
}

export async function removeWorktreeFromRail({ repoKey, worktreeKey }) {
  const wts = getWorktreesByRepo();
  const panes = getPanesByWorktree();
  const order = getRepoOrder();

  if (wts[repoKey]) {
    wts[repoKey] = wts[repoKey].filter(w => w !== worktreeKey);
    if (wts[repoKey].length === 0) {
      delete wts[repoKey];
      const idx = order.indexOf(repoKey);
      if (idx >= 0) order.splice(idx, 1);
    }
  }
  delete panes[worktreeKey];

  await patchUI({
    repo_order: order,
    worktrees_by_repo: wts,
    panes_by_worktree: panes,
  });
}
