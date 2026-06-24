import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { highlightCode } from "../highlightCode.jsx";

const out = (code, lang) => render(<pre>{highlightCode(code, lang)}</pre>);

describe("highlightCode", () => {
  it("emits tok-* spans for known langs", () => {
    const s = out("const x = 1;", "js");
    expect(s).toContain("tok-keyword");
    expect(s).toContain("<span");
  });
  it("preserves full source text including unhighlighted gaps", () => {
    const code = "const answer = compute(40 + 2);";
    const stripped = out(code, "javascript").replace(/<[^>]+>/g, "");
    expect(stripped).toBe(code);
  });
  it("returns plain text for unknown langs", () => {
    const s = out("MOVE A TO B", "cobol");
    expect(s).not.toContain("<span");
    expect(s).toContain("MOVE A TO B");
  });
});
