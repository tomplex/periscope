// The left-rail attention zone. Two mount points (see <Rail>):
//   <AttentionTop>    — NEEDS YOU + PINNED, rendered ABOVE the project tree.
//   <ActivitySection> — the low-signal ACTIVITY log, rendered BELOW the tree.
// Reads the windows + alertItems signals through the pure transforms in
// attention.js. Out-of-tree rows use `attn-row` classes (NOT child-row) so they
// never enter the tree's connector-adjacency CSS.
import { useEffect } from "preact/hooks";
import { windows, dismissedAlertIds, dismissedNeedsPids, railSelection } from "../store.js";
import { alertItems, revealPane } from "./alertFeed.js";
import * as prefs from "../prefs.js";
import { relTime, waitLabel, shortestUniqueSuffix } from "../util.js";
import { SectionHeader } from "./SectionHeader.jsx";
import {
  buildNeedsYou, needsYouCount, resolvePinned, buildActivity,
  isSoftQuestion, prunedNeedsDismissals,
} from "./attention.js";
import { statusDotClass } from "./RailRows.jsx";

function paneLabel(w) {
  return w?.name || (w?.is_claude ? "claude" : "shell");
}
// `shorten` collapses a long slash-path session to its shortest unique suffix.
function originLabel(w, fallbackSession, fallbackName, shorten) {
  const session = w?.session || fallbackSession || "";
  const name = paneLabel(w) || fallbackName || "";
  return `${shorten ? shorten(session) : session} · ${name}`;
}
// Suffix-shortener over every live session name, so a label only grows a
// segment when it would actually collide with another open session.
function sessionShortener(live) {
  const all = [...new Set((live || []).map((w) => w.session).filter(Boolean))];
  return (s) => shortestUniqueSuffix(s, all);
}

// Mirror the rail's own selection write (string highlight signal + object pref).
// Static import of railSelection is safe — store.js imports nothing from here.
function selectPane(w) {
  if (!w?.pid) return;
  railSelection.value = `pane:${w.pid}`;
  prefs.setLastSelected({ kind: "pane", pid: w.pid });
}

// Toggle takes the section's already-computed current state so the inverted
// Activity default lives at exactly one place (the read site), not here.
function toggle(key, currentlyCollapsed) {
  prefs.setRailCollapsedKey(key, !currentlyCollapsed);
}

// NEEDS YOU + PINNED — top of the rail, above the project tree.
export function AttentionTop() {
  // Subscribe to prefs explicitly (pins + section-collapse live there), the
  // same way Rail does — so reactivity is self-contained.
  prefs.prefsSignal.value;
  const live = windows.value || [];
  const items = alertItems.value || [];
  const dismissed = dismissedAlertIds.value;
  const dismissedNeeds = dismissedNeedsPids.value;
  const collapsed = prefs.getRailCollapsed();
  const shorten = sessionShortener(live);

  const needsRows = buildNeedsYou(live, items, dismissed, dismissedNeeds);
  const needsCollapsed = collapsed["sec:needs"] === true;

  const pinned = resolvePinned(prefs.getPinnedPids(), live);
  const pinnedCollapsed = collapsed["sec:pinned"] === true;

  // Prune soft-question dismissals for panes that have left needs-input, so a
  // fresh question re-surfaces. Runs after render (no signal write during it).
  useEffect(() => {
    const next = prunedNeedsDismissals(dismissedNeedsPids.value, live);
    if (next.size !== dismissedNeedsPids.value.size) dismissedNeedsPids.value = next;
  }, [windows.value]);

  function dismiss(id) {
    const next = new Set(dismissed);
    next.add(id);
    dismissedAlertIds.value = next;
  }
  // Click a live needs-you row: navigate to it, and if it's a soft question
  // (no real dialog), dismiss it from the zone. Real dialogs stay sticky.
  function onLiveClick(w) {
    if (isSoftQuestion(w)) {
      const next = new Set(dismissedNeedsPids.value);
      next.add(w.pid);
      dismissedNeedsPids.value = next;
    }
    selectPane(w);
  }

  return (
    <>
      {needsRows.length > 0 && (
        <>
          <SectionHeader
            icon="⚠" label="NEEDS YOU" tone="alert"
            count={needsYouCount(needsRows)}
            collapsed={needsCollapsed}
            onToggle={() => toggle("sec:needs", needsCollapsed)}
          />
          {!needsCollapsed && needsRows.map((r) =>
            r.kind === "live" ? (
              <div key={`live:${r.pid}`} class="rail-row attn-row attn-needs"
                   onClick={() => onLiveClick(r.w)}>
                <span class="attn-dot dot dot-alert dot-pulse"></span>
                <span class="attn-label">{originLabel(r.w, null, null, shorten)}</span>
                <span class="attn-reason">{waitLabel(r.w?.waiting_for)}</span>
              </div>
            ) : (
              <div key={`evt:${r.id}`} class="rail-row attn-row attn-needs attn-event"
                   onClick={() => revealPane(r.target)}>
                <span class="attn-ico">⚠</span>
                <span class="attn-label">{originLabel(r.w, r.session, r.name, shorten)}</span>
                <span class="attn-reason">need_human · {relTime(r.ts)}</span>
                <button class="attn-x" title="dismiss"
                        onClick={(e) => { e.stopPropagation(); dismiss(r.id); }}>×</button>
              </div>
            )
          )}
        </>
      )}

      {pinned.length > 0 && (
        <>
          <SectionHeader
            icon="★" label="PINNED" count={pinned.length}
            collapsed={pinnedCollapsed}
            onToggle={() => toggle("sec:pinned", pinnedCollapsed)}
          />
          {!pinnedCollapsed && pinned.map((w) => (
            <div key={`pin:${w.pid}`} class="rail-row attn-row attn-pinned"
                 onClick={() => selectPane(w)}>
              <span class="attn-ico">{w.is_claude ? "✻" : "$"}</span>
              <span class="attn-label">{originLabel(w, null, null, shorten)}</span>
              <span class={statusDotClass(w.state)} style="margin-left:auto"></span>
            </div>
          ))}
        </>
      )}
    </>
  );
}

// ACTIVITY — bottom of the rail, below the project tree. Default collapsed.
export function ActivitySection() {
  prefs.prefsSignal.value;
  const items = alertItems.value || [];
  const collapsed = prefs.getRailCollapsed();
  const shorten = sessionShortener(windows.value || []);

  const activity = buildActivity(items);
  const activityCollapsed = collapsed["sec:activity"] !== false;  // absent → collapsed

  return (
    <>
      <SectionHeader
        label="ACTIVITY" count={activity.length || null}
        collapsed={activityCollapsed}
        onToggle={() => toggle("sec:activity", activityCollapsed)}
      />
      {!activityCollapsed && activity.map((r) => (
        <div key={`act:${r.id}`} class={`rail-row attn-row attn-activity attn-${r.kind}`}
             onClick={() => revealPane(r.target)}>
          <span class="attn-ico">{r.kind === "done" ? "✓" : r.kind === "milestone" ? "★" : "•"}</span>
          <span class="attn-label">{originLabel(null, r.session, r.name, shorten)}</span>
          <span class="attn-reason">{relTime(r.ts)}</span>
        </div>
      ))}
    </>
  );
}
