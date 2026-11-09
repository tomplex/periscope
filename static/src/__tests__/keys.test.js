import { describe, expect, it } from "vitest";
import { isNextKey, isRefreshKey, panelToggleFor, refreshTargetFor, selectedPid } from "../keys.js";

const key = (over = {}) => ({ key: "r", metaKey: false, ctrlKey: false, shiftKey: false, ...over });

describe("selectedPid", () => {
  it("extracts a pid from a pane highlight-key", () => {
    expect(selectedPid("pane:aa11")).toBe("aa11");
  });

  it("returns null for the review form and for no selection", () => {
    // railSelection is a union of key shapes; only the pane form has a pid.
    expect(selectedPid("review:/dev/x")).toBeNull();
    expect(selectedPid(null)).toBeNull();
    expect(selectedPid(undefined)).toBeNull();
  });
});

describe("isRefreshKey", () => {
  it("matches meta+r and ctrl+r, either case", () => {
    expect(isRefreshKey(key({ metaKey: true }))).toBe(true);
    expect(isRefreshKey(key({ ctrlKey: true }))).toBe(true);
    expect(isRefreshKey(key({ metaKey: true, key: "R" }))).toBe(true);
  });

  it("ignores a bare r so typing in the terminal is unaffected", () => {
    expect(isRefreshKey(key())).toBe(false);
  });

  it("still matches with shift held — ⌘⇧R is the app-reload branch", () => {
    // The handler claims the chord first, THEN splits on shift; if this
    // returned false the browser would reload before we could preventDefault.
    expect(isRefreshKey(key({ metaKey: true, shiftKey: true }))).toBe(true);
  });
});

describe("refreshTargetFor", () => {
  it("targets the document when a file tab is frontmost", () => {
    expect(refreshTargetFor("pane:aa11", { aa11: "file:/tmp/x.html" })).toBe("document");
  });

  it("targets the terminal when the pane tab is frontmost", () => {
    expect(refreshTargetFor("pane:aa11", { aa11: "pane" })).toBe("terminal");
  });

  it("targets the terminal when the pane has no recorded tab", () => {
    expect(refreshTargetFor("pane:aa11", {})).toBe("terminal");
  });

  it("targets the terminal for a review selection or no selection", () => {
    expect(refreshTargetFor("review:/dev/x", { aa11: "file:/tmp/x" })).toBe("terminal");
    expect(refreshTargetFor(null, { aa11: "file:/tmp/x" })).toBe("terminal");
  });

  it("does not throw on a missing tab map", () => {
    expect(refreshTargetFor("pane:aa11", undefined)).toBe("terminal");
  });

  it("reads the tab of the SELECTED pane, not any pane with a file open", () => {
    // A doc open on another pane must not steer ⌘R away from the terminal
    // you are actually looking at.
    const tabs = { other: "file:/tmp/x.html", aa11: "pane" };
    expect(refreshTargetFor("pane:aa11", tabs)).toBe("terminal");
  });
});

describe("panelToggleFor", () => {
  // e.code, not e.key: on a Mac, holding ⌥ turns e.key for B into "∫".
  const b = (over = {}) => ({ code: "KeyB", key: "b", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...over });

  it("⌘B toggles the navigator, ⌥⌘B the context column", () => {
    expect(panelToggleFor(b({ metaKey: true }))).toBe("rail");
    expect(panelToggleFor(b({ metaKey: true, altKey: true, key: "∫" }))).toBe("side");
  });
  it("accepts ctrl for a non-mac keyboard", () => {
    expect(panelToggleFor(b({ ctrlKey: true }))).toBe("rail");
    expect(panelToggleFor(b({ ctrlKey: true, altKey: true }))).toBe("side");
  });
  it("ignores a bare B, shifted chords, and other keys", () => {
    expect(panelToggleFor(b())).toBe(null);
    expect(panelToggleFor(b({ metaKey: true, shiftKey: true }))).toBe(null);
    expect(panelToggleFor(b({ metaKey: true, code: "KeyN", key: "n" }))).toBe(null);
  });
  it("ignores key-repeat, so a held chord toggles once", () => {
    expect(panelToggleFor(b({ metaKey: true, repeat: true }))).toBe(null);
  });
});

describe("isNextKey", () => {
  const j = (over = {}) => ({ code: "KeyJ", key: "j", metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, repeat: false, ...over });

  it("is ⌘J", () => {
    expect(isNextKey(j({ metaKey: true }))).toBe(true);
  });

  it("ignores Ctrl+J, which the terminal sends to the pane as a line feed", () => {
    expect(isNextKey(j({ ctrlKey: true }))).toBe(false);
  });

  it("ignores a bare J, other modifiers and auto-repeat", () => {
    expect(isNextKey(j())).toBe(false);
    expect(isNextKey(j({ metaKey: true, shiftKey: true }))).toBe(false);
    expect(isNextKey(j({ metaKey: true, altKey: true }))).toBe(false);
    expect(isNextKey(j({ metaKey: true, repeat: true }))).toBe(false);
  });
});
