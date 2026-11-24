// The pane menu (PaneMenu.jsx): the items it lists for a pane and what a key
// does in it. Pure: no DOM, no signals.
import { TAB_COLOURS } from "../tabColours.js";

// The menu's stops, in order: the swatch strip (the six colours, then No
// colour), then the items under it. The cursor is a stop index, -1 before
// the first arrow.
export const NO_COLOUR_STOP = TAB_COLOURS.length;
export const SWATCHES = TAB_COLOURS.length + 1;

// The items under the strip. `move` is the destination account's label and
// `editor` the editor's name, each null when the pane can't take it
// (moveAccountTarget, canOpenInEditor). Kill has no key, so a stray key press
// can't close a tab.
export function paneMenuItems({ pinned, namePinned, move, editor }) {
  return [
    { id: "rename", key: "r", label: "Rename colors…" },
    { id: "pin", key: "p", label: pinned ? "Unpin tab" : "Pin tab", sep: true },
    move && { id: "move", key: "m", label: `Move to account ${move}` },
    { id: "name", key: "n", label: "Freeze name", checked: Boolean(namePinned) },
    editor && { id: "editor", key: "e", label: `Open in ${editor}` },
    { id: "kill", label: "Kill tab", sep: true },
  ].filter(Boolean);
}

// What the card previews while the cursor is on `stop`: a colour id, null
// for No colour, undefined off the strip.
export function stopPreview(stop) {
  if (stop >= 0 && stop < NO_COLOUR_STOP) return TAB_COLOURS[stop].id;
  return stop === NO_COLOUR_STOP ? null : undefined;
}

// The cursor enters the strip on the pane's own colour.
function homeSwatch(current) {
  const i = TAB_COLOURS.findIndex((c) => c.id === current);
  return i < 0 ? NO_COLOUR_STOP : i;
}

// What a key press does: 1–6 pick a colour and 0 removes it, an item's letter
// runs it, Up and Down move between the strip and the items, Left and Right
// move along the strip, Enter acts on the cursor, and Tab closes the menu.
// Returns {pick} (null removes the colour), {run: item id}, {cursor},
// {close}, or null when the key is not the menu's. A chord (⌘, Ctrl or ⌥) is
// never the menu's: ⌘R reloads and ⌘0 resets the zoom.
export function paneMenuKey({ key, metaKey, ctrlKey, altKey }, cursor, items, current) {
  if (metaKey || ctrlKey || altKey) return null;
  if (/^[1-6]$/.test(key)) return { pick: TAB_COLOURS[Number(key) - 1].id };
  if (key === "0") return { pick: null };
  const byLetter = key.length === 1 && items.find((it) => it.key === key.toLowerCase());
  if (byLetter) return { run: byLetter.id };
  if (key === "Tab") return { close: true };
  const last = SWATCHES + items.length - 1;
  const onStrip = cursor >= 0 && cursor < SWATCHES;
  if (key === "ArrowRight" || key === "ArrowLeft") {
    if (!onStrip) return { cursor };
    return { cursor: (cursor + (key === "ArrowRight" ? 1 : SWATCHES - 1)) % SWATCHES };
  }
  if (key === "ArrowDown") {
    if (cursor < 0 || cursor === last) return { cursor: homeSwatch(current) };
    return { cursor: onStrip ? SWATCHES : cursor + 1 };
  }
  if (key === "ArrowUp") {
    if (cursor < 0 || onStrip) return { cursor: last };
    return { cursor: cursor === SWATCHES ? homeSwatch(current) : cursor - 1 };
  }
  if (key === "Enter" && cursor >= 0) {
    return onStrip ? { pick: stopPreview(cursor) } : { run: items[cursor - SWATCHES].id };
  }
  return null;
}
