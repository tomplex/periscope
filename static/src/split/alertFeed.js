// The cross-pane alert feed: owns the native-notify/dock-badge side effects
// and exposes `alertItems` as the read model. The list itself arrives on the
// pushed /api/state blob (store.alerts) rather than a poll of its own, so it
// shares the state hub's transport and its notify()-driven kick — an alert
// lands as soon as the tool call that raised it returns. Non-component
// (mirrors poll.js → windows) so the badge stays fresh and native-notify
// fires regardless of what's rendered. Started once from Split.jsx.
import { effect } from "@preact/signals";
import * as prefs from "../prefs.js";
import { alerts, railSelection, windows } from "../store.js";
import { inTauri, notify, onNotificationClick, setBadgeCount } from "../tauri.js";

// The pushed feed IS the read model — re-exported so consumers keep importing
// alerts from the module that owns their semantics.
export const alertItems = alerts;

let seenAlertKeys = null;       // first-load sentinel — see maybeNativeNotify
let started = false;

// Reveal a pane from an alert/native-notification click via inline rail
// selection. No-op when the pane isn't in the live window list (closed
// since the alert fired).
export function revealPane(target) {
  if (!target) return;
  const w = (windows.value || []).find((x) => x.target === target);
  if (!w?.pid) return;
  railSelection.value = `pane:${w.pid}`;
  prefs.setLastSelected({ kind: "pane", pid: w.pid });
}

function maybeNativeNotify(list) {
  if (!inTauri()) return;
  const needHuman = list.filter((r) => r.kind === "need_human");
  if (seenAlertKeys === null) {
    seenAlertKeys = new Set(needHuman.map((r) => r.id));
    return;
  }
  for (const r of needHuman) {
    if (seenAlertKeys.has(r.id)) continue;
    seenAlertKeys.add(r.id);
    const paneLabel = `${r.session} · ${r.name || `:${r.index}`}`;
    notify({ title: `⚠ ${paneLabel}`, body: r.message || "", target: r.target });
  }
  const current = new Set(needHuman.map((r) => r.id));
  for (const k of seenAlertKeys) if (!current.has(k)) seenAlertKeys.delete(k);
}

export function startAlertFeed() {
  if (started) return;
  started = true;
  onNotificationClick((target) => revealPane(target));
  // Side effects only — the list is written by poll.js. `null` is the
  // not-yet-loaded value: skipping it keeps the sentinel below honest, and
  // transport failure is already surfaced by poll.js's connection banner, so
  // the stale list simply stays on screen.
  effect(() => {
    const list = alerts.value;
    if (list === null) return;
    maybeNativeNotify(list);
    setBadgeCount(list.filter((r) => r.kind === "need_human").length);
  });
}
