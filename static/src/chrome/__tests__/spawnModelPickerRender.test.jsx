// Render-path smoke test for <SpawnModelPicker> — same rationale as the
// account picker's: catches wiring errors (signal not read, active class on
// the wrong chip) that a build alone can't. Click → PATCH is browser-verified.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { MODELS } from "../../models.js";
import { spawnModel } from "../../store.js";
import { SpawnModelPicker } from "../SpawnModelPicker.jsx";

afterEach(() => {
  spawnModel.value = null;
});

function activeLabel(html) {
  const m = html.match(/class="spawn-acct-btn is-active"[^>]*>([^<]+)</);
  return m?.[1] ?? null;
}

describe("<SpawnModelPicker>", () => {
  it("marks default active when no pin is set", () => {
    spawnModel.value = null;
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("default");
  });

  it("marks the pinned alias active, and only it", () => {
    spawnModel.value = "opus";
    const html = render(<SpawnModelPicker />);
    expect(activeLabel(html)).toBe("opus");
    expect(html.match(/is-active/g)).toHaveLength(1);
  });

  it("renders one chip per registry entry", () => {
    const html = render(<SpawnModelPicker />);
    expect(html.match(/spawn-acct-btn/g).length).toBe(MODELS.length);
  });
});
