// Tab colours: the six colours a pane's card can take, and how strongly they
// tint it (Settings → Tab colours). The ids are what prefs stores, and
// routes/prefs.py validates against them (a test keeps the two in step). The
// hues sit clear of the state and kind hues, at a lower chroma than a blip
// (DESIGN.md, Tab colours).
export const TAB_COLOURS = [
  { id: "rose", name: "Rose" },
  { id: "clay", name: "Clay" },
  { id: "moss", name: "Moss" },
  { id: "sky", name: "Sky" },
  { id: "iris", name: "Iris" },
  { id: "plum", name: "Plum" },
];

// How much of the colour goes into the card's surface: styles.css --tab-wash,
// which main.jsx sets from the pick.
export const TAB_TINTS = [
  { id: "quiet", name: "Quiet", wash: 9 },
  { id: "default", name: "Default", wash: 13 },
  { id: "strong", name: "Strong", wash: 18 },
];

// The longest name a colour can take; routes/prefs.py holds the same limit
// (a test keeps the two in step) and cleans what the menu sends.
export const TAB_COLOUR_NAME_MAX = 24;

export function tabTintById(id) {
  return TAB_TINTS.find((t) => t.id === id) || TAB_TINTS.find((t) => t.id === "default");
}

// The colour menu's rows, in order: the six colours, then "No colour", then
// "Rename colours…". The cursor is a row index, -1 before the first arrow.
export const NO_COLOUR_ROW = TAB_COLOURS.length;
export const RENAME_ROW = TAB_COLOURS.length + 1;
const ROWS = TAB_COLOURS.length + 2;

// What the card previews while the cursor is on `row`: a colour id, null for
// "No colour", undefined (no preview) on "Rename colours…".
export function rowPreview(row) {
  if (row >= 0 && row < NO_COLOUR_ROW) return TAB_COLOURS[row].id;
  return row === NO_COLOUR_ROW ? null : undefined;
}

// What a key press does in the colour menu: 1–6 pick a colour, 0 or Backspace
// remove it, R renames, the arrows move the cursor, Enter acts on its row and
// Tab closes the menu. `pick: null` removes the colour; null means the key is
// not the menu's. A chord (⌘, Ctrl or ⌥ held) is never the menu's: ⌘R
// reloads and ⌘0 resets the zoom.
export function colourMenuKey({ key, metaKey, ctrlKey, altKey }, cursor) {
  if (metaKey || ctrlKey || altKey) return null;
  if (/^[1-6]$/.test(key)) return { pick: TAB_COLOURS[Number(key) - 1].id };
  if (key === "0" || key === "Backspace") return { pick: null };
  if (key === "r" || key === "R") return { rename: true };
  if (key === "Tab") return { close: true };
  if (key === "ArrowDown") return { cursor: (cursor + 1) % ROWS };
  if (key === "ArrowUp") return { cursor: cursor < 0 ? ROWS - 1 : (cursor + ROWS - 1) % ROWS };
  if (key === "Enter" && cursor >= 0) {
    if (cursor === RENAME_ROW) return { rename: true };
    return { pick: rowPreview(cursor) };
  }
  return null;
}
