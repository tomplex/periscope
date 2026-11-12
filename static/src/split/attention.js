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
      pane_id: r.pane_id,
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
      pane_id: r.pane_id,
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
// Reuses the same predicate as buildNeedsYou (isAcked + dismissals) so a row's
// blip and its Next card can never disagree about who is waiting.
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

// Panes waiting on you, one entry each: a pane can have both a live
// needs-input row and an unacked need_human event (Claude notifies while its
// pane sits waiting), and the rail lists both. A pane waits since the earlier
// of the server's needs-input stamp (needs_since, which survives a reload) and
// its oldest unanswered event, the rail's own urgency tier
// (awaitingReplyByPid). Next's asks (and through them Clawd's bell) and the
// dock badge all count these, so none can disagree with the rail. Map of
// key -> { row, since, message }; a live row wins over an event row for the
// same pane, and `message` is the newest unanswered need_human's: the agent's
// own words, when it gave any.
export function waitingPanes(windows, alertItems, dismissedIds = new Set(), dismissedNeedsPids = new Set(), nowSec = 0) {
  const asked = awaitingReplyByPid(windows, alertItems, dismissedIds);
  const waiting = new Map();
  for (const r of buildNeedsYou(windows, alertItems, dismissedIds, dismissedNeedsPids)) {
    const key = r.w?.pid != null ? `pid:${r.w.pid}` : `target:${r.target}`;
    const since = Math.min(
      r.kind === "event" ? r.ts : (r.w.needs_since || nowSec),
      ...(r.w?.pid != null && asked.has(r.w.pid) ? [asked.get(r.w.pid)] : []),
    );
    const prev = waiting.get(key);   // events arrive newest-first
    waiting.set(key, {
      row: prev?.row.kind === "live" ? prev.row : r,
      since: prev ? Math.min(prev.since, since) : since,
      message: prev?.message || (r.kind === "event" ? r.message : null) || null,
    });
  }
  return waiting;
}

// One PR, however many panes show it.
export const prKey = (w) => `${w.repo_slug || w.repo_key || ""}#${w.pr}`;

// An open PR whose checks are red. A merged or closed PR keeps its last ✗, but
// nothing is left to fix. Clawd's CI duty asks the same question.
export function isCiFailing(w) {
  return w?.ci === "✗" && !!w.pr && (!w.pr_state || w.pr_state === "open");
}

// Why a pane is broken, or null: "api" (an API error) or "ci" (its open PR's
// checks are red). The blip, its words (notify/copy.js blipReason), Next's one
// card per PR and Clawd all ask this.
export function brokenCause(w) {
  if (w.api_error) return "api";
  if (isCiFailing(w)) return "ci";
  return null;
}

// A finished pane is news for an hour. After that its blip dims, though it
// stays in Next until it's seen.
const DONE_FRESH_S = 3600;

// What a pane's blip says (DESIGN.md's blips), one word per pane: "asks",
// "working", "broken", "done", "done-old", "idle" or "shell". `asks` is
// whether Next lists the pane as asking (waitingPanes): a live question, less
// a soft one you've opened, or an unanswered need_human, which asks even once
// the pane has gone idle. A spinner beats broken: an agent retrying after an
// API error, or fixing its red CI, doesn't need you yet.
export function paneBlip(w, nowS, asks = false) {
  if (!w.agent) return "shell";
  if (asks) return "asks";
  if (w.state === "working") return "working";
  if (brokenCause(w)) return "broken";
  if (w.state === "done") return nowS - (w.completed_at || 0) < DONE_FRESH_S ? "done" : "done-old";
  return "idle";
}

// What wants you, one entry per pane, in the order ⌘J walks it: panes that
// ask (longest wait first), then broken ones, then buildReady's finished ones.
// Entry: { key, kind: "asks" | "broken" | "done", w, row, since, message }.
// `row` is the buildNeedsYou or buildReady row behind an asks or done entry
// (null for broken), which says how opening it behaves
// (alertFeed.openNextEntry). `w` is null for an event whose pane has closed.
// `message` is an asking pane's newest need_human (waitingPanes). A PR failing
// CI is one entry however many panes show it, as Clawd counts it; its other
// panes still show broken in the strip and the tree.
export function buildNext(windows, alertItems, dismissedIds = new Set(), dismissedNeedsPids = new Set(),
  dismissedReadyPids = new Set(), nowS = 0) {
  const asks = [...waitingPanes(windows, alertItems, dismissedIds, dismissedNeedsPids, nowS)]
    .sort((a, b) => a[1].since - b[1].since)
    .map(([key, { row, since, message }]) => ({ key: `asks:${key}`, kind: "asks", w: row.w, row, since, message }));
  const asking = new Set(asks.map((e) => e.w?.pid).filter(Boolean));
  const brokenPids = new Set();
  const failingPrs = new Set();
  const broken = [];
  for (const w of windows || []) {
    if (paneBlip(w, nowS, asking.has(w.pid)) !== "broken") continue;
    brokenPids.add(w.pid);
    if (brokenCause(w) === "ci") {
      if (failingPrs.has(prKey(w))) continue;
      failingPrs.add(prKey(w));
    }
    broken.push({ key: `broken:${w.pid}`, kind: "broken", w, row: null, since: null, message: null });
  }
  const taken = new Set([...asking, ...brokenPids]);
  const done = buildReady(windows, alertItems, dismissedIds, dismissedReadyPids)
    .filter((r) => !taken.has(r.w?.pid))
    .map((r) => ({
      key: r.kind === "live" ? `done:${r.pid}` : `done:${r.id}`, kind: "done", w: r.w, row: r,
      since: r.kind === "live" ? r.w.completed_at : r.ts,
      message: r.kind === "event" ? r.message : null,
    }));
  return [...asks, ...broken, ...done];
}

// The entry ⌘J opens: the first one this walk hasn't opened (`opened`, a set
// of entry keys), skipping the pane already on screen. Opening a done card or
// a soft question removes it, so walking by position would fall back to the
// top on every press. Once every entry has been opened, the walk starts over.
export function nextToOpen(entries, selectedPid, opened) {
  const elsewhere = entries.filter((e) => !e.w || String(e.w.pid) !== String(selectedPid));
  return elsewhere.find((e) => !opened.has(e.key)) || elsewhere[0] || null;
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
