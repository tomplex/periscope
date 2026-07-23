import { describe, expect, it } from "vitest";
import { intralineSegments, tokenize, withIntraline } from "../intraline.js";

const joined = (segs) => segs.map((s) => s.text).join("");
const changed = (segs) => segs.filter((s) => s.changed).map((s) => s.text);

describe("tokenize", () => {
  it("is lossless — segments must reproduce the line exactly", () => {
    const s = "  foo(a, b);  // trailing";
    expect(tokenize(s).join("")).toBe(s);
  });
});

describe("intralineSegments", () => {
  it("marks only the changed argument", () => {
    const r = intralineSegments("foo(a, b)", "foo(a, c)");
    expect(changed(r.del)).toEqual(["b"]);
    expect(changed(r.add)).toEqual(["c"]);
  });

  it("marks a renamed identifier mid-line", () => {
    const r = intralineSegments(
      "const oldName = compute(x);",
      "const newName = compute(x);",
    );
    expect(changed(r.del)).toEqual(["oldName"]);
    expect(changed(r.add)).toEqual(["newName"]);
  });

  it("is lossless — segments rejoin to the original lines", () => {
    const d = "    return a + b;";
    const a = "    return a - b;";
    const r = intralineSegments(d, a);
    expect(joined(r.del)).toBe(d);
    expect(joined(r.add)).toBe(a);
  });

  it("declines unrelated lines rather than lighting them up end-to-end", () => {
    expect(intralineSegments(
      "import os",
      "def completely_different(x, y, z):",
    )).toBeNull();
  });

  it("declines a wholly-rewritten single token (the row color already says it)", () => {
    expect(intralineSegments("one", "ONE")).toBeNull();
  });

  it("returns null for identical lines", () => {
    expect(intralineSegments("same", "same")).toBeNull();
  });

  it("handles insertion at end of line", () => {
    const r = intralineSegments("foo(a)", "foo(a, b)");
    expect(changed(r.del)).toEqual([]);
    expect(changed(r.add)).toEqual([", b"]);
  });
});

describe("withIntraline", () => {
  it("pairs a balanced del-run with the following add-run", () => {
    const out = withIntraline([
      { kind: "ctx", text: "before" },
      { kind: "del", text: "x = 1" },
      { kind: "del", text: "y = 2" },
      { kind: "add", text: "x = 9" },
      { kind: "add", text: "y = 8" },
      { kind: "ctx", text: "after" },
    ]);
    expect(changed(out[1].segs)).toEqual(["1"]);
    expect(changed(out[3].segs)).toEqual(["9"]);
    expect(changed(out[2].segs)).toEqual(["2"]);
    expect(changed(out[4].segs)).toEqual(["8"]);
  });

  it("declines unbalanced runs — which line pairs with which would be a guess", () => {
    const out = withIntraline([
      { kind: "del", text: "a = 1" },
      { kind: "add", text: "a = 2" },
      { kind: "add", text: "b = 3" },
    ]);
    expect(out.every((l) => l.segs === undefined)).toBe(true);
  });

  it("leaves pure additions and context untouched", () => {
    const out = withIntraline([
      { kind: "ctx", text: "c" },
      { kind: "add", text: "brand new" },
    ]);
    expect(out.every((l) => l.segs === undefined)).toBe(true);
  });

  it("does not mutate the input", () => {
    const input = [
      { kind: "del", text: "a = 1" },
      { kind: "add", text: "a = 2" },
    ];
    withIntraline(input);
    expect(input[0].segs).toBeUndefined();
  });

  it("terminates on a trailing del-run with no adds", () => {
    const out = withIntraline([
      { kind: "ctx", text: "c" },
      { kind: "del", text: "gone" },
    ]);
    expect(out).toHaveLength(2);
  });
});
