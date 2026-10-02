// Render-path test for <UpdatePill>. Same rationale/technique as
// usagePillRender.test.jsx. It matters more here than for most chrome: the
// pill is invisible in dev by construction (the commits-behind check runs only
// on the prod worker, and a dev worktree has no upstream to count against), so
// the browser cannot exercise these states at all.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { updateInfo } from "../../store.js";
import { CommitList, UpdatePill } from "../UpdatePill.jsx";

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
    // The popover exists closed; nothing has been fetched yet.
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('role="dialog" hidden>');
  });

  it("singularizes a single commit", () => {
    updateInfo.value = { behind: 1, checked_at: 1, running: false };
    expect(render(<UpdatePill />)).toContain("1 commit behind origin");
  });

  it("warns when local commits make the fast-forward impossible", () => {
    // The failure this exists for: the update's fast-forward refuses once the
    // checkout carries local commits AND upstream has moved, so "↑ 10 behind"
    // alone armed a button that could not succeed and said nothing about why.
    updateInfo.value = { behind: 10, ahead: 1, checked_at: 1, running: false };
    const html = render(<UpdatePill />);
    expect(html).toContain("↑ 10 behind ⚠");
    expect(html).toContain("1 local commit");
    expect(html).toContain("will refuse");
    // Still armed: the count can be stale (an external rebase already fixed
    // it), and a failed update is harmless — it aborts before touching launchd.
    expect(html).toContain("pull, re-provision and restart");
  });

  it("pluralizes the local-commit warning", () => {
    updateInfo.value = { behind: 3, ahead: 4, checked_at: 1, running: false };
    expect(render(<UpdatePill />)).toContain("4 local commits");
  });

  it("says nothing about local commits when there are none", () => {
    updateInfo.value = { behind: 10, ahead: 0, checked_at: 1, running: false };
    const html = render(<UpdatePill />);
    expect(html).not.toContain("will refuse");
    expect(html).toContain("↑ 10 behind");
    expect(html).not.toContain("⚠");
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

describe("<CommitList>", () => {
  it("lists sha and subject, and counts what the server capped off", () => {
    const commits = [
      { sha: "abc1234", subject: "first" },
      { sha: "def5678", subject: "second <b>" },
    ];
    const html = render(<CommitList commits={commits} behind={5} />);
    expect(html).toContain("abc1234");
    expect(html).toContain("second &lt;b"); // subjects are text, never markup
    expect(html).toContain("…and 3 more");
  });

  it("omits the overflow line when the list is complete", () => {
    const html = render(<CommitList commits={[{ sha: "a", subject: "x" }]} behind={1} />);
    expect(html).not.toContain("more");
  });

  it("distinguishes loading, a failed request and an unrecorded list", () => {
    expect(render(<CommitList commits={null} behind={3} />)).toContain("loading");
    expect(render(<CommitList commits="error" behind={3} />)).toContain("couldn't load");
    expect(render(<CommitList commits={[]} behind={3} />)).toContain("git log failed");
  });
});
