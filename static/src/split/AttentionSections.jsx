// The left-rail attention zone: NEEDS YOU + PINNED + ACTIVITY, stacked above
// the project tree, all rendered inside <Rail>'s <aside>. Reads the windows +
// alertItems signals through the pure transforms in attention.js. Out-of-tree
// rows use `attn-row` classes (NOT child-row) so they never enter the tree's
// connector-adjacency CSS.
import { windows, dismissedAlertIds, railSelection } from "../store.js";
import { alertItems, revealPane } from "./alertFeed.js";
import * as prefs from "../prefs.js";
import { relTime, waitLabel } from "../util.js";
import { SectionHeader } from "./SectionHeader.jsx";
import { buildNeedsYou, needsYouCount, resolvePinned, buildActivity } from "./attention.js";
import { statusDotClass } from "./RailRows.jsx";

function paneLabel(w) {
  return w?.name || (w?.is_claude ? "claude" : "shell");
}
function originLabel(w, fallbackSession, fallbackName) {
  const session = w?.session || fallbackSession || "";
  const name = paneLabel(w) || fallbackName || "";
  return `${session} · ${name}`;
}

// Mirror the rail's own selection write (string highlight signal + object pref).
// Static import of railSelection is safe — store.js imports nothing from here.
function selectPane(w) {
  if (!w?.pid) return;
  railSelection.value = `pane:${w.pid}`;
  prefs.setLastSelected({ kind: "pane", pid: w.pid });
}

export function AttentionSections() {
  // Subscribe to prefs explicitly (pins + section-collapse live there), the
  // same way Rail does — so reactivity is self-contained, not inherited from
  // the parent's subscription.
  prefs.prefsSignal.value;
  const live = windows.value || [];
  const items = alertItems.value || [];
  const dismissed = dismissedAlertIds.value;
  const collapsed = prefs.getRailCollapsed();

  // NEEDS YOU
  const needsRows = buildNeedsYou(live, items, dismissed);
  const needsCollapsed = collapsed["sec:needs"] === true;

  // PINNED
  const pinned = resolvePinned(prefs.getPinnedPids(), live);
  const pinnedCollapsed = collapsed["sec:pinned"] === true;

  // ACTIVITY (default collapsed: absent → collapsed)
  const activity = buildActivity(items);
  const activityCollapsed = collapsed["sec:activity"] !== false;

  // Toggle takes the section's already-computed current state so the inverted
  // Activity default lives at exactly one place (the const above), not here.
  function toggle(key, currentlyCollapsed) {
    prefs.setRailCollapsedKey(key, !currentlyCollapsed);
  }
  function dismiss(id) {
    const next = new Set(dismissed);
    next.add(id);
    dismissedAlertIds.value = next;
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
                   onClick={() => selectPane(r.w)}>
                <span class="attn-dot dot dot-alert dot-pulse"></span>
                <span class="attn-label">{originLabel(r.w)}</span>
                <span class="attn-reason">{waitLabel(r.w?.waiting_for)}</span>
              </div>
            ) : (
              <div key={`evt:${r.id}`} class="rail-row attn-row attn-needs attn-event"
                   onClick={() => revealPane(r.target)}>
                <span class="attn-ico">⚠</span>
                <span class="attn-label">{originLabel(r.w, r.session, r.name)}</span>
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
              <span class="attn-label">{originLabel(w)}</span>
              <span class={statusDotClass(w.state)} style="margin-left:auto"></span>
            </div>
          ))}
        </>
      )}

      <SectionHeader
        label="ACTIVITY" count={activity.length || null}
        collapsed={activityCollapsed}
        onToggle={() => toggle("sec:activity", activityCollapsed)}
      />
      {!activityCollapsed && activity.map((r) => (
        <div key={`act:${r.id}`} class={`rail-row attn-row attn-activity attn-${r.kind}`}
             onClick={() => revealPane(r.target)}>
          <span class="attn-ico">{r.kind === "done" ? "✓" : r.kind === "milestone" ? "★" : "•"}</span>
          <span class="attn-label">{originLabel(null, r.session, r.name)}</span>
          <span class="attn-reason">{relTime(r.ts)}</span>
        </div>
      ))}
    </>
  );
}
