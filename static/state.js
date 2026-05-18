// Cross-module mutable state. Persistence now lives in prefs.js — this module
// only holds in-flight UI state that doesn't survive a reload.

export const state = {
  // grid
  currentFilter: "all",
  lastWindows: [],
  lastProjects: [],              // from /api/state — non-archived rows only
  projectsByTmux: {},             // indexProjects(lastProjects), rebuilt each render
  editingTarget: null,           // pauses polling while a card rename input is open
  collapsedSessions: new Set(),  // hydrated from prefs.getCollapsed() at boot

  // modal
  activeTarget: null,
  modalRenaming: false,          // pauses modal header refresh during inline rename
};
