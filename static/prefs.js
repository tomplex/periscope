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

let lastError = "";

export function isLoaded() {
  return cache.loaded;
}

export function lastLoadError() {
  return lastError;
}

export async function loadPrefs() {
  try {
    const res = await fetch("/api/prefs");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cache.ui = data.ui || {};
    cache.windows = data.windows || {};
    cache.commands = data.commands || [];
    cache.loaded = true;
    lastError = "";
    await migrateLocalStorage();
    return cache;
  } catch (err) {
    cache.loaded = false;
    lastError = err.message || String(err);
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
  return cache.ui.view === "stream" ? "stream" : "grid";
}

async function patchUI(patch) {
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
