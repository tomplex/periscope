// Render-path smoke test for <SpawnModelPicker> — same rationale as the
// account picker's: catches wiring errors (signal not read, active class on
// the wrong chip) that a build alone can't. Click → PATCH is browser-verified.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { PIN_MODELS } from "../../models.js";
import { launchDefault, spawnModel } from "../../store.js";
import { SpawnModelPicker } from "../SpawnModelPicker.jsx";

afterEach(() => {
  spawnModel.value = null;
  launchDefault.value = null;
});

function activeLabel(html) {
  const m = html.match(/class="spawn-acct-btn is-active"[^>]*>([^<]+)</);
  return m?.[1] ?? null;
}

describe("<SpawnModelPicker>", () => {
  it("marks auto active when no pin is set and shows what auto resolves to", () => {
    spawnModel.value = null;
    launchDefault.value = { account: "b", model: "fable", reason: "b · week resets Wed 22:59 · fable" };
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → fable");
  });

  it("reads auto → default before the first poll", () => {
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → default");
  });

  it("marks a stored auto pin active exactly like unset", () => {
    spawnModel.value = "auto";
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → default");
  });

  it("marks the pinned alias active, and only it", () => {
    spawnModel.value = "opus[1m]";
    const html = render(<SpawnModelPicker />);
    expect(activeLabel(html)).toBe("opus 1m");
    expect(html.match(/is-active/g)).toHaveLength(1);
  });

  it("marks a literal default pin active", () => {
    spawnModel.value = "default";
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("default");
  });

  it("renders one chip per pin entry", () => {
    const html = render(<SpawnModelPicker />);
    expect(html.match(/spawn-acct-btn/g).length).toBe(PIN_MODELS.length);
  });
});
