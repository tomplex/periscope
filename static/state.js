// Cross-module mutable state and localStorage persistence.
//
// Only fields read across modules live in the shared `state` object. State
// that is genuinely internal to one subsystem (xterm handles, debounce timers,
// click-debounce maps) stays as module locals in the owning module.

const ORDER_KEY = "periscope:sessionOrder";
const COLLAPSED_KEY = "periscope:collapsedSessions";

// One-time migration from the pre-rename keys. Reads the old value if present
// and the new key is empty, then deletes the old key. Safe to leave in place;
// after one load it's a no-op.
function migrateOldKey(oldK, newK) {
  const v = localStorage.getItem(oldK);
  if (v !== null && localStorage.getItem(newK) === null) {
    localStorage.setItem(newK, v);
  }
  if (v !== null) localStorage.removeItem(oldK);
}
migrateOldKey("work-dashboard:sessionOrder", ORDER_KEY);
migrateOldKey("work-dashboard:collapsedSessions", COLLAPSED_KEY);

export function loadOrder() {
  try { return JSON.parse(localStorage.getItem(ORDER_KEY)) || []; }
  catch { return []; }
}
export function saveOrder(order) {
  localStorage.setItem(ORDER_KEY, JSON.stringify(order));
}
export function loadCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY)) || []); }
  catch { return new Set(); }
}
export function saveCollapsed(set) {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
}

export const state = {
  // grid
  currentFilter: "all",
  lastWindows: [],
  editingTarget: null,           // pauses polling while a card rename input is open
  collapsedSessions: loadCollapsed(),

  // modal
  activeTarget: null,
  modalRenaming: false,          // pauses modal header refresh during inline rename
};
