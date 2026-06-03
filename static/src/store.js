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
export const activeTarget = signal(null);     // detail-pane focused pane "session:index" (split); shared paste/poll target
export const modalTarget = signal(null);      // the modal's OWN open-state: the pane it shows, or null when closed. Kept distinct from activeTarget so split-view inline selection (which sets activeTarget) never pops the modal.
export const railSelection = signal(null);    // string highlight-key: "pane:<pid>" | "review:<worktree>" | null
export const dragState = signal(null);
export const usage = signal(null);

// poll-pause flags (replace state.editingTarget / state.dragging guards):
export const editingTarget = signal(null);
export const modalRenaming = signal(false);
export const modalAutoRenaming = signal(false);

// Transcript-seen flag (split-view detail). Set once a Claude pane's first
// poll returns real turns. Currently consumed by Sidebar's FilesSection as a
// "transcript data exists for this pid" gate. The detail-mode toggle itself
// (Transcript/Terminal) is persisted via UI prefs (detail_mode_by_pid).
export const transcriptSeen = signal({});   // { [pid]: true }

// Shared transcript messages — written by useTranscriptPoll in
// Transcript.jsx (the kept-mounted instance for each opened Claude pid),
// read by both TranscriptView (own messages) and Sidebar's Files section
// (selected pane's messages). One poll per selected pid; no duplicate
// fetches. Evicted alongside transcript-host pruning in Detail.jsx.
export const paneTranscript = signal({});   // { [pid]: { messages, sessionId } }

// File-preview TABS (browser-style). Each pane has its own set of open
// file tabs and a currently-active tab. The Pane's own terminal/transcript
// is always the implicit first tab (key "pane"); file tabs are keyed by
// "file:<path>". Setters all go through openFileTab below — keeps the
// add-or-focus + activate behavior identical across callers.
export const paneTabs = signal({});         // { [pid]: [{ path, line, target }, ...] }
export const paneActiveTab = signal({});    // { [pid]: "pane" | "file:<path>" }

// Add (or focus, if already open) a file tab for the currently-active
// pane. Callers are: terminal Cmd+click (Detail.jsx), Sidebar Files row,
// Transcript tool-call chip Cmd+click. All share activeTarget — the
// per-callsite pid lookup is done here so the call sites stay terse.
export function openFileTab(entry) {
  const tgt = activeTarget.value;
  if (!tgt) return;
  const w = (windows.value || []).find((x) => x.target === tgt);
  if (!w) return;
  const pid = w.pid;
  const tabs = paneTabs.value[pid] || [];
  const has = tabs.some((t) => t.path === entry.path);
  if (!has) {
    paneTabs.value = { ...paneTabs.value, [pid]: [...tabs, { ...entry, target: tgt }] };
  }
  paneActiveTab.value = { ...paneActiveTab.value, [pid]: `file:${entry.path}` };
}

export function closeFileTab(pid, path) {
  const tabs = paneTabs.value[pid] || [];
  const next = tabs.filter((t) => t.path !== path);
  paneTabs.value = { ...paneTabs.value, [pid]: next };
  if (paneActiveTab.value[pid] === `file:${path}`) {
    paneActiveTab.value = { ...paneActiveTab.value, [pid]: "pane" };
  }
}

export function setActiveTab(pid, tabKey) {
  paneActiveTab.value = { ...paneActiveTab.value, [pid]: tabKey };
}

// Dismissed need_human alert ids (transient — resets on restart, the feed is
// in-memory anyway). The Needs-you section filters these out.
export const dismissedAlertIds = signal(new Set());
