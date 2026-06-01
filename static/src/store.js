// Transient read model — the single source of in-flight UI state for the
// Preact app. Components read these signals; the poll loop (Task 5) writes
// `windows`/`projects`/`usage`. Durable state lives in prefs.js (the signal
// there is the persistence boundary). Nothing here is persisted.
//
// These replace the ad-hoc mutable fields the vanilla app kept in state.js
// plus the poll-pause guards that lived as `state.editingTarget` /
// `state.dragging`.
import { signal } from "@preact/signals";

export const windows = signal([]);            // /api/state windows (poll-fed)
export const projects = signal([]);
export const currentFilter = signal("all");
export const view = signal("split");          // grid | split   (stream removed)
export const activeTarget = signal(null);     // modal/detail focused pane "session:index"
export const railSelection = signal(null);    // string highlight-key: "pane:<pid>" | "review:<worktree>" | null
export const dragState = signal(null);
export const usage = signal(null);

// poll-pause flags (replace state.editingTarget / state.dragging guards):
export const editingTarget = signal(null);
export const modalRenaming = signal(false);
export const modalAutoRenaming = signal(false);
