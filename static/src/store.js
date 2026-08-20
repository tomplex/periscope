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
export const workspaces = signal([]);  // /api/state workspaces (poll-fed)
export const tracks = signal([]);      // /api/state track registry rows (poll-fed) — includes EMPTY goal tracks
// Cross-pane alert feed, fed by /api/state. `null` means "not loaded yet" and
// is distinct from `[]` ("loaded, no alerts"): alertFeed's native-notify
// sentinel must not treat the pre-load value as a real empty feed, or every
// alert already present at boot fires a desktop notification.
export const alerts = signal(null);
export const currentFilter = signal("all");
export const activeTarget = signal(null);     // detail-pane focused pane "session:index" (split); shared paste/poll target
export const railSelection = signal(null);    // string highlight-key: "pane:<pid>" | "review:<worktree>" | null
export const dragState = signal(null);
export const usage = signal(null);
export const updateInfo = signal(null);       // /api/state update summary: { behind, checked_at, running }
export const spawnAccount = signal(null);     // pinned spawn account id, null = auto (most headroom)
export const editor = signal("");              // /api/state preferred editor display name; "" => action hidden

// poll-pause flags (replace state.editingTarget / state.dragging guards):
export const editingTarget = signal(null);

// Transcript-seen flag (split-view detail). Set once a Claude pane's first
// poll returns real turns. Currently consumed by Inspector's FilesSection as a
// "transcript data exists for this pid" gate. The detail-mode toggle itself
// (Transcript/Terminal) is persisted via UI prefs (detail_mode_by_pid).
export const transcriptSeen = signal({});   // { [pid]: true }

// Shared transcript messages — written by useTranscriptPoll in
// Transcript.jsx (the kept-mounted instance for each opened Claude pid),
// read by both TranscriptView (own messages) and Inspector's Files section
// (selected pane's messages). One poll per selected pid; no duplicate
// fetches. Evicted alongside transcript-host pruning in Detail.jsx.
export const paneTranscript = signal({});   // { [pid]: { messages, sessionId } }

// File-preview TABS (browser-style). Each pane has its own set of open
// file tabs and a currently-active tab. The Pane's own terminal/transcript
// is always the implicit first tab (key "pane"); file tabs are keyed by
// "file:<path>". The tab state is SERVER-OWNED (persisted per-pid in
// state.json, surfaced as `open_tabs` / `active_tab` on /api/state
// windows): these signals are the local read model, hydrated by
// syncTabsFromWindows each poll. User actions update them optimistically
// and POST the mutation; the open_document MCP tool writes server state
// directly and lands here on the next poll.
export const paneTabs = signal({});         // { [pid]: [{ path, line, target }, ...] }
export const paneActiveTab = signal({});    // { [pid]: "pane" | "file:<path>" }
// Bumped by ⌘R to force the visible preview tab to re-read from disk NOW,
// without waiting for the mtime poll's next tick. A single global counter, not
// a per-tab key: only the visible tab fetches, so a broadcast costs one read.
export const docRefreshNonce = signal(0);

// Timestamp of the last optimistic tab mutation. Hydration skips one poll
// period after a mutation so an /api/state response that was already
// in flight when the user clicked can't briefly revert the optimistic
// update before the POST's effect is polled back.
let lastTabMutation = 0;

export function syncTabsFromWindows(ws) {
  if (Date.now() - lastTabMutation < 3000) return;
  const tabs = {};
  const active = {};
  for (const w of ws) {
    if (!w.pid) continue;
    const list = w.open_tabs || [];
    if (list.length) {
      tabs[w.pid] = list.map((t) => ({ ...t, target: w.target }));
    }
    if (w.active_tab && w.active_tab !== "pane") active[w.pid] = w.active_tab;
  }
  paneTabs.value = tabs;
  paneActiveTab.value = active;
}

function postTabMutation(action, body) {
  lastTabMutation = Date.now();
  fetch(`/api/pane/tabs/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {});
}

// Add (or focus, if already open) a file tab for the currently-active
// pane. Callers are: terminal Cmd+click (Detail.jsx), Inspector Files row,
// Transcript tool-call chip Cmd+click. All share activeTarget — the
// per-callsite pid lookup is done here so the call sites stay terse.
export function openFileTab(entry) {
  const tgt = activeTarget.value;
  if (!tgt) return;
  const w = (windows.value || []).find((x) => x.target === tgt);
  if (!w) return;
  const pid = w.pid;
  const tabs = paneTabs.value[pid] || [];
  const existing = tabs.find((t) => t.path === entry.path);
  if (!existing) {
    paneTabs.value = { ...paneTabs.value, [pid]: [...tabs, { ...entry, target: tgt }] };
  } else if (entry.line && entry.line !== existing.line) {
    // Re-opening an open file at a DIFFERENT line (clicking a second hunk in
    // the Changes tab) must move the jump target, or the click looks dead.
    paneTabs.value = {
      ...paneTabs.value,
      [pid]: tabs.map((t) => (t.path === entry.path ? { ...t, line: entry.line } : t)),
    };
  }
  paneActiveTab.value = { ...paneActiveTab.value, [pid]: `file:${entry.path}` };
  postTabMutation("open", { pid, path: entry.path, line: entry.line ?? null });
}

export function closeFileTab(pid, path) {
  const tabs = paneTabs.value[pid] || [];
  const next = tabs.filter((t) => t.path !== path);
  paneTabs.value = { ...paneTabs.value, [pid]: next };
  if (paneActiveTab.value[pid] === `file:${path}`) {
    paneActiveTab.value = { ...paneActiveTab.value, [pid]: "pane" };
  }
  postTabMutation("close", { pid, path });
}

export function setActiveTab(pid, tabKey) {
  paneActiveTab.value = { ...paneActiveTab.value, [pid]: tabKey };
  postTabMutation("activate", { pid, tab: tabKey });
}

// Dismissed need_human alert ids (transient — resets on restart, the feed is
// in-memory anyway). The Needs-you section filters these out.
export const dismissedAlertIds = signal(new Set());

// Soft-question live rows (Claude's reply ended in "?") the user clicked away.
// Keyed by pid; pruned when the pane leaves needs-input so a fresh question
// re-surfaces. Transient. The Needs-you section filters these out.
export const dismissedNeedsPids = signal(new Set());

// Live "done" rows the user clicked away in the Ready section. Keyed by pid;
// pruned when the pane leaves "done" so the next completion re-surfaces.
// Needed because clicking an already-selected pane doesn't re-open the WS, so
// nothing else would bump acked_at and clear the row. Transient.
export const dismissedReadyPids = signal(new Set());
