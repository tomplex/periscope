// Pure transforms for the left-rail attention zone — no signals, no DOM.
// Mirrors railTree.js's posture (consumed by render, testable in isolation).
// This is the one unit-tested frontend module; keep it pure.

// A live needs-input row is "soft" when it's only there because Claude's reply
// ended in a question (asked_question) with no actual blocking dialog open
// (waiting_for unset). These are the rows the user can click to dismiss; real
// dialogs (waiting_for set: permission / AskUserQuestion) stay sticky.
export function isSoftQuestion(w) {
  return !!w?.asked_question && !w?.waiting_for;
}

// Drop dismissed-pids whose pane is no longer in `state`, so a pane that
// re-enters the state later re-appears (dismissal is scoped to one episode).
// Used for both needs-input soft questions and Ready done-rows.
export function prunedStateDismissals(dismissedPids, windows, state) {
  const active = new Set(
    (windows || []).filter((w) => w.state === state).map((w) => w.pid)
  );
  const next = new Set();
  for (const pid of dismissedPids) if (active.has(pid)) next.add(pid);
  return next;
}

// Union of live needs-input panes + unacked need_human events, ordered
// live-first then events newest-first. `dismissedNeedsPids` hides live rows the
// user has clicked away (only soft-question rows are ever added — see the
// component); they reappear once the pane leaves needs-input (prune above).
export function buildNeedsYou(windows, alertItems, dismissedIds, dismissedNeedsPids = new Set()) {
  const byTarget = indexByTarget(windows);
  const live = (windows || [])
    .filter((w) => w.state === "needs-input")
    .filter((w) => !dismissedNeedsPids.has(w.pid))
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

// The green counterpart to buildNeedsYou: live "done" panes (Claude finished,
// unacked) + unacked done events, live-first then events newest-first. A done
// event whose target is already a live row is dropped — notify(done) fires at
// the same moment the pane's state flips, so the duplicate is the common case.
export function buildReady(windows, alertItems, dismissedIds, dismissedReadyPids = new Set()) {
  const byTarget = indexByTarget(windows);
  const live = (windows || [])
    .filter((w) => w.state === "done")
    .filter((w) => !dismissedReadyPids.has(w.pid))
    .map((w) => ({ kind: "live", pid: w.pid, w }));
  const liveTargets = new Set(live.map((r) => r.w.target));
  const events = (alertItems || [])
    .filter((r) => r.kind === "done")
    .filter((r) => !dismissedIds.has(r.id))
    .filter((r) => !liveTargets.has(r.target))
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

// How long an unanswered question has been waiting, as a severity tier. A
// question that has sat for an hour is a different event from one raised a
// minute ago, and rendering them identically is how a blocked pane stays
// blocked: over a 3-day run a spawned session's request for a scheduling
// decision was answered twice WITHOUT being read, because nothing about the
// display changed as it aged. Thresholds are deliberate constants, matching
// the memHint tiering idiom.
const WAIT_STALE_S = 600;    // 10 min — past a plausible "just stepped away"
const WAIT_URGENT_S = 3600;  // 1 hour — this pane has been parked, not paused

export function waitTier(sinceTs, nowS) {
  const age = (nowS || 0) - (sinceTs || 0);
  if (age >= WAIT_URGENT_S) return "urgent";
  if (age >= WAIT_STALE_S) return "stale";
  return "fresh";
}

// pid -> ts of the OLDEST unanswered need_human for that pane. Oldest, not
// newest: a pane that asked three times has been waiting since the first ask,
// and taking the newest would reset its age on every repeat — exactly the
// case that most needs escalating.
//
// Reuses the same predicate as the NEEDS YOU section (isAcked + dismissals) so
// the tree marker and the section can never disagree about who is waiting.
export function awaitingReplyByPid(windows, alertItems, dismissedIds = new Set()) {
  const byTarget = indexByTarget(windows);
  const out = new Map();
  for (const r of alertItems || []) {
    if (r.kind !== "need_human") continue;
    if (dismissedIds.has(r.id)) continue;
    if (isAcked(r, byTarget)) continue;
    const pid = byTarget[r.target]?.pid;
    if (!pid) continue;
    const prev = out.get(pid);
    if (prev === undefined || r.ts < prev) out.set(pid, r.ts);
  }
  return out;
}

// Live "working" panes, in /api/state order. A pure mirror of pane state —
// no events, no dismissals: rows appear and disappear with the spinner.
export function buildRunning(windows) {
  return (windows || [])
    .filter((w) => w.state === "working")
    .map((w) => ({ kind: "live", pid: w.pid, w }));
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
    (r) => r.kind === "done" || r.kind === "info"
  );
}

function indexByTarget(windows) {
  const m = {};
  for (const w of windows || []) m[w.target] = w;
  return m;
}
