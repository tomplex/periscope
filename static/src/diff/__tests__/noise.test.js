import { describe, expect, it } from "vitest";
import { isGenerated, sortFiles } from "../noise.js";

describe("isGenerated", () => {
  it("catches the committed bundle that motivated this", () => {
    expect(isGenerated("static/dist/app.js")).toBe(true);
    expect(isGenerated("static/dist/chunks/preview.js")).toBe(true);
  });

  it("catches lockfiles and minified/derived artifacts", () => {
    for (const p of [
      "package-lock.json", "uv.lock", "Cargo.lock", "go.sum",
      "web/app.min.js", "web/app.min.css", "web/app.js.map",
      "src/__snapshots__/x.snap",
    ]) expect(isGenerated(p), p).toBe(true);
  });

  it("matches on a path SEGMENT, not a substring", () => {
    // "redistribute/" contains "dist" but is hand-written source.
    expect(isGenerated("redistribute/index.js")).toBe(false);
    expect(isGenerated("src/buildings/model.py")).toBe(false);
    expect(isGenerated("dist/app.js")).toBe(true);       // leading segment
  });

  it("leaves real source alone", () => {
    for (const p of [
      "periscope/gitdiff.py", "static/src/diff/noise.js",
      "docs/spec.md", "Makefile", "lock.py",
    ]) expect(isGenerated(p), p).toBe(false);
  });

  it("is safe on empty/missing input", () => {
    expect(isGenerated("")).toBe(false);
    expect(isGenerated(undefined)).toBe(false);
  });
});

describe("sortFiles", () => {
  it("sinks generated files below real ones", () => {
    const out = sortFiles([
      { path: "static/dist/app.js" },
      { path: "periscope/a.py" },
      { path: "uv.lock" },
      { path: "periscope/b.py" },
    ]);
    expect(out.map((f) => f.path)).toEqual([
      "periscope/a.py", "periscope/b.py", "static/dist/app.js", "uv.lock",
    ]);
  });

  it("is stable within each group, so the list doesn't reshuffle on refresh", () => {
    const input = [
      { path: "z.py" }, { path: "a.py" }, { path: "dist/z.js" }, { path: "dist/a.js" },
    ];
    expect(sortFiles(input).map((f) => f.path))
      .toEqual(["z.py", "a.py", "dist/z.js", "dist/a.js"]);
  });

  it("handles empty input", () => {
    expect(sortFiles([])).toEqual([]);
    expect(sortFiles(undefined)).toEqual([]);
  });
});
