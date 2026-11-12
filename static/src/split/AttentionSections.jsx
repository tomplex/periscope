// The rail's attention zone. Three mount points (see <Rail>):
//   <NextQueue>       — what wants you, in the agent's words, under the sonar strip.
//   <PinnedSection>   — the panes you pinned, above the tree.
//   <ActivitySection> — the low-signal ACTIVITY log, rendered BELOW the tree.
// Reads the windows + alertItems signals through the pure transforms in
// attention.js. Out-of-tree rows use `attn-row` classes (NOT child-row) so they
// never enter the tree's connector-adjacency CSS.
import { useEffect, useState } from "preact/hooks";
import { nextWords } from "../notify/copy.js";
import * as prefs from "../prefs.js";
import { dismissedNeedsPids, dismissedReadyPids, windows } from "../store.js";
import { AGENT_META, paneLabel, relTime, shortestUniqueSuffix } from "../util.js";
import { alertItems, dismissEvents, markReadySeen, nextEntries, openNextEntry, revealPane, selectPane } from "./alertFeed.js";
import { buildActivity, prunedStateDismissals, resolvePinned, waitTier } from "./attention.js";
import { useFrozenRows } from "./layoutFreeze.js";
import { SectionHeader } from "./SectionHeader.jsx";

// Cards shown before the rest fold into "+N".
const NEXT_SHOWN = 3;

// Toggle takes the section's already-computed current state so the inverted
// Activity default lives at exactly one place (the read site), not here.
function toggle(key, currentlyCollapsed) {
  prefs.setRailCollapsedKey(key, !currentlyCollapsed);
}

// A pane that has closed since its event fired has only the event's names.
function entryName(e) {
  return e.w ? paneLabel(e.w) : e.row?.name || e.row?.session || "a closed pane";
}

// The whole card opens its pane on click; the name is the button a keyboard or
// screen reader reaches (a role=button card would hide its × from them), and
// its click bubbles to the card's.
function NextCard({ e, blip, track, nowS, onOpen, onDismiss }) {
  const { lead, code, reason } = nextWords(e);
  const meta = AGENT_META[e.w?.agent] || AGENT_META.claude;
  return (
    <div class={`next-card pane-blip-${blip}`} onClick={onOpen}>
      <span class="pane-stripe" aria-hidden="true"></span>
      <div class="next-head">
        <span class={`rail-icon ${meta.className}`} title={meta.label}>{meta.glyph}</span>
        <button class="next-name" aria-label={`${entryName(e)}, ${reason}: ${lead}${code ? ` ${code}` : ""}`}>
          {entryName(e)}
        </button>
        {e.since ? (
          <span class={`rail-age${e.kind === "asks" ? ` wait-${waitTier(e.since, nowS)}` : ""}`}>{relTime(e.since)}</span>
        ) : null}
        {onDismiss && (
          <button
            class="next-x"
            title={e.kind === "done" ? "mark seen without opening it" : "dismiss"}
            aria-label={e.kind === "done" ? `mark ${entryName(e)} seen` : `dismiss ${entryName(e)}`}
            onClick={(ev) => { ev.stopPropagation(); onDismiss(); }}
          >×</button>
        )}
      </div>
      <div class="next-words" title={code ? `${lead} ${code}` : lead}>
        {lead}{code && <> <code class="next-code">{code}</code></>}
      </div>
      <div class="next-meta">
        <span class="next-track">{track}</span>
        <span aria-hidden="true">·</span>
        <span class={`next-why why-${e.kind}`}>{reason}</span>
      </div>
    </div>
  );
}

// What wants you, under the sonar strip. `blipOf` / `trackOf` are the rail's,
// so a card's stripe matches its blip and its row.
export function NextQueue({ blipOf, trackOf }) {
  const live = windows.value || [];
  const nowS = Math.floor(Date.now() / 1000);
  const [showAll, setShowAll] = useState(false);
  // Hold the queue's MEMBERSHIP still while the pointer is in the rail, so the
  // card you're aiming at can't move out from under the cursor (layoutFreeze.js).
  // Contents stay live.
  const { rows: entries, stale } = useFrozenRows(nextEntries.value);

  // Prune episode-scoped dismissals for panes that have left the relevant
  // state, so the next question/completion re-surfaces. Runs after render
  // (no signal write during it).
  useEffect(() => {
    const nextNeeds = prunedStateDismissals(dismissedNeedsPids.value, live, "needs-input");
    if (nextNeeds.size !== dismissedNeedsPids.value.size) dismissedNeedsPids.value = nextNeeds;
    const nextReady = prunedStateDismissals(dismissedReadyPids.value, live, "done");
    if (nextReady.size !== dismissedReadyPids.value.size) dismissedReadyPids.value = nextReady;
  }, [windows.value]);

  // × on a done card marks the pane seen; on an event's card it dismisses
  // every unanswered event of that kind the pane raised. A live question and
  // a broken pane leave on their own.
  function dismissFor(e) {
    if (e.row?.kind === "event") return () => dismissEvents(e.kind === "asks" ? "need_human" : "done", e.row.target);
    if (e.kind === "done") return () => markReadySeen(e.w);
    return null;
  }

  const shown = showAll ? entries : entries.slice(0, NEXT_SHOWN);
  const folded = entries.slice(NEXT_SHOWN);
  return (
    <section class="next" aria-label="what wants you">
      {/* Say so when the freeze is actually withholding something. A rail that
          quietly stops updating is worse than one that moves. */}
      {stale && (
        <div class="attn-frozen" title="Move the pointer out of the rail to apply">
          updates paused
        </div>
      )}
      <div class="next-title-row">
        <span class="next-title">Next</span>
        {entries.length > 0 && <span class={`next-count count-${entries[0].kind}`}>{entries.length}</span>}
        <span class="next-hint">
          {entries.length > 0 ? <>open next <kbd class="ps-kbd">⌘J</kbd></> : "nothing wants you"}
        </span>
      </div>
      {shown.map((e) => (
        <NextCard
          key={e.key}
          e={e}
          blip={e.w ? blipOf(e.w) : e.kind}
          track={e.w ? trackOf(e.w) : e.row?.session || ""}
          nowS={nowS}
          onOpen={() => openNextEntry(e)}
          onDismiss={dismissFor(e)}
        />
      ))}
      {folded.length > 0 && (
        <button class="next-more" aria-expanded={showAll} onClick={() => setShowAll(!showAll)}>
          {showAll ? "show fewer" : (
            <>
              <span class="next-more-n">+{folded.length}</span>
              <span class="next-more-list">{folded.map((e) => `${entryName(e)} ${nextWords(e).reason}`).join(" · ")}</span>
            </>
          )}
        </button>
      )}
    </section>
  );
}

// The panes you pinned, above the tree.
export function PinnedSection({ blipOf, trackOf }) {
  prefs.prefsSignal.value;
  const pinned = resolvePinned(prefs.getPinnedPids(), windows.value || []);
  const collapsed = prefs.getRailCollapsed()["sec:pinned"] === true;
  if (!pinned.length) return null;
  return (
    <>
      <SectionHeader
        icon="★" label="PINNED" count={pinned.length}
        collapsed={collapsed}
        onToggle={() => toggle("sec:pinned", collapsed)}
      />
      {!collapsed && pinned.map((w) => (
        <div key={`pin:${w.pid}`} class="rail-row attn-row attn-pinned" onClick={() => selectPane(w)}>
          <span class={`blip blip-${blipOf(w)}`} aria-hidden="true"></span>
          <span class="attn-label">{paneLabel(w)}</span>
          <span class="attn-reason">{trackOf(w)}</span>
        </div>
      ))}
    </>
  );
}

// ACTIVITY — bottom of the rail, below the project tree. Default collapsed.
export function ActivitySection() {
  prefs.prefsSignal.value;
  const items = alertItems.value || [];
  const collapsed = prefs.getRailCollapsed();
  // Suffix-shortener over every live session name, so a label only grows a
  // segment when it would actually collide with another open session.
  const sessions = [...new Set((windows.value || []).map((w) => w.session).filter(Boolean))];

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
          <span class="attn-label">{`${shortestUniqueSuffix(r.session || "", sessions)} · ${r.name || ""}`}</span>
          <span class="attn-reason">{relTime(r.ts)}</span>
        </div>
      ))}
    </>
  );
}
