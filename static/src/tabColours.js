// Tab colours: the six colours a pane's card can take, and how strongly they
// tint it (Settings → Tab colours). The ids are what prefs stores, and
// routes/prefs.py validates against them (a test keeps the two in step). The
// hues sit between the state hues at a lower chroma, so a colour never reads
// as a state (DESIGN.md, Tab colours).
export const TAB_COLOURS = [
  { id: "rose", name: "Rose" },
  { id: "clay", name: "Clay" },
  { id: "moss", name: "Moss" },
  { id: "teal", name: "Teal" },
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

// The longest name a colour can take; routes/prefs.py holds the same limit.
export const TAB_COLOUR_NAME_MAX = 24;

export function tabTintById(id) {
  return TAB_TINTS.find((t) => t.id === id) || TAB_TINTS.find((t) => t.id === "default");
}

// The names worth storing: trimmed, and neither empty nor the hue's own name.
export function cleanColourNames(names) {
  const clean = {};
  for (const c of TAB_COLOURS) {
    const name = (names[c.id] || "").trim();
    if (name && name.toLowerCase() !== c.name.toLowerCase()) clean[c.id] = name;
  }
  return clean;
}

// The colour menu's rows, in order: the six colours, then "No colour", then
// "Rename colours…". The cursor is a row index, -1 before the first arrow.
export const NO_COLOUR_ROW = TAB_COLOURS.length;
export const RENAME_ROW = TAB_COLOURS.length + 1;
const ROWS = TAB_COLOURS.length + 2;

// What a key does in the colour menu: 1–6 pick a colour, 0 or Backspace
// remove it, R renames, the arrows move the cursor and Enter acts on its row.
// `pick: null` removes the colour; null means the key is not the menu's.
export function colourMenuKey(key, cursor) {
  if (/^[1-6]$/.test(key)) return { pick: TAB_COLOURS[Number(key) - 1].id };
  if (key === "0" || key === "Backspace") return { pick: null };
  if (key === "r" || key === "R") return { rename: true };
  if (key === "ArrowDown") return { cursor: (cursor + 1) % ROWS };
  if (key === "ArrowUp") return { cursor: cursor < 0 ? ROWS - 1 : (cursor + ROWS - 1) % ROWS };
  if (key === "Enter" && cursor >= 0) {
    if (cursor === RENAME_ROW) return { rename: true };
    return { pick: cursor === NO_COLOUR_ROW ? null : TAB_COLOURS[cursor].id };
  }
  return null;
}
