import { describe, expect, it } from "vitest";
import { terminalTheme } from "../theme.js";

describe("terminal theme", () => {
  it("does not contrast Codex's ANSI-black panels with the canvas", () => {
    expect(terminalTheme.black).toBe(terminalTheme.background);
  });
});
