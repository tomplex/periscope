import { describe, expect, it } from "vitest";
import { freezeRows, isStale } from "../layoutFreeze.js";

const r = (key, status) => ({ key, status });

describe("freezeRows", () => {
  it("passes live rows straight through when not frozen", () => {
    const live = [r("a"), r("b")];
    expect(freezeRows(live, [r("a")], false)).toBe(live);
  });

  it("passes live through when nothing is held yet", () => {
    const live = [r("a")];
    expect(freezeRows(live, null, true)).toBe(live);
  });

  it("retains a row that vanished while frozen — it's what you're aiming at", () => {
    const held = [r("a"), r("b")];
    const live = [r("a")];                    // b finished and left RUNNING
    expect(freezeRows(live, held, true).map((x) => x.key)).toEqual(["a", "b"]);
  });

  it("withholds a row that appeared while frozen", () => {
    const held = [r("a")];
    const live = [r("a"), r("new")];
    expect(freezeRows(live, held, true).map((x) => x.key)).toEqual(["a"]);
  });

  it("keeps CONTENTS live for retained rows — only membership is frozen", () => {
    const held = [r("a", "working")];
    const live = [r("a", "idle")];
    expect(freezeRows(live, held, true)[0].status).toBe("idle");
  });

  it("falls back to the held object for a row no longer live", () => {
    const held = [r("gone", "working")];
    expect(freezeRows([], held, true)[0]).toEqual(r("gone", "working"));
  });

  it("preserves held ORDER so rows don't reshuffle under the cursor", () => {
    const held = [r("a"), r("b"), r("c")];
    const live = [r("c"), r("b"), r("a")];    // server reordered
    expect(freezeRows(live, held, true).map((x) => x.key)).toEqual(["a", "b", "c"]);
  });
});

describe("isStale", () => {
  it("is false when not frozen", () => {
    expect(isStale([r("a")], [r("b")], false)).toBe(false);
  });

  it("is false when frozen and nothing changed", () => {
    expect(isStale([r("a", "x")], [r("a", "y")], true)).toBe(false);
  });

  it("is true when a row appeared", () => {
    expect(isStale([r("a"), r("b")], [r("a")], true)).toBe(true);
  });

  it("is true when a row vanished", () => {
    expect(isStale([r("a")], [r("a"), r("b")], true)).toBe(true);
  });

  it("is true on a same-length swap, not just a length change", () => {
    expect(isStale([r("a"), r("c")], [r("a"), r("b")], true)).toBe(true);
  });
});
