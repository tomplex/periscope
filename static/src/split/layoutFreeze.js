// Holds the top of the rail still while you're aiming at it: the sonar
// strip's lines and the Next queue under them.
//
// The queue sits above the tree and the same panes also appear in it, so one
// pane finishing or asking shifts everything below. That happens all the
// time, and the row you were about to click moves out from under the cursor.
// The strip sits above the queue, so a track gaining its first pane or losing
// its last would move every card too.
//
// Animating the movement does NOT fix that — a smoothly-moving target is still
// a moving target. The fix is to stop moving while the pointer is in the rail,
// which is exactly and only when layout stability matters. Everywhere else you
// want it live.
//
// Membership only. Row CONTENTS (status text, spinner, timers) keep updating
// while frozen — freezing those would make the rail look hung.

import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";

// True while the pointer is inside the rail. Set by <Rail>, read by the
// attention sections. A signal rather than a prop so nothing between them has
// to re-render to pass it down.
export const railHovered = signal(false);

// Bumped when you act on what Next lists, wherever from: opening an entry (its
// card, ⌘J) or dismissing one (its ×, a banner's Mark seen). That is a user
// action, so a held list lets its pending changes through at once rather than
// keep showing the card you just acted on.
export const freezeReleases = signal(0);
export function releaseFreeze() {
  freezeReleases.value += 1;
}

// freezeRows for a component: holds `liveRows`' membership while the pointer
// is in the rail, until the pointer leaves or releaseFreeze. { rows, stale }.
export function useFrozenRows(liveRows, identity = (r) => r.key) {
  const frozen = railHovered.value;
  const held = useRef(null);
  const release = useRef(freezeReleases.value);
  if (release.current !== freezeReleases.value) {
    release.current = freezeReleases.value;
    held.current = null;
  }
  // Capture the live set whenever we're thawed; that snapshot is what the next
  // freeze holds. No dep array — cheap, and it must track every render.
  useEffect(() => {
    if (!frozen) held.current = liveRows;
  });
  return {
    rows: freezeRows(liveRows, held.current, frozen, identity),
    stale: isStale(liveRows, held.current, frozen, identity),
  };
}

/**
 * Pick which rows to render given the live rows and whether we're frozen.
 *
 * Frozen: keep the previously-rendered ROW SET, but swap in each row's current
 * object so contents stay live. A row that vanished while frozen is retained
 * (that's the point — it's what you're aiming at); a row that appeared while
 * frozen is withheld until the thaw.
 *
 * `identity` maps a row to its stable key.
 */
export function freezeRows(liveRows, heldRows, frozen, identity = (r) => r.key) {
  const live = liveRows || [];
  if (!frozen || !heldRows) return live;
  const byId = new Map(live.map((r) => [identity(r), r]));
  // Held order is preserved so nothing reshuffles under the cursor either.
  return heldRows.map((h) => byId.get(identity(h)) || h);
}

/**
 * True when the frozen view has drifted from the live one — the caller uses
 * this to show a subtle "updates paused" hint rather than silently lying.
 */
export function isStale(liveRows, heldRows, frozen, identity = (r) => r.key) {
  if (!frozen || !heldRows) return false;
  const a = (liveRows || []).map(identity);
  const b = heldRows.map(identity);
  if (a.length !== b.length) return true;
  const bs = new Set(b);
  return a.some((k) => !bs.has(k));
}
