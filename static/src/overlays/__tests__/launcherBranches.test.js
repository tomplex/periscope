import { describe, expect, it } from "vitest";
import { filterBranches, launchTargets, pickerBranches, shortlistBranches, trackBranches } from "../LauncherModal.jsx";

// Pure branch-derivation helper for the per-track "+ New tab" launcher: it
// collects the distinct branches in a track, each with a representative
// worktree cwd. Windows without a branch are skipped.
describe("trackBranches", () => {
  it("collects distinct branches with a representative cwd", () => {
    const wins = [
      { track_id: "t1", branch: "main", cwd: "/repo" },
      { track_id: "t1", branch: "tc/feat", cwd: "/wt/feat" },
      { track_id: "t1", branch: "tc/feat", cwd: "/wt/feat-2" }, // dup branch
      { track_id: "other", branch: "x", cwd: "/x" },            // other track
    ];
    expect(trackBranches("t1", wins)).toEqual([
      { branch: "main", cwd: "/repo" },
      { branch: "tc/feat", cwd: "/wt/feat" }, // first cwd wins
    ]);
  });

  it("skips windows without a branch", () => {
    const wins = [
      { track_id: "t1", branch: "", cwd: "/repo" },
      { track_id: "t1", cwd: "/repo" },
      { track_id: "t1", branch: "main", cwd: "/repo" },
    ];
    expect(trackBranches("t1", wins)).toEqual([{ branch: "main", cwd: "/repo" }]);
  });

  it("returns [] for an unknown track or empty input", () => {
    expect(trackBranches("nope", [])).toEqual([]);
    expect(trackBranches("t1", null)).toEqual([]);
  });
});

// The picker spans the track's whole repo, not just what's running — the fix
// for "it's really hard to open tabs in things that aren't currently open".
describe("pickerBranches", () => {
  const cat = {
    worktrees: [
      { repo: "/r", branch: "main", path: "/r" },
      { repo: "/r", branch: "dormant", path: "/wt/dormant" },
      { repo: "/other", branch: "elsewhere", path: "/wt/elsewhere" },
    ],
    repos: [{ repo: "/r", branches: ["main", "dormant", "no-worktree"] }],
  };

  it("puts live branches first and badges them", () => {
    const wins = [{ track_id: "t1", branch: "main", cwd: "/repo" }];
    const out = pickerBranches("t1", wins, "/r", cat);
    expect(out[0]).toEqual({ branch: "main", cwd: "/repo", live: true });
  });

  it("offers worktrees that are not currently running", () => {
    const out = pickerBranches("t1", [], "/r", cat);
    expect(out).toContainEqual({ branch: "dormant", cwd: "/wt/dormant", live: false });
  });

  it("offers branches with no worktree, leaving cwd empty for the server", () => {
    const out = pickerBranches("t1", [], "/r", cat);
    expect(out).toContainEqual({ branch: "no-worktree", cwd: "", live: false });
  });

  it("never leaks another repo's worktrees into the track", () => {
    const out = pickerBranches("t1", [], "/r", cat);
    expect(out.map((b) => b.branch)).not.toContain("elsewhere");
  });

  it("does not duplicate a branch that is both live and in the catalog", () => {
    const wins = [{ track_id: "t1", branch: "dormant", cwd: "/wt/dormant" }];
    const out = pickerBranches("t1", wins, "/r", cat);
    expect(out.filter((b) => b.branch === "dormant")).toHaveLength(1);
  });

  it("falls back to live-only before the catalog loads", () => {
    const wins = [{ track_id: "t1", branch: "main", cwd: "/repo" }];
    expect(pickerBranches("t1", wins, "/r", null)).toEqual([
      { branch: "main", cwd: "/repo", live: true },
    ]);
  });

  it("returns live-only for a repo-less (loose) track", () => {
    expect(pickerBranches("t1", [], null, cat)).toEqual([]);
  });
});

// The launcher renders only a handful of branches at rest — a repo with 130
// branches previously rendered all of them as chips and filled the viewport.
describe("shortlistBranches", () => {
  const b = (branch, live = false) => ({ branch, cwd: "", live });
  // Input order is commit recency (the backend sorts by -committerdate).
  const all = [b("recent-1"), b("running", true), b("recent-2"), b("master"), b("old")];

  it("always gives the default branch the first slot, however stale", () => {
    expect(shortlistBranches(all, "master", 3).map((x) => x.branch)[0]).toBe("master");
  });

  it("ranks running branches above commit recency", () => {
    expect(shortlistBranches(all, "master", 3).map((x) => x.branch))
      .toEqual(["master", "running", "recent-1"]);
  });

  it("falls back to recency order when nothing is running", () => {
    const quiet = [b("r1"), b("r2"), b("main"), b("r3")];
    expect(shortlistBranches(quiet, "main", 3).map((x) => x.branch))
      .toEqual(["main", "r1", "r2"]);
  });

  it("never duplicates a branch that is both default and running", () => {
    const out = shortlistBranches([b("main", true), b("x")], "main", 5);
    expect(out.filter((x) => x.branch === "main")).toHaveLength(1);
  });

  it("tolerates a default branch that is not in the list", () => {
    expect(shortlistBranches([b("x"), b("y")], "master", 5).map((v) => v.branch))
      .toEqual(["x", "y"]);
  });

  it("returns everything when the list is under the limit", () => {
    expect(shortlistBranches([b("x")], "master", 5)).toHaveLength(1);
  });
});

describe("filterBranches", () => {
  const all = [
    { branch: "master" },
    { branch: "tc/attribute-qa-tooling" },
    { branch: "tc/anthology-build-flags" },
  ];

  it("is empty for an empty query (the shortlist renders instead)", () => {
    expect(filterBranches(all, "")).toEqual([]);
    expect(filterBranches(all, "   ")).toEqual([]);
  });

  it("matches terms in any order, across the / separator", () => {
    expect(filterBranches(all, "qa tool").map((b) => b.branch))
      .toEqual(["tc/attribute-qa-tooling"]);
    expect(filterBranches(all, "tc/anthology").map((b) => b.branch))
      .toEqual(["tc/anthology-build-flags"]);
  });

  it("is case-insensitive and requires every term", () => {
    expect(filterBranches(all, "MASTER").map((b) => b.branch)).toEqual(["master"]);
    expect(filterBranches(all, "qa nope")).toEqual([]);
  });
});

// Agent and command were separate sections; picking Codex REPLACED the command
// list rather than filtering it, so custom commands disappeared entirely.
describe("launchTargets", () => {
  it("offers both agents ahead of custom commands, Claude first", () => {
    const out = launchTargets([{ label: "shell", exec: "" }]);
    expect(out.map((t) => t.label)).toEqual(["Claude", "Codex", "shell"]);
  });

  it("keeps custom commands when Codex is available", () => {
    const out = launchTargets([{ label: "vim", exec: "vim" }]);
    expect(out.find((t) => t.label === "vim")).toMatchObject({ mode: "shell", exec: "vim" });
  });

  it("routes both agents through mode=agent so the server builds the argv", () => {
    const out = launchTargets([]);
    expect(out[0]).toMatchObject({ mode: "agent", agent: "claude" });
    expect(out[1]).toMatchObject({ mode: "agent", agent: "codex" });
  });

  it("drops a custom command that collides with a built-in agent", () => {
    const out = launchTargets([{ label: "claude", exec: "claude" }, { label: "Codex", exec: "codex" }]);
    expect(out.map((t) => t.label)).toEqual(["Claude", "Codex"]);
  });

  it("works with no commands configured", () => {
    expect(launchTargets(null).map((t) => t.label)).toEqual(["Claude", "Codex"]);
  });
});
