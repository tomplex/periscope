// Cross-module mutable state. Persistence now lives in prefs.js — this module
// only holds in-flight UI state that doesn't survive a reload.

export const state = {
  // grid
  currentFilter: "all",
  lastWindows: [],
  lastProjects: [],              // from /api/state — non-archived rows only
  projectsByTmux: {},             // indexProjects(lastProjects), rebuilt each render
  editingTarget: null,           // pauses polling while a card rename input is open
  dragging: false,               // pauses polling mid-drag so the re-render doesn't kill the drag
  collapsedSessions: new Set(),  // hydrated from prefs.getCollapsed() at boot

  // stream view
  streamQuery: "",               // type-to-filter substring (matches name + session)
  streamFocusedTarget: null,     // target ("sess:N") of the keyboard-focused row
  streamVisible: [],             // last rendered order of {target, session} for ↑/↓ nav

  // modal
  activeTarget: null,
  modalRenaming: false,          // pauses modal header refresh during inline rename

  // rail (split view)
  railDragging: null,             // { kind: "repo"|"worktree"|"child", key: string }
  railSelected: null,             // mirror of prefs.last_selected for fast read
};
