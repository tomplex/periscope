// Unicode 11 width activation through the REAL vendored xterm files, with
// the REAL production Terminal options. Pins the failure where
// `term.unicode.activeVersion = "11"` threw ("allowProposedApi" unset), the
// try/catch reduced it to a console.warn, and every emoji stayed one cell
// wide — rendering the mirrored shell prompt one column short of tmux's
// grid, so the cursor floated one cell ahead of where typed text landed.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { describe, expect, it } from "vitest";
import { XTERM_OPTIONS } from "../terminalCore.js";

// The vendored browser UMD bundles only touch the DOM in Terminal.open();
// construction + write work with stub globals, which is all widths need.
function loadVendored() {
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    queueMicrotask,
    Promise,
    navigator: { userAgent: "node", platform: "MacIntel", language: "en" },
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const f of ["xterm.js", "addon-unicode11.js"]) {
    const src = readFileSync(new URL(`../../../vendor/${f}`, import.meta.url), "utf8");
    vm.runInContext(src, sandbox, { filename: f });
  }
  return sandbox;
}

describe("unicode11 widths", () => {
  it("activates under the production Terminal options (no silent throw)", async () => {
    const g = loadVendored();
    const term = new g.Terminal({ ...XTERM_OPTIONS, cols: 20, rows: 5 });
    // Mirrors startLiveTerminal exactly — but WITHOUT the try/catch, so a
    // regression fails the test instead of degrading to a console.warn.
    term.loadAddon(new g.Unicode11Addon.Unicode11Addon());
    term.unicode.activeVersion = "11";
    expect(term.unicode.activeVersion).toBe("11");

    // 🐍 must occupy two cells, matching tmux's wcwidth — at width 1 every
    // glyph after an emoji renders one column left of tmux's grid.
    await new Promise((r) => term.write("a\u{1F40D}b", r));
    const line = term.buffer.active.getLine(0);
    expect(line.getCell(1).getChars()).toBe("\u{1F40D}");
    expect(line.getCell(1).getWidth()).toBe(2);
    expect(line.getCell(3).getChars()).toBe("b");
  });
});
