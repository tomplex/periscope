// Render-path test for <UpdatePill>. Same rationale/technique as
// usagePillRender.test.jsx. It matters more here than for most chrome: the
// pill is invisible in dev by construction (the commits-behind check runs only
// on the prod worker, and a dev worktree has no upstream to count against), so
// the browser cannot exercise these states at all.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { updateInfo } from "../../store.js";
import { UpdatePill } from "../UpdatePill.jsx";

afterEach(() => {
  updateInfo.value = null;
});

describe("<UpdatePill>", () => {
  it("renders nothing when there is no update info", () => {
    expect(render(<UpdatePill />)).toBe("");
  });

  it("renders nothing when the checkout is current", () => {
    // The common case by far — the pill must cost nothing when up to date.
    updateInfo.value = { behind: 0, checked_at: 1, running: false };
    expect(render(<UpdatePill />)).toBe("");
  });

  it("shows the commit count when behind", () => {
    updateInfo.value = { behind: 12, checked_at: 1, running: false };
    const html = render(<UpdatePill />);
    expect(html).toContain("↑ 12 behind");
    expect(html).toContain("12 commits behind origin");
  });

  it("singularizes a single commit", () => {
    updateInfo.value = { behind: 1, checked_at: 1, running: false };
    expect(render(<UpdatePill />)).toContain("1 commit behind origin");
  });

  it("shows a disabled running state when an update is already in flight", () => {
    // `running` arrives from the server, so a second tab must show the state
    // an update started in the first one — not an armed button.
    updateInfo.value = { behind: 12, checked_at: 1, running: true };
    const html = render(<UpdatePill />);
    expect(html).toContain("updating…");
    expect(html).toContain("is-running");
    expect(html).toContain("disabled");
  });
});
