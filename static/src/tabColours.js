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
