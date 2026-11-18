// Tab colours' pure helpers: the tint a pref resolves to. The pane menu's keys
// and preview are in split/__tests__/paneMenuItems.test.js.
import { describe, expect, it } from "vitest";
import { tabTintById } from "../tabColours.js";

describe("tabTintById", () => {
  it("finds a known tint and falls back to Default", () => {
    expect(tabTintById("strong").wash).toBe(18);
    expect(tabTintById(undefined).id).toBe("default");
    expect(tabTintById("loud").id).toBe("default");
  });
});
