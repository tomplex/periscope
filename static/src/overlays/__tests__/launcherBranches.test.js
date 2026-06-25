import { describe, expect, it } from "vitest";
import { trackBranches } from "../LauncherModal.jsx";

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
