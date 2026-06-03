// Pure transforms for the left-rail attention zone — no signals, no DOM.
// Mirrors railTree.js's posture (consumed by render, testable in isolation).
// This is the one unit-tested frontend module; keep it pure.

// Union of live needs-input panes + unacked need_human events, ordered
// live-first then events newest-first.
export function buildNeedsYou(windows, alertItems, dismissedIds) {
  const byTarget = indexByTarget(windows);
  const live = (windows || [])
    .filter((w) => w.state === "needs-input")
    .map((w) => ({ kind: "live", pid: w.pid, w }));
  // No reason field here: the human label is rendered in the component via
  // waitLabel(w.waiting_for). window_view.py forces asked_question=False for
  // mapped live sessions, so a flag-derived label would be dead in prod;
  // waiting_for carries the real distinction (incl. AskUserQuestion).
  const events = (alertItems || [])
    .filter((r) => r.kind === "need_human")
    .filter((r) => !dismissedIds.has(r.id))
    .filter((r) => !isAcked(r, byTarget))
    .sort((a, b) => b.ts - a.ts)
    .map((r) => ({
      kind: "event",
      id: r.id,
      target: r.target,
      w: byTarget[r.target] || null,
      message: r.message,
      ts: r.ts,
      session: r.session,
      name: r.name,
    }));
  return [...live, ...events];
}

// An event is acked once the user has engaged the pane after it fired:
// max(focused_at, acted_at) > event.ts. Missing window → not acked.
// Note: the payload's `acted_at` already folds in the persisted modal-open
// "acked_at" stamp (window_view.py), so opening the modal also acks — this is
// intentionally more generous than the spec's literal rule, matching the
// "however you got there" goal.
export function isAcked(event, windowByTarget) {
  const w = windowByTarget[event.target];
  if (!w) return false;
  return Math.max(w.focused_at || 0, w.acted_at || 0) > event.ts;
}

export function needsYouCount(needsYouRows) {
  return needsYouRows.length;
}

// Resolve the pinned-pid list against live windows, in pin order; dead ids
// dropped silently (render-time pruning — never persist-prune, or a
// transiently-absent pane loses its pin).
export function resolvePinned(pinnedPids, windows) {
  const byPid = {};
  for (const w of windows || []) byPid[w.pid] = w;
  return (pinnedPids || []).map((pid) => byPid[pid]).filter(Boolean);
}

// The low-signal Activity feed: everything that isn't need_human.
export function buildActivity(alertItems) {
  return (alertItems || []).filter(
    (r) => r.kind === "done" || r.kind === "info" || r.kind === "milestone"
  );
}

function indexByTarget(windows) {
  const m = {};
  for (const w of windows || []) m[w.target] = w;
  return m;
}
