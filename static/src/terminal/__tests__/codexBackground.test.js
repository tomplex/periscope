import { describe, expect, it } from "vitest";
import { normalizeAgentTerminalData } from "../terminalCore.js";

const CODEX_BG = "\x1b[48;2;30;30;30m";
const TERM_BG = "\x1b[48;2;40;44;52m";

describe("Codex terminal background normalization", () => {
  it("maps Codex's truecolor panel background to Periscope's canvas", () => {
    expect(normalizeAgentTerminalData(`${CODEX_BG}prompt`, "codex"))
      .toBe(`${TERM_BG}prompt`);
  });

  it("leaves other providers and other truecolors unchanged", () => {
    expect(normalizeAgentTerminalData(CODEX_BG, "claude")).toBe(CODEX_BG);
    expect(normalizeAgentTerminalData("\x1b[48;2;31;31;31m", "codex"))
      .toBe("\x1b[48;2;31;31;31m");
  });

  it("normalizes binary terminal frames", () => {
    const encoded = new TextEncoder().encode(`${CODEX_BG}permission`);
    const normalized = normalizeAgentTerminalData(encoded, "codex");
    expect(new TextDecoder().decode(normalized)).toBe(`${TERM_BG}permission`);
  });
});
