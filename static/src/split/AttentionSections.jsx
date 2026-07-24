// The left-rail attention zone. Two mount points (see <Rail>):
//   <AttentionTop>    — NEEDS YOU + READY + RUNNING + PINNED, rendered ABOVE the project tree.
//   <ActivitySection> — the low-signal ACTIVITY log, rendered BELOW the tree.
// Reads the windows + alertItems signals through the pure transforms in
// attention.js. Out-of-tree rows use `attn-row` classes (NOT child-row) so they
// never enter the tree's connector-adjacency CSS.
import { useEffect, useRef } from "preact/hooks";
import * as prefs from "../prefs.js";
import {dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, railSelection,
  windows, 
} from "../store.js";
import { relTime, shortestUniqueSuffix, waitLabel } from "../util.js";
import { alertItems, revealPane } from "./alertFeed.js";
import {buildActivity,
  buildNeedsYou, buildReady, buildRunning, 
  isSoftQuestion, needsYouCount, prunedStateDismissals,resolvePinned, 
} from "./attention.js";
import { freezeRows, isStale, railHovered } from "./layoutFreeze.js";
import { statusDotClass } from "./RailRows.jsx";
import { SectionHeader } from "./SectionHeader.jsx";

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

// Stable identity for the freeze latch — mirrors the JSX keys below, where
// live rows key on pane pid and event rows on alert id.
function rowKey(r) {
  return `${r.kind}:${r.pid ?? r.id}`;
}

// NEEDS YOU + READY + RUNNING + PINNED — top of the rail, above the project tree.
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

  const needsRowsLive = buildNeedsYou(live, items, dismissed, dismissedNeeds);
  const needsCollapsed = collapsed["sec:needs"] === true;

  const readyRowsLive = buildReady(live, items, dismissed, dismissedReadyPids.value);
  const readyCollapsed = collapsed["sec:ready"] === true;

  const runningRowsLive = buildRunning(live);
  const runningCollapsed = collapsed["sec:running"] === true;

  const pinned = resolvePinned(prefs.getPinnedPids(), live);
  const pinnedCollapsed = collapsed["sec:pinned"] === true;

  // --- layout freeze (see layoutFreeze.js) ---------------------------------
  // Hold section MEMBERSHIP still while the pointer is in the rail, so the row
  // you're aiming at can't move out from under the cursor. Contents stay live.
  // PINNED is deliberately excluded: it only changes when you pin something,
  // and a user action must never be the thing that's withheld.
  const frozen = railHovered.value;
  const held = useRef(null);
  const needsRows = freezeRows(needsRowsLive, held.current?.needs, frozen, rowKey);
  const readyRows = freezeRows(readyRowsLive, held.current?.ready, frozen, rowKey);
  const runningRows = freezeRows(runningRowsLive, held.current?.running, frozen, rowKey);
  const stale =
    isStale(needsRowsLive, held.current?.needs, frozen, rowKey) ||
    isStale(readyRowsLive, held.current?.ready, frozen, rowKey) ||
    isStale(runningRowsLive, held.current?.running, frozen, rowKey);

  // Capture the live set whenever we're thawed; that snapshot is what the next
  // freeze holds. No dep array — cheap, and it must track every render.
  useEffect(() => {
    if (!frozen) {
      held.current = {
        needs: needsRowsLive, ready: readyRowsLive, running: runningRowsLive,
      };
    }
  });

  // Any click inside the sections is a user action, so let the pending changes
  // through immediately rather than holding a row the user just acted on.
  function flush() {
    held.current = null;
  }

  // Prune episode-scoped dismissals for panes that have left the relevant
  // state, so the next question/completion re-surfaces. Runs after render
  // (no signal write during it).
  useEffect(() => {
    const nextNeeds = prunedStateDismissals(dismissedNeedsPids.value, live, "needs-input");
    if (nextNeeds.size !== dismissedNeedsPids.value.size) dismissedNeedsPids.value = nextNeeds;
    const nextReady = prunedStateDismissals(dismissedReadyPids.value, live, "done");
    if (nextReady.size !== dismissedReadyPids.value.size) dismissedReadyPids.value = nextReady;
  }, [windows.value]);

  function dismiss(id) {
    flush();
    const next = new Set(dismissed);
    next.add(id);
    dismissedAlertIds.value = next;
  }
  // Click a live needs-you row: navigate to it, and if it's a soft question
  // (no real dialog), dismiss it from the zone. Real dialogs stay sticky.
  function onLiveClick(w) {
    flush();
    if (isSoftQuestion(w)) {
      const next = new Set(dismissedNeedsPids.value);
      next.add(w.pid);
      dismissedNeedsPids.value = next;
    }
    selectPane(w);
  }
  // Click a live ready row: navigate + always dismiss for this done-episode.
  // Selecting normally acks via the detail terminal's WS connect, but an
  // already-selected pane won't reconnect — the dismissal covers that case.
  function onReadyClick(w) {
    flush();
    const next = new Set(dismissedReadyPids.value);
    next.add(w.pid);
    dismissedReadyPids.value = next;
    selectPane(w);
  }

  return (
    <>
      {/* Say so when the freeze is actually withholding something. A rail that
          quietly stops updating is worse than one that moves. */}
      {stale && (
        <div class="attn-frozen" title="Move the pointer out of the rail to apply">
          updates paused
        </div>
      )}
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

      {readyRows.length > 0 && (
        <>
          <SectionHeader
            icon="✓" label="READY" tone="ready"
            count={readyRows.length}
            collapsed={readyCollapsed}
            onToggle={() => toggle("sec:ready", readyCollapsed)}
          />
          {!readyCollapsed && readyRows.map((r) =>
            r.kind === "live" ? (
              <div key={`rlive:${r.pid}`} class="rail-row attn-row attn-ready"
                   onClick={() => onReadyClick(r.w)}>
                <span class="attn-dot dot dot-blue dot-pulse-done"></span>
                <span class="attn-label">{originLabel(r.w, null, null, shorten)}</span>
                <span class="attn-reason">{relTime(r.w?.completed_at)}</span>
              </div>
            ) : (
              <div key={`revt:${r.id}`} class="rail-row attn-row attn-ready attn-event"
                   onClick={() => revealPane(r.target)}>
                <span class="attn-ico">✓</span>
                <span class="attn-label">{originLabel(r.w, r.session, r.name, shorten)}</span>
                <span class="attn-reason">done · {relTime(r.ts)}</span>
                <button class="attn-x" title="dismiss"
                        onClick={(e) => { e.stopPropagation(); dismiss(r.id); }}>×</button>
              </div>
            )
          )}
        </>
      )}

      {runningRows.length > 0 && (
        <>
          <SectionHeader
            icon="⟳" label="RUNNING" tone="working"
            count={runningRows.length}
            collapsed={runningCollapsed}
            onToggle={() => toggle("sec:running", runningCollapsed)}
          />
          {!runningCollapsed && runningRows.map((r) => (
            <div key={`run:${r.pid}`} class="rail-row attn-row attn-running"
                 onClick={() => selectPane(r.w)}>
              <span class="attn-dot dot dot-green"></span>
              <span class="attn-label">{originLabel(r.w, null, null, shorten)}</span>
              <span class="attn-reason">{`${r.w?.spinner || "working"}…`}</span>
            </div>
          ))}
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
          <span class="attn-ico">{r.kind === "done" ? "✓" : "•"}</span>
          <span class="attn-label">{originLabel(null, r.session, r.name, shorten)}</span>
          <span class="attn-reason">{relTime(r.ts)}</span>
        </div>
      ))}
    </>
  );
}
