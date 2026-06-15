// Cache of /api/prefs + mutators. The persistence boundary: Preact
// components read durable state through these getters (subscribing to
// `prefsSignal`) and mutate only via the mutators here, which POST + update
// the signal. See
// docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md.
//
// Ported from static/prefs.js. The ONLY structural change: the private
// `cache` object is now a signal (`prefsSignal`) so component reads are
// reactive. Every getter reads `prefsSignal.value`; every mutator replaces
// `prefsSignal.value` with a new object (eager optimistic update, revert on
// network failure). All invariants from the vanilla module are preserved
// verbatim: the `!loaded` write guard, optimistic-update-then-revert, the
// setAnnotation undefined-vs-object revert distinction, and the one-time
// localStorage→server migration at boot.

import { signal } from "@preact/signals";
import { apiCall } from "./util.js";

// The cache mirrors the server's state.json shape. `loaded` flips to true
// only after a successful loadPrefs(); mutators refuse to write while false.
const prefsSignal = signal({
  loaded: false,
  ui: {},
  windows: {},
  commands: [],
});

// Exported so other modules / debugging can subscribe to the raw blob.
export { prefsSignal };

// Reads `.value` once; helper to keep getters terse and consistent.
const P = () => prefsSignal.value;

export async function loadPrefs() {
  try {
    const res = await fetch("/api/prefs");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    prefsSignal.value = {
      ...prefsSignal.value,
      ui: data.ui || {},
      windows: data.windows || {},
      commands: data.commands || [],
      loaded: true,
    };
    await migrateLocalStorage();
    return prefsSignal.value;
  } catch (err) {
    prefsSignal.value = { ...prefsSignal.value, loaded: false };
    return null;
  }
}

// ── UI prefs ────────────────────────────────────────────────────────────

export function getCommands() {
  return P().commands || [];
}

export async function addCommand({ label, exec }) {
  if (!P().loaded) return false;
  const data = await apiCall("add command", "/api/prefs/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  prefsSignal.value = { ...P(), commands: data.commands };
  return true;
}

export async function updateCommand(oldLabel, { label, exec }) {
  if (!P().loaded) return false;
  const data = await apiCall("update command", `/api/prefs/commands/${encodeURIComponent(oldLabel)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  prefsSignal.value = { ...P(), commands: data.commands };
  return true;
}

export async function deleteCommand(label) {
  if (!P().loaded) return false;
  const data = await apiCall("delete command", `/api/prefs/commands/${encodeURIComponent(label)}`, {
    method: "DELETE",
  });
  if (!data) return false;
  prefsSignal.value = { ...P(), commands: data.commands };
  return true;
}

export async function reorderCommands(labels) {
  if (!P().loaded) return false;
  const data = await apiCall("reorder commands", "/api/prefs/commands", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ labels }),
  });
  if (!data) return false;
  prefsSignal.value = { ...P(), commands: data.commands };
  return true;
}

export async function patchUI(patch) {
  if (!P().loaded) {
    // Try to load first; refuse the write if that still fails so we don't
    // clobber real server state with empty defaults.
    await loadPrefs();
    if (!P().loaded) return false;
  }
  const previous = { ...P().ui };
  prefsSignal.value = { ...P(), ui: { ...P().ui, ...patch } };  // eager local update
  const data = await apiCall("save prefs", "/api/prefs/ui", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!data) {
    prefsSignal.value = { ...P(), ui: previous };  // revert on failure
    return false;
  }
  prefsSignal.value = { ...P(), ui: data.ui };
  return true;
}

// Write a server-authoritative ui blob into the cache WITHOUT re-POSTing —
// used by the open flow, where the server already persisted the pref.
export function setUI(uiBlob) {
  prefsSignal.value = { ...P(), ui: uiBlob };
}

// ── Window annotations ──────────────────────────────────────────────────

export function getAnnotation(pid) {
  if (!pid) return null;
  const entry = P().windows[pid];
  if (!entry) return null;
  const notes = entry.notes || "";
  const tags = entry.tags || [];
  const pinned_files = entry.pinned_files || [];
  if (!notes && !tags.length && !pinned_files.length) return null;
  return { notes, tags, pinned_files };
}

export async function setAnnotation(pid, { notes, tags, pinned_files }) {
  if (!P().loaded) {
    await loadPrefs();
    if (!P().loaded) return false;
  }
  const previous = P().windows[pid];
  const entry = previous || {};
  const optimistic = {
    ...entry,
    notes: notes ?? entry.notes,
    tags: tags ?? entry.tags,
    pinned_files: pinned_files ?? entry.pinned_files,
  };
  prefsSignal.value = { ...P(), windows: { ...P().windows, [pid]: optimistic } };
  // exclude_none on the wire so server treats an omitted field as "no change",
  // matching how undefined args are handled here on the client.
  const body = {};
  if (notes !== undefined) body.notes = notes;
  if (tags !== undefined) body.tags = tags;
  if (pinned_files !== undefined) body.pinned_files = pinned_files;
  const data = await apiCall("save annotation", `/api/prefs/windows/${encodeURIComponent(pid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!data) {
    // Revert distinction: a brand-new entry (previous === undefined) must be
    // deleted, not restored to {} — restoring an empty object would leave a
    // phantom annotation key. A pre-existing entry is restored as-is.
    const reverted = { ...P().windows };
    if (previous === undefined) delete reverted[pid];
    else reverted[pid] = previous;
    prefsSignal.value = { ...P(), windows: reverted };
    return false;
  }
  // Server returns the cleaned annotation (deduped, trimmed); use it.
  prefsSignal.value = {
    ...P(),
    windows: { ...P().windows, [pid]: { ...(P().windows[pid] || {}), ...data.annotation } },
  };
  return true;
}

// ── Per-pane pinned files (Inspector's Files section) ─────────────────────
// Pins are stored as `pinned_files` on the window annotation blob. The order
// in the array IS the display order in the Pinned group (insertion order).

export function getPinnedFiles(pid) {
  if (!pid) return [];
  const entry = P().windows[pid];
  return [...(entry?.pinned_files || [])];
}

export function togglePinnedFile(pid, path) {
  if (!pid || !path) return Promise.resolve(false);
  const cur = getPinnedFiles(pid);
  const next = cur.includes(path) ? cur.filter((p) => p !== path) : [...cur, path];
  return setAnnotation(pid, { pinned_files: next });
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

  const ui = P().ui;
  const serverEmpty =
    !ui.session_order &&
    !ui.collapsed_sessions &&
    !ui.view;
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

export function getRepoOrder() {
  return [...(P().ui?.repo_order || [])];
}

export function setRepoOrder(order) {
  return patchUI({ repo_order: order });
}

export function getWorktreesByRepo() {
  return { ...(P().ui?.worktrees_by_repo || {}) };
}

export function setWorktreesByRepo(map) {
  return patchUI({ worktrees_by_repo: map });
}

export function getPanesByWorktree() {
  return { ...(P().ui?.panes_by_worktree || {}) };
}

export function setPanesByWorktree(map) {
  return patchUI({ panes_by_worktree: map });
}

export function getRailCollapsed() {
  return { ...(P().ui?.rail_collapsed || {}) };
}

export function setRailCollapsedKey(key, collapsed) {
  const next = getRailCollapsed();
  next[key] = collapsed;
  return patchUI({ rail_collapsed: next });
}

export function getLastSelected() {
  return P().ui?.last_selected || null;
}

export function setLastSelected(sel) {
  return patchUI({ last_selected: sel });
}

export function getPinnedPids() {
  return [...(P().ui?.pinned_pids || [])];
}
export function setPinnedPids(list) {
  return patchUI({ pinned_pids: list });
}
export function togglePin(pid) {
  const cur = getPinnedPids();
  const next = cur.includes(pid) ? cur.filter((p) => p !== pid) : [...cur, pid];
  return setPinnedPids(next);
}

// Per-pane detail-mode toggle (split view). Reactive via prefsSignal — read
// directly in components for live updates. Map shape: { [pid]: "terminal" | "transcript" }.
export function getDetailMode(pid) {
  return P().ui?.detail_mode_by_pid?.[pid] || null;
}

export function setDetailMode(pid, mode) {
  const next = { ...(P().ui?.detail_mode_by_pid || {}) };
  next[pid] = mode;
  return patchUI({ detail_mode_by_pid: next });
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
