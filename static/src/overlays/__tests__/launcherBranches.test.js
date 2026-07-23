import { describe, expect, it } from "vitest";
import { pickerBranches, trackBranches } from "../LauncherModal.jsx";

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
