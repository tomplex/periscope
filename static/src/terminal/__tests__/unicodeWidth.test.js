import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { describe, expect, it } from "vitest";

// The mirrored terminal MUST agree with tmux on character width. xterm's
// BUILT-IN provider is Unicode 6, whose wcwidth falls through to 1 for
// everything above the BMP except the CJK extension planes — so every emoji is
// one cell. tmux uses a modern wcwidth and calls them two. Each emoji on a line
// then drifts the cursor one column: with a 🐍 in the shell prompt, the block
// cursor rendered one cell past where typed text actually landed, permanently.
//
// Loaded through `vm` as a plain script, the same way index.html loads it
// (UMD → global), rather than require(): the repo is "type": "module", so
// require() evaluates the UMD as ESM and hands back an empty namespace.
function loadVendoredAddon() {
  const path = fileURLToPath(
    new URL("../../../vendor/addon-unicode11.js", import.meta.url),
  );
  const sandbox = {};
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(path, "utf8"), sandbox);
  return sandbox.Unicode11Addon;
}

function provider() {
  const ns = loadVendoredAddon();
  const inst = new ns.Unicode11Addon();
  let captured = null;
  inst.activate({ unicode: { register: (p) => { captured = p; } } });
  return captured;
}

describe("terminal unicode widths match tmux", () => {
  it("vendored addon exposes the same global terminalCore reads", () => {
    expect(typeof loadVendoredAddon().Unicode11Addon).toBe("function");
  });

  it("registers the Unicode 11 provider", () => {
    expect(provider().version).toBe("11");
  });

  it("gives emoji two cells (Unicode 6 gives one — the cursor-drift bug)", () => {
    const p = provider();
    expect(p.wcwidth(0x1f40d)).toBe(2); // 🐍 — the one in the prompt
    expect(p.wcwidth(0x1f600)).toBe(2); // 😀
  });

  it("leaves ASCII and CJK widths alone", () => {
    const p = provider();
    expect(p.wcwidth(0x61)).toBe(1); // a
    expect(p.wcwidth(0x4e2d)).toBe(2); // 中
  });
});
