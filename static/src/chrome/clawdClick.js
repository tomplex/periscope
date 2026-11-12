// What a click on Clawd does (Tamagotchi.jsx), apart from the widget so it
// tests without a DOM: the action for the bubble's item, through the rail's
// own row behaviour, and the double-click rule.
import { openNeedsPane, openReadyPane, revealPane, selectPane } from "../split/alertFeed.js";
import { windows } from "../store.js";
import { endParty } from "./clawdWatch.js";
import { openUpdatePopover } from "./UpdatePill.jsx";

// Act on a duty item's target (clawdDuty.js). The pane is looked up by pid
// now, not when the item was built, so a window renumbered since can't take
// the click. A pane that asks or finished is dismissed exactly as its rail Next
// card would be; a party ends whether or not its pane is still there.
export function actOn(t) {
  const w = (t.pid != null && (windows.value || []).find((x) => String(x.pid) === String(t.pid))) || null;
  if (t.action === "update") return openUpdatePopover({ focus: true });
  if (t.action === "needs") return openNeedsPane(w);
  if (t.action === "ready") return openReadyPane(w);
  if (t.action === "reveal") return revealPane(t.target);
  if (t.action === "party") endParty(t.key);
  selectPane(w);
}

export const DBLCLICK_MS = 250;

// A click acts on the bubble's item, or pets Clawd when nothing is pending; a
// double-click always pets, and its second click is never a second action.
// The update popover opens over the mascot, so that click waits out the
// double-click — the second click would otherwise close what the first opened.
export function clickHandlers({ target, pet, act = actOn, schedule = setTimeout, cancel = clearTimeout }) {
  let pending = null;
  return {
    onClick(e) {
      if (e.detail > 1) return;
      const t = target();
      if (!t) return pet();
      if (t.action === "update") {
        pending = schedule(() => act(t), DBLCLICK_MS);
        return;
      }
      act(t);
    },
    onDblClick() {
      cancel(pending);
      pet();
    },
    dispose() {
      cancel(pending);
    },
  };
}
