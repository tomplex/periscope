// Render-path smoke test for <SpawnAccountPicker> — same rationale as
// usagePillRender.test.jsx: catches wiring errors (signal not read, active
// class on the wrong chip) that a build alone can't. Click → PATCH behavior
// is browser-verified.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { spawnAccount } from "../../store.js";
import { SpawnAccountPicker } from "../SpawnAccountPicker.jsx";

afterEach(() => {
  spawnAccount.value = null;
});

function activeLabel(html) {
  const m = html.match(/class="spawn-acct-btn is-active"[^>]*>([^<]+)</);
  return m && m[1];
}

describe("<SpawnAccountPicker>", () => {
  it("marks auto active when no pin is set", () => {
    spawnAccount.value = null;
    expect(activeLabel(render(<SpawnAccountPicker />))).toBe("auto");
  });

  it("marks the pinned account's letter active", () => {
    spawnAccount.value = "b";
    const html = render(<SpawnAccountPicker />);
    expect(activeLabel(html)).toBe("B");
    // Exactly one chip is active.
    expect(html.match(/is-active/g)).toHaveLength(1);
  });

  it("renders one chip per account plus auto", () => {
    const html = render(<SpawnAccountPicker />);
    expect(html.match(/spawn-acct-btn/g).length).toBe(3); // auto, A, B
  });
});
