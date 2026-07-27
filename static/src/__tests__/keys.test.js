import { describe, expect, it } from "vitest";
import { isRefreshKey, refreshTargetFor, selectedPid } from "../keys.js";

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
