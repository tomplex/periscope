// Tab colours' pure helpers: what a key does in the colour menu, what the card
// previews at each row, and the tint a pref resolves to.
import { describe, expect, it } from "vitest";
import { colourMenuKey, NO_COLOUR_ROW, RENAME_ROW, rowPreview, tabTintById } from "../tabColours.js";

const press = (key, cursor = -1, mods = {}) => colourMenuKey({ key, ...mods }, cursor);

describe("colourMenuKey", () => {
  it("picks the colour at 1–6, in the menu's order", () => {
    expect(press("1")).toEqual({ pick: "rose" });
    expect(press("4")).toEqual({ pick: "sky" });
    expect(press("6", 3)).toEqual({ pick: "plum" });
  });

  it("removes the colour at 0 and Backspace", () => {
    expect(press("0")).toEqual({ pick: null });
    expect(press("Backspace", 2)).toEqual({ pick: null });
  });

  it("renames at R, either case", () => {
    expect(press("r")).toEqual({ rename: true });
    expect(press("R", 0)).toEqual({ rename: true });
  });

  it("closes at Tab, so focus can't wander out of an open menu", () => {
    expect(press("Tab", 2)).toEqual({ close: true });
    expect(press("Tab", -1, { shiftKey: true })).toEqual({ close: true });
  });

  it("moves the cursor with the arrows, wrapping at both ends", () => {
    expect(press("ArrowDown")).toEqual({ cursor: 0 });
    expect(press("ArrowDown", RENAME_ROW)).toEqual({ cursor: 0 });
    expect(press("ArrowUp")).toEqual({ cursor: RENAME_ROW });
    expect(press("ArrowUp", 0)).toEqual({ cursor: RENAME_ROW });
    expect(press("ArrowUp", NO_COLOUR_ROW)).toEqual({ cursor: 5 });
  });

  it("acts on the cursor's row at Enter, and does nothing before the first arrow", () => {
    expect(press("Enter", 1)).toEqual({ pick: "clay" });
    expect(press("Enter", NO_COLOUR_ROW)).toEqual({ pick: null });
    expect(press("Enter", RENAME_ROW)).toEqual({ rename: true });
    expect(press("Enter")).toBeNull();
  });

  it("leaves a chord to the browser and the app: ⌘R reloads, ⌘0 resets the zoom", () => {
    expect(press("r", -1, { metaKey: true })).toBeNull();
    expect(press("r", -1, { ctrlKey: true })).toBeNull();
    expect(press("0", -1, { metaKey: true })).toBeNull();
    expect(press("Backspace", -1, { metaKey: true })).toBeNull();
    expect(press("3", -1, { altKey: true })).toBeNull();
  });

  it("leaves every other key alone", () => {
    expect(press("7")).toBeNull();
    expect(press("a", 0)).toBeNull();
  });
});

describe("rowPreview", () => {
  it("previews a colour row's colour, no colour on its row, and nothing on Rename", () => {
    expect(rowPreview(0)).toBe("rose");
    expect(rowPreview(5)).toBe("plum");
    expect(rowPreview(NO_COLOUR_ROW)).toBeNull();
    expect(rowPreview(RENAME_ROW)).toBeUndefined();
    expect(rowPreview(-1)).toBeUndefined();
  });
});

describe("tabTintById", () => {
  it("finds a known tint and falls back to Default", () => {
    expect(tabTintById("strong").wash).toBe(18);
    expect(tabTintById(undefined).id).toBe("default");
    expect(tabTintById("loud").id).toBe("default");
  });
});
