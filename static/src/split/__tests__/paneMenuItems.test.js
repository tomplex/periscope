// The pane menu's pure helpers: which items it lists for a pane, what each key
// does, and what the card previews at each stop. A wrong key here runs an
// action on the wrong pane, or lets a key fall through to a terminal.
import { describe, expect, it } from "vitest";
import { NO_COLOUR_STOP, paneMenuItems, paneMenuKey, stopPreview, SWATCHES } from "../paneMenuItems.js";

const ALL = paneMenuItems({ pinned: false, namePinned: false, move: "B", editor: "Cursor" });
const BARE = paneMenuItems({ pinned: true, namePinned: true, move: null, editor: null });
const press = (key, cursor = -1, { items = ALL, current = null, ...mods } = {}) =>
  paneMenuKey({ key, ...mods }, cursor, items, current);
const stopOf = (items, id) => SWATCHES + items.findIndex((it) => it.id === id);

describe("paneMenuItems", () => {
  it("lists the colour rename, then the pane's actions, then Kill", () => {
    expect(ALL.map((it) => it.id)).toEqual(["rename", "pin", "move", "name", "editor", "kill"]);
    expect(ALL.find((it) => it.id === "move").label).toBe("Move to account B");
    expect(ALL.find((it) => it.id === "editor").label).toBe("Open in Cursor");
  });

  it("leaves out Move and the editor when the pane can't take them", () => {
    expect(BARE.map((it) => it.id)).toEqual(["rename", "pin", "name", "kill"]);
  });

  it("says Unpin on a pinned tab and checks Freeze name on a frozen one", () => {
    expect(BARE.find((it) => it.id === "pin").label).toBe("Unpin tab");
    expect(ALL.find((it) => it.id === "pin").label).toBe("Pin tab");
    expect(BARE.find((it) => it.id === "name").checked).toBe(true);
    expect(ALL.find((it) => it.id === "name").checked).toBe(false);
  });

  it("gives Kill no key", () => {
    expect(ALL.find((it) => it.id === "kill").key).toBeUndefined();
  });
});

describe("paneMenuKey", () => {
  it("picks the colour at 1–6 and removes it at 0", () => {
    expect(press("1")).toEqual({ pick: "rose" });
    expect(press("6", 3)).toEqual({ pick: "plum" });
    expect(press("0")).toEqual({ pick: null });
  });

  it("runs an item by its letter, either case", () => {
    expect(press("p")).toEqual({ run: "pin" });
    expect(press("M")).toEqual({ run: "move" });
    expect(press("n")).toEqual({ run: "name" });
    expect(press("e")).toEqual({ run: "editor" });
    expect(press("r")).toEqual({ run: "rename" });
  });

  it("leaves the letter of an item the pane doesn't have to the page", () => {
    expect(press("m", -1, { items: BARE })).toBeNull();
    expect(press("e", -1, { items: BARE })).toBeNull();
  });

  it("closes at Tab, so focus can't wander out of an open menu", () => {
    expect(press("Tab", 2)).toEqual({ close: true });
  });

  it("enters the strip on the pane's own colour, or on No colour", () => {
    expect(press("ArrowDown", -1, { current: "sky" })).toEqual({ cursor: 3 });
    expect(press("ArrowDown")).toEqual({ cursor: NO_COLOUR_STOP });
    expect(press("ArrowUp", SWATCHES, { current: "iris" })).toEqual({ cursor: 4 });
  });

  it("moves along the strip with Left and Right, wrapping, and holds still on an item", () => {
    expect(press("ArrowRight", 0)).toEqual({ cursor: 1 });
    expect(press("ArrowRight", NO_COLOUR_STOP)).toEqual({ cursor: 0 });
    expect(press("ArrowLeft", 0)).toEqual({ cursor: NO_COLOUR_STOP });
    expect(press("ArrowLeft", SWATCHES + 1)).toEqual({ cursor: SWATCHES + 1 });
  });

  it("moves between the strip and the items with Up and Down, wrapping at both ends", () => {
    const last = SWATCHES + ALL.length - 1;
    expect(press("ArrowDown", 2)).toEqual({ cursor: SWATCHES });
    expect(press("ArrowDown", SWATCHES)).toEqual({ cursor: SWATCHES + 1 });
    expect(press("ArrowDown", last, { current: "rose" })).toEqual({ cursor: 0 });
    expect(press("ArrowUp")).toEqual({ cursor: last });
    expect(press("ArrowUp", 2)).toEqual({ cursor: last });
    expect(press("ArrowUp", SWATCHES + 2)).toEqual({ cursor: SWATCHES + 1 });
  });

  it("acts on the cursor at Enter, and does nothing before the first arrow", () => {
    expect(press("Enter", 1)).toEqual({ pick: "clay" });
    expect(press("Enter", NO_COLOUR_STOP)).toEqual({ pick: null });
    expect(press("Enter", stopOf(ALL, "kill"))).toEqual({ run: "kill" });
    expect(press("Enter", stopOf(BARE, "name"), { items: BARE })).toEqual({ run: "name" });
    expect(press("Enter")).toBeNull();
  });

  it("leaves a chord to the browser and the app: ⌘R reloads, ⌘0 resets the zoom", () => {
    expect(press("r", -1, { metaKey: true })).toBeNull();
    expect(press("p", -1, { ctrlKey: true })).toBeNull();
    expect(press("0", -1, { metaKey: true })).toBeNull();
    expect(press("3", -1, { altKey: true })).toBeNull();
  });

  it("leaves every other key alone, Backspace included", () => {
    expect(press("7")).toBeNull();
    expect(press("a", 0)).toBeNull();
    expect(press("Backspace", 2)).toBeNull();
  });
});

describe("stopPreview", () => {
  it("previews a swatch's colour, no colour on No colour, and nothing off the strip", () => {
    expect(stopPreview(0)).toBe("rose");
    expect(stopPreview(5)).toBe("plum");
    expect(stopPreview(NO_COLOUR_STOP)).toBeNull();
    expect(stopPreview(SWATCHES)).toBeUndefined();
    expect(stopPreview(-1)).toBeUndefined();
  });
});
