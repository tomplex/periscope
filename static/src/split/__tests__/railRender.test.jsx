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
import { PaneHeader } from "../Detail.jsx";
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
        agent: "claude", state: "working", track_id: "/dev/myproj", track_name: "myproj",
        repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
        cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"), pane_id: "%1" },
      { pid: "bb22", session: "managed", index: 1, target: "managed:1", name: "shell",
        agent: null, state: "shell", track_id: "/dev/myproj", track_name: "myproj",
        repo_key: "/dev/other", repo_label: "other", branch: "master",
        cwd: "/dev/other", worktree_affiliation: aff("off-repo", "other"), pane_id: "%2" },
      // a second track with two distinct branches → branch sub-clusters
      { pid: "cc33", session: "managed", index: 2, target: "managed:2", name: "feat-work",
        agent: "claude", state: "idle", track_id: "tk_feature", track_name: "Feature",
        repo_key: "/dev/fdy", repo_label: "fdy", branch: "master",
        cwd: "/dev/fdy", worktree_affiliation: aff("no-repo"), pane_id: "%3" },
      { pid: "dd44", session: "managed", index: 3, target: "managed:3", name: "feat-x",
        agent: "claude", state: "idle", track_id: "tk_feature", track_name: "Feature",
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
    expect(html).toContain('<span class="rail-icon icon-claude" title="Claude">✻</span>');
  });

  it("shows distinct provider symbols on Claude and Codex panes", () => {
    projects.value = [];
    tracks.value = [];
    const pane = (over) => ({
      pid: "agent", session: "managed", index: 0, target: "managed:0",
      state: "idle", track_id: "/dev/myproj", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"),
      pane_id: "%1", ...over,
    });
    windows.value = [
      pane({ pid: "claude", name: "claude", agent: "claude" }),
      pane({ pid: "codex", index: 1, target: "managed:1", name: "codex",
        agent: "codex", pane_id: "%2" }),
    ];

    const html = render(<Rail />);
    expect(html).toContain('<span class="rail-icon icon-claude" title="Claude">✻</span>');
    expect(html).toContain('<span class="rail-icon icon-codex" title="Codex">⬡</span>');
  });

  it("chips the account only on panes off the default subscription", () => {
    projects.value = [];
    tracks.value = [];
    const pane = (over) => ({
      pid: "p", session: "managed", index: 0, target: "managed:0", name: "claude",
      agent: "claude", state: "idle", track_id: "/dev/myproj", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"),
      pane_id: "%1", ...over,
    });
    windows.value = [
      pane({ pid: "onb", account: "b" }),
      pane({ pid: "ondefault", index: 1, target: "managed:1", account: "default", pane_id: "%2" }),
    ];

    const html = render(<Rail />);
    expect(html).toContain('class="rail-account"');
    expect(html.split('class="rail-account"').length - 1).toBe(1);
    expect(html).toContain("@b");
  });

  it("chips the wrapper profile only on panes off the default profile", () => {
    projects.value = [];
    tracks.value = [];
    const pane = (over) => ({
      pid: "p", session: "managed", index: 0, target: "managed:0", name: "claude",
      agent: "claude", state: "idle", track_id: "/dev/myproj", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"),
      pane_id: "%1", ...over,
    });
    windows.value = [
      pane({ pid: "lab", profile: "lab" }),
      pane({ pid: "normal", index: 1, target: "managed:1", profile: "default", pane_id: "%2" }),
      // A pre-profiles server stamps no field at all — must read as normal,
      // not as an unlabeled chip.
      pane({ pid: "legacy", index: 2, target: "managed:2", pane_id: "%3" }),
    ];

    const html = render(<Rail />);
    expect(html.split('class="rail-profile"').length - 1).toBe(1);
    expect(html).toContain(">lab</span>");
  });

  it("offers the move-account action on Claude panes only, labeled with its destination", () => {
    projects.value = [];
    tracks.value = [];
    const pane = (over) => ({
      pid: "p", session: "managed", index: 0, target: "managed:0", name: "claude",
      agent: "claude", state: "idle", track_id: "/dev/myproj", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"),
      pane_id: "%1", ...over,
    });
    windows.value = [
      pane({ pid: "onA", account: "default" }),
      pane({ pid: "onB", index: 1, target: "managed:1", account: "b", pane_id: "%2" }),
      pane({ pid: "sh", index: 2, target: "managed:2", agent: null, pane_id: "%3" }),
    ];

    const html = render(<Rail />);

    expect(html.split("rail-move-acct").length - 1).toBe(2);  // not on the shell pane
    expect(html).toContain("→ B");   // the A pane's destination
    expect(html).toContain("→ A");   // the B pane's destination
  });

  it("keeps the Claude symbol during a frontend-only rolling reload", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [{
      pid: "legacy", session: "managed", index: 0, target: "managed:0",
      name: "legacy-claude", is_claude: true, state: "idle",
      track_id: "/dev/myproj", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"), pane_id: "%1",
    }];

    expect(render(<Rail />))
      .toContain('<span class="rail-icon icon-claude" title="Claude">✻</span>');
  });

  it("renders an EMPTY goal track from the registry with its + New tab", () => {
    projects.value = [];
    tracks.value = [{ id: "tk_fresh", name: "Fresh goal", repo: "/dev/fdy" }];
    windows.value = [
      { pid: "aa11", session: "managed", index: 0, target: "managed:0", name: "claude",
        agent: "claude", state: "working", track_id: "/dev/myproj", track_name: "myproj",
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
      agent: "claude", state: "idle", track_name: "sts2-seed-finder",
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
    name: "claude", agent: "claude", state: "idle", track_id: "/dev/myproj",
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

describe("delegation lineage placement", () => {
  const paneWin = (over = {}) => ({
    session: "managed", agent: "claude", state: "idle",
    track_id: "/dev/myproj", track_name: "myproj", repo_key: "/dev/myproj",
    repo_label: "myproj", branch: "master", cwd: "/dev/myproj",
    worktree_affiliation: aff("at-pin"), ...over,
  });

  it("the rail card carries no lineage chip — it lives in the detail header", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [
      paneWin({ pid: "lead1", index: 0, target: "managed:0", name: "world-model-driver", pane_id: "%1" }),
      paneWin({ pid: "work1", index: 1, target: "managed:1", name: "porter", pane_id: "%2",
                spawned_by: "lead1", spawner_name: "world-model-driver" }),
    ];
    const html = render(<Rail />);
    expect(html).not.toContain("lineage");
  });

  it("names the spawner in the delegated pane's header, above the status line", () => {
    windows.value = [
      paneWin({ pid: "lead1", index: 0, target: "managed:0", name: "world-model-driver", pane_id: "%1" }),
    ];
    const w = paneWin({ pid: "work1", index: 1, target: "managed:1", name: "porter", pane_id: "%2",
                        spawned_by: "lead1", spawner_name: "world-model-driver",
                        status_line: "porting the mover" });
    const html = render(<PaneHeader w={w} mode="terminal" onMode={() => {}} />);
    expect(html).toContain("header-lineage");
    expect(html).toContain("↳ launched by world-model-driver");
    expect(html).not.toContain("lineage-gone");   // live lead stays clickable
    // lineage precedes the status summary in flow (both are full-width lines)
    expect(html.indexOf("header-lineage")).toBeLessThan(html.indexOf("header-status"));
  });

  it("still names a spawner that has exited, marked inert", () => {
    // Leads exit: the only real lineage on the dev box was a 3-link chain
    // whose MIDDLE pane was already dead. Hiding it then erases exactly
    // the long chain this feature exists to show.
    windows.value = [];
    const w = paneWin({ pid: "work1", index: 1, target: "managed:1", name: "porter", pane_id: "%2",
                        spawned_by: "gone", spawner_name: "model-migration" });
    const html = render(<PaneHeader w={w} mode="terminal" onMode={() => {}} />);
    expect(html).toContain("↳ launched by model-migration");
    expect(html).toContain("lineage-gone");
  });

  it("shows no lineage line on a hand-created pane's header", () => {
    windows.value = [];
    const w = paneWin({ pid: "solo1", index: 0, target: "managed:0", name: "claude", pane_id: "%1" });
    expect(render(<PaneHeader w={w} mode="terminal" onMode={() => {}} />)).not.toContain("lineage");
  });
});

describe("<Rail> context chip", () => {
  it("paints the context chip from tokens when the status line did not parse", () => {
    projects.value = [];
    tracks.value = [];
    windows.value = [{
      pid: "p1", session: "managed", index: 0, target: "managed:0", name: "worker",
      agent: "claude", state: "idle", track_id: "t1", track_name: "myproj",
      repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
      cwd: "/dev/myproj", worktree_affiliation: aff("at-pin"), pane_id: "%1",
      context_pct: null, ctx_tokens: 600000, ctx_class: "hot", ctx_hint: "clearing pays for itself",
    }];
    const html = render(<Rail />);
    expect(html).toContain("600k");
    expect(html).toContain("ctx-hot");
    expect(html).toContain("clearing pays for itself");
  });
});

import { ctxClass, ctxChipText } from "../RailRows.jsx";

describe("ctxClass", () => {
  it("uses the server band when present", () => {
    expect(ctxClass({ ctx_class: "hot" })).toBe(" ctx-hot");
    expect(ctxClass({ ctx_class: "warn" })).toBe(" ctx-warn");
  });

  it("falls through to the percent band when cost pressure has nothing to say", () => {
    // "none" means cost pressure is silent, not that the pane should paint
    // plain — the auto-compact warning still applies at 95%.
    expect(ctxClass({ ctx_class: "none", context_pct: 95 })).toBe(" ctx-hot");
  });

  it("lets cost pressure win when it has something to say", () => {
    expect(ctxClass({ ctx_class: "warn", context_pct: 95 })).toBe(" ctx-warn");
  });

  it("falls back to the percent bands when the server said nothing", () => {
    expect(ctxClass({ context_pct: 95 })).toBe(" ctx-hot");
    expect(ctxClass({ context_pct: 65 })).toBe(" ctx-warn");
    expect(ctxClass({ context_pct: 10 })).toBe("");
    expect(ctxClass({})).toBe("");
  });
});

describe("ctxChipText", () => {
  it("prefers the percentage when the status line parsed", () => {
    expect(ctxChipText({ context_pct: 72, ctx_tokens: 600000 })).toBe("72%");
  });

  it("falls back to tokens when the status line did not parse", () => {
    expect(ctxChipText({ context_pct: null, ctx_tokens: 600000 })).toBe("600k");
  });

  it("returns null when there is nothing to show", () => {
    expect(ctxChipText({})).toBe(null);
  });
});
