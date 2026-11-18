// Tab colours' pure helpers: what a key does in the colour menu, which names
// are worth storing, and the tint a pref resolves to.
import { describe, expect, it } from "vitest";
import { cleanColourNames, colourMenuKey, NO_COLOUR_ROW, RENAME_ROW, tabTintById } from "../tabColours.js";

describe("colourMenuKey", () => {
  it("picks the colour at 1–6, in the menu's order", () => {
    expect(colourMenuKey("1", -1)).toEqual({ pick: "rose" });
    expect(colourMenuKey("4", -1)).toEqual({ pick: "teal" });
    expect(colourMenuKey("6", 3)).toEqual({ pick: "plum" });
  });

  it("removes the colour at 0 and Backspace", () => {
    expect(colourMenuKey("0", -1)).toEqual({ pick: null });
    expect(colourMenuKey("Backspace", 2)).toEqual({ pick: null });
  });

  it("renames at R, either case", () => {
    expect(colourMenuKey("r", -1)).toEqual({ rename: true });
    expect(colourMenuKey("R", 0)).toEqual({ rename: true });
  });

  it("moves the cursor with the arrows, wrapping at both ends", () => {
    expect(colourMenuKey("ArrowDown", -1)).toEqual({ cursor: 0 });
    expect(colourMenuKey("ArrowDown", RENAME_ROW)).toEqual({ cursor: 0 });
    expect(colourMenuKey("ArrowUp", -1)).toEqual({ cursor: RENAME_ROW });
    expect(colourMenuKey("ArrowUp", 0)).toEqual({ cursor: RENAME_ROW });
    expect(colourMenuKey("ArrowUp", NO_COLOUR_ROW)).toEqual({ cursor: 5 });
  });

  it("acts on the cursor's row at Enter, and does nothing before the first arrow", () => {
    expect(colourMenuKey("Enter", 1)).toEqual({ pick: "clay" });
    expect(colourMenuKey("Enter", NO_COLOUR_ROW)).toEqual({ pick: null });
    expect(colourMenuKey("Enter", RENAME_ROW)).toEqual({ rename: true });
    expect(colourMenuKey("Enter", -1)).toBeNull();
  });

  it("leaves every other key alone", () => {
    expect(colourMenuKey("7", -1)).toBeNull();
    expect(colourMenuKey("a", 0)).toBeNull();
    expect(colourMenuKey("Tab", 0)).toBeNull();
  });
});

describe("cleanColourNames", () => {
  it("trims names and drops the empty ones", () => {
    expect(cleanColourNames({ iris: "  ci ", rose: "   ", teal: "" })).toEqual({ iris: "ci" });
  });

  it("drops a name that is the hue's own, in any case", () => {
    expect(cleanColourNames({ moss: "moss", clay: "CLAY", plum: "plums" })).toEqual({ plum: "plums" });
  });

  it("drops names for colours it doesn't know", () => {
    expect(cleanColourNames({ red: "urgent", teal: "ui" })).toEqual({ teal: "ui" });
  });
});

describe("tabTintById", () => {
  it("finds a known tint and falls back to Default", () => {
    expect(tabTintById("strong").wash).toBe(18);
    expect(tabTintById(undefined).id).toBe("default");
    expect(tabTintById("loud").id).toBe("default");
  });
});
