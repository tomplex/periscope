// Render-path smoke test for <Rail> — the vitest equivalent of "does the
// rail actually paint" for the track-anchored tree. Pure-tree logic is covered
// in railTree.test.js; this catches wiring errors in Rail.jsx / RailRows.jsx
// (bad prop names, unscoped vars) that a build alone can't, since this repo
// browser-verifies instead of mounting components elsewhere. prefs stays at its
// unloaded default, so the syncRailPrefs side effect is guarded off
// (loaded:false write guard).

import render from "preact-render-to-string";
import { afterEach, describe, expect, it, vi } from "vitest";
import { alerts, dismissedAlertIds, projects, tracks, windows } from "../../store.js";
import { Rail } from "../Rail.jsx";

const aff = (kind, label = null) => ({ kind, label });

afterEach(() => {
  vi.useRealTimers();
  alerts.value = [];
  dismissedAlertIds.value = new Set();
});

describe("<Rail> render smoke", () => {
  it("renders track groups, chips, and single-branch flat tabs", () => {
    projects.value = [];
    windows.value = [
      // single-branch track: two tabs on the same branch render flat
      { pid: "aa11", session: "managed", index: 0, target: "managed:0", name: "claude",
        is_claude: true, state: "working", track_id: "/dev/myproj", track_name: "myproj",
        repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
        cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"), pane_id: "%1" },
      { pid: "bb22", session: "managed", index: 1, target: "managed:1", name: "shell",
        is_claude: false, state: "shell", track_id: "/dev/myproj", track_name: "myproj",
        repo_key: "/dev/other", repo_label: "other", branch: "master",
        cwd: "/dev/other", worktree_affiliation: aff("off-repo", "other"), pane_id: "%2" },
      // a second track with two distinct branches → branch sub-clusters
      { pid: "cc33", session: "managed", index: 2, target: "managed:2", name: "feat-work",
        is_claude: true, state: "idle", track_id: "tk_feature", track_name: "Feature",
        repo_key: "/dev/fdy", repo_label: "fdy", branch: "master",
        cwd: "/dev/fdy", worktree_affiliation: aff("no-repo"), pane_id: "%3" },
      { pid: "dd44", session: "managed", index: 3, target: "managed:3", name: "feat-x",
        is_claude: true, state: "idle", track_id: "tk_feature", track_name: "Feature",
        repo_key: "/dev/fdy", repo_label: "fdy", branch: "feat-x",
        cwd: "/dev/fdy", worktree_affiliation: aff("no-repo"), pane_id: "%4" },
    ];

    const html = render(<Rail />);

    expect(html).toContain("myproj");        // single-branch track row label
    expect(html).toContain("Feature");       // multi-branch track row label
    expect(html).toContain("⧉ other/master"); // off-repo chip on a track tab
    expect(html).toContain("master");        // branch sub-cluster label
    expect(html).toContain("feat-x");        // second branch sub-cluster label
    expect(html).toContain("New tab");       // newtab affordance
  });

  it("renders an EMPTY goal track from the registry with its + New tab", () => {
    projects.value = [];
    tracks.value = [{ id: "tk_fresh", name: "Fresh goal", repo: "/dev/fdy" }];
    windows.value = [
      { pid: "aa11", session: "managed", index: 0, target: "managed:0", name: "claude",
        is_claude: true, state: "working", track_id: "/dev/myproj", track_name: "myproj",
        repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
        cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"), pane_id: "%1" },
    ];

    const html = render(<Rail />);
    tracks.value = [];   // don't leak into other cases

    expect(html).toContain("Fresh goal");    // empty track card renders
    const newTabs = html.split("New tab").length - 1;
    expect(newTabs).toBe(2);                 // one per track, incl. the empty one
  });

  it("a repo-default and a same-named goal track differ by icon and menu", () => {
    // Reproduces the reported rail: two rows both labeled "sts2-seed-finder",
    // one the repo's catchall and one a goal track. Only the goal track gets
    // the ⋯ menu — the catchall's dissolve/teardown both fail server-side.
    projects.value = [];
    tracks.value = [];
    const w = (over) => ({
      pid: "x", session: "managed", index: 0, target: "managed:0", name: "claude",
      is_claude: true, state: "idle", track_name: "sts2-seed-finder",
      repo_key: "/dev/sts2", repo_label: "sts2", branch: "master",
      cwd: "/dev/sts2", worktree_affiliation: aff("at-pin"), pane_id: "%1", ...over,
    });
    windows.value = [
      w({ pid: "aa", track_id: "/dev/sts2", track_kind: "repo", pane_id: "%1" }),
      w({ pid: "bb", track_id: "tk_sts2", track_kind: "goal", pane_id: "%2", index: 1 }),
    ];

    const html = render(<Rail />);

    expect(html.split("sts2-seed-finder").length - 1).toBeGreaterThanOrEqual(2);  // labels collide
    expect(html).toContain('title="project"');   // repo-default icon
    expect(html).toContain('title="track"');     // goal-track icon
    expect(html.split("rail-track-menu-btn").length - 1).toBe(1);  // goal track only
  });
});

describe("<Rail> awaiting-reply marker", () => {
  // The pane row must carry the unanswered-question signal on its own, not
  // only inside the collapsible NEEDS YOU section. Drives the real wiring:
  // store.alerts -> Rail.jsx awaitingReplyByPid -> PaneRow chip.
  const paneWin = (over = {}) => ({
    pid: "aa11", session: "managed", index: 0, target: "managed:0",
    name: "claude", is_claude: true, state: "idle", track_id: "/dev/myproj",
    track_name: "myproj", repo_key: "/dev/myproj", repo_label: "myproj",
    branch: "master", cwd: "/dev/myproj",
    worktree_affiliation: aff("at-pin"), pane_id: "%1",
    focused_at: 0, acted_at: 0, ...over,
  });
  const ask = (over = {}) => ({
    id: "al1", kind: "need_human", target: "managed:0", ts: 1000,
    session: "managed", name: "claude", message: "which branch?", ...over,
  });

  it("marks a pane with an unanswered need_human, escalating with age", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [paneWin()];
    dismissedAlertIds.value = new Set();

    // Just asked -> quiet tier.
    vi.setSystemTime(1000 * 1000);
    alerts.value = [ask()];
    expect(render(<Rail />)).toContain("wait-fresh");

    // Over an hour later, same unanswered ask -> urgent.
    vi.setSystemTime((1000 + 3700) * 1000);
    expect(render(<Rail />)).toContain("wait-urgent");
  });

  it("drops the marker once the user has engaged the pane", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [paneWin({ acted_at: 2000 })];
    dismissedAlertIds.value = new Set();
    alerts.value = [ask({ ts: 1000 })];
    vi.setSystemTime(3000 * 1000);
    expect(render(<Rail />)).not.toContain("rail-await");
  });

  it("shows no marker when nothing has been asked", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [paneWin()];
    dismissedAlertIds.value = new Set();
    alerts.value = [];
    expect(render(<Rail />)).not.toContain("rail-await");
  });
});
