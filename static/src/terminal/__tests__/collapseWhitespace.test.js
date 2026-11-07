import { describe, expect, it } from "vitest";
import { collapseWhitespace } from "../terminalCore.js";

describe("copied terminal text", () => {
  it("drops the padding a pane fills each row with", () => {
    expect(collapseWhitespace("foo bar      \n    baz qux    \n")).toBe("foo bar\nbaz qux");
  });

  it("collapses a run inside a line to one space", () => {
    expect(collapseWhitespace("a  \t  b")).toBe("a b");
  });

  it("collapses blank lines to one line break", () => {
    expect(collapseWhitespace("one   \n      \n\ntwo")).toBe("one\ntwo");
  });

  it("trims a single-row selection that ran into the padding", () => {
    expect(collapseWhitespace("  ls -la          ")).toBe("ls -la");
  });
});
