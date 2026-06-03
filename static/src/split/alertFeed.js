// The cross-pane alert feed: owns the /api/alerts/recent poll loop and the
// native-notify/dock-badge side effects, exposing `alertItems` as the read
// model. Non-component (mirrors grid/poll.js → windows) so the badge stays
// fresh and native-notify fires regardless of what's rendered. Started once
// from Split.jsx. Lifted from the former overlays/Alerts.jsx.
import { signal } from "@preact/signals";
import { showToast } from "../overlays/Toast.jsx";
import { setBadgeCount, notify, onNotificationClick, inTauri } from "../tauri.js";
import { view, windows, railSelection } from "../store.js";
import * as prefs from "../prefs.js";
import { openModal } from "../modal/Modal.jsx";

const POLL_MS = 3000;

export const alertItems = signal([]);

let pollFailed = false;
let seenAlertKeys = null;       // first-poll sentinel — see maybeNativeNotify
let started = false;

// Reveal a pane from an alert/native-notification click. Split → inline select;
// else modal fallback. (Moved verbatim from Alerts.jsx.)
export function revealPane(target) {
  if (!target) return;
  if (view.value === "split") {
    const w = (windows.value || []).find((x) => x.target === target);
    if (w?.pid) {
      railSelection.value = `pane:${w.pid}`;
      prefs.setLastSelected({ kind: "pane", pid: w.pid });
      return;
    }
  }
  openModal(target);
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

async function poll() {
  try {
    const res = await fetch("/api/alerts/recent?limit=100");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (pollFailed) { pollFailed = false; showToast("notifications feed reconnected", "good"); }
    const list = data.items || [];
    maybeNativeNotify(list);
    setBadgeCount(list.filter((r) => r.kind === "need_human").length);
    alertItems.value = list;
  } catch (e) {
    if (!pollFailed) { pollFailed = true; showToast(`notifications feed unavailable: ${e.message}`, "bad"); }
  }
}

export function startAlertFeed() {
  if (started) return;
  started = true;
  onNotificationClick((target) => revealPane(target));
  poll();
  setInterval(poll, POLL_MS);
}
