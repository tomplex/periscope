// The cross-pane alert feed: exposes `alertItems` as the read model and the
// row behaviours the rail and Clawd share. The list itself arrives on the
// pushed /api/state blob (store.alerts) rather than a poll of its own, so it
// shares the state hub's transport and its notify()-driven kick — an alert
// lands as soon as the tool call that raised it returns. Native banners and
// the dock badge are notify/feed.js's. Started once from Split.jsx.
import * as prefs from "../prefs.js";
import { alerts, dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, railSelection, windows } from "../store.js";
import { onNotificationClick } from "../tauri.js";
import { isSoftQuestion } from "./attention.js";

// The pushed feed IS the read model — re-exported so consumers keep importing
// alerts from the module that owns their semantics.
export const alertItems = alerts;

let started = false;

// Reveal a pane from an alert/native-notification click via inline rail
// selection. No-op when the pane isn't in the live window list (closed
// since the alert fired).
export function revealPane(target) {
  if (!target) return;
  selectPane((windows.value || []).find((x) => x.target === target));
}

// Select a live window inline in the rail, as clicking its row does.
export function selectPane(w) {
  if (!w?.pid) return;
  railSelection.value = `pane:${w.pid}`;
  prefs.setLastSelected({ kind: "pane", pid: w.pid });
}

// What opening a pane that asks or finished does, shared by the rail's Next
// cards and the header's Clawd so the two can't drift. A soft
// question (no real dialog) is dismissed for this episode; a real dialog stays
// sticky. A done row is always dismissed: selecting normally acks it through
// the terminal's WS connect, but an already-selected pane won't reconnect.
export function openNeedsPane(w) {
  if (isSoftQuestion(w)) dismissedNeedsPids.value = new Set([...dismissedNeedsPids.value, w.pid]);
  selectPane(w);
}

// Dismiss a READY row without opening its pane: a done banner's Mark seen, or
// a done card's ×. Its notify(done) events are the same news: READY hides
// them only while the live row shows, so they'd take its place.
export function markReadySeen(w) {
  if (!w?.pid) return;
  dismissedReadyPids.value = new Set([...dismissedReadyPids.value, w.pid]);
  dismissEvents("done", w.target);
}

// Dismiss every unacked `kind` event a pane has raised: an asking card's ×
// covers all its questions, not just the one on the card.
export function dismissEvents(kind, target) {
  const ids = (alerts.value || []).filter((r) => r.kind === kind && r.target === target).map((r) => r.id);
  if (ids.length) dismissedAlertIds.value = new Set([...dismissedAlertIds.value, ...ids]);
}

export function openReadyPane(w) {
  markReadySeen(w);
  selectPane(w);
}

// What opening a Next entry (attention.buildNext) does: a live question or
// finished pane opens as above, an event reveals its pane, and a broken pane
// is selected.
export function openNextEntry(e) {
  if (e.kind === "broken") selectPane(e.w);
  else if (e.row.kind === "event") revealPane(e.row.target);
  else if (e.kind === "asks") openNeedsPane(e.w);
  else openReadyPane(e.w);
}

export function startAlertFeed() {
  if (started) return;
  started = true;
  // An older shell's banner click carries only the pane target (a newer one
  // sends kinds to notify/feed.js instead).
  onNotificationClick((target) => revealPane(target));
}

// Select a branch's review row / the Reviews queue, as clicking them does.
// The persisted pref carries repo + branch (the rail key's slug can't be
// inverted: `--` is ambiguous).
export function selectReview(repo, branch) {
  if (!repo || !branch) return;
  railSelection.value = `review:${repo}/${branch.replace(/\//g, "--")}`;
  prefs.setLastSelected({ kind: "review", repo, branch });
}

export function selectReviewsQueue() {
  railSelection.value = "reviews";
  prefs.setLastSelected({ kind: "reviews" });
}
