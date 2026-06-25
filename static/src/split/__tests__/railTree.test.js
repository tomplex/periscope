import { describe, expect, it } from "vitest";
import {
  groupLabel, indexProjects, mergeLiveAndPrefs, paneChip, projectLabel,
} from "../railTree.js";

// Window factory. `track_id` is the backend-resolved grouping authority
// (always present — repo-default fallback guarantees a value). `branch` is
// the mid-tier derivation key (also backend-supplied). repo_key/cwd are the
// cwd-derived display fields used only by paneChip.
const win = (over = {}) => ({
  pid: "p1", session: "managed", track_id: "tk_a", branch: "master",
  repo_key: "/dev/myproj", repo_label: "myproj",
  cwd: "/dev/myproj", state: "idle",
  worktree_affiliation: { kind: "at-pin", label: null },
  ...over,
});
const proj = (over = {}) => ({
  pinned_dir: "/dev/myproj", name: "myproj", tmux_session: "myproj",
  repo: "/dev/myproj", base_branch: null, ...over,
});

describe("mergeLiveAndPrefs — track grouping", () => {
  it("groups windows under their track_id", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a" }),
      win({ pid: "b", track_id: "tk_b" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { trackOrder: [], tabsByTrack: {}, branchOrderByTrack: {} });
    expect(new Set(m.trackOrder)).toEqual(new Set(["tk_a", "tk_b"]));
    expect(m.tabsByTrack.tk_a).toEqual(["a"]);
    expect(m.tabsByTrack.tk_b).toEqual(["b"]);
  });

  it("a window with a track_id but no explicit pref still groups", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_x" })], [], [], {});
    expect(m.trackOrder).toEqual(["tk_x"]);
    expect(m.tabsByTrack.tk_x).toEqual(["a"]);
  });

  it("a track with one branch renders flat (no branch sub-clusters)", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "master" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    expect(m.branchesByTrack.tk_a).toEqual([]);          // flat marker
    expect(m.tabsByTrack.tk_a).toEqual(["a", "b"]);
    expect(m.tabsByBranch.tk_a).toBeUndefined();
  });

  it("a track with two distinct branches emits two sub-clusters", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "feat-x" }),
      win({ pid: "c", track_id: "tk_a", branch: "master" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    // first-seen branch order
    expect(m.branchesByTrack.tk_a).toEqual(["master", "feat-x"]);
    expect(m.tabsByBranch.tk_a.master).toEqual(["a", "c"]);
    expect(m.tabsByBranch.tk_a["feat-x"]).toEqual(["b"]);
    // tabsByTrack stays the flat all-tabs order regardless
    expect(m.tabsByTrack.tk_a).toEqual(["a", "b", "c"]);
  });

  it("track order is honored from prefs; live-new tracks append", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a" }),
      win({ pid: "b", track_id: "tk_b" }),
      win({ pid: "c", track_id: "tk_new" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { trackOrder: ["tk_b", "tk_a"] });
    expect(m.trackOrder).toEqual(["tk_b", "tk_a", "tk_new"]);
  });

  it("stale pref track ids (no live window) are dropped", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_a" })], [], [], {
      trackOrder: ["tk_gone", "tk_a"],
    });
    expect(m.trackOrder).toEqual(["tk_a"]);
  });

  it("tab order within a track is honored from prefs", () => {
    const wins = [
      win({ pid: "x", track_id: "tk_a" }),
      win({ pid: "y", track_id: "tk_a" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { tabsByTrack: { tk_a: ["y"] } });
    expect(m.tabsByTrack.tk_a).toEqual(["y", "x"]);  // pref first, new appended
  });

  it("branch order within a track is honored from prefs", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "feat-x" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { branchOrderByTrack: { tk_a: ["feat-x"] } });
    expect(m.branchesByTrack.tk_a).toEqual(["feat-x", "master"]);  // pref first
  });

  it("a window missing branch is bucketed without crashing", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: undefined }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    // two distinct branch buckets ("master" + the empty fallback) → sub-clusters
    expect(m.branchesByTrack.tk_a.length).toBe(2);
    expect(m.tabsByTrack.tk_a).toEqual(["a", "b"]);
  });
});

describe("labels", () => {
  it("projectLabel: name, then base_branch, then session", () => {
    expect(projectLabel(proj({ name: "nice" }), "s")).toBe("nice");
    expect(projectLabel(proj({ name: "", base_branch: "feat-x" }), "s")).toBe("feat-x");
    expect(projectLabel(undefined, "sess")).toBe("sess");
  });

  it("groupLabel: name for null-repo own group, basename otherwise", () => {
    const byPin = indexProjects([proj({ pinned_dir: "/notes", repo: null, name: "Notes" })]);
    expect(groupLabel("/notes", byPin)).toBe("Notes");
    expect(groupLabel("/dev/myproj", byPin)).toBe("myproj");
  });
});

describe("paneChip", () => {
  it("at-pin → no chip", () => {
    expect(paneChip(win())).toBe(null);
  });

  it("sibling → the sibling worktree's branch from aff.label", () => {
    const w = win({ worktree_affiliation: { kind: "sibling", label: "feat-x" } });
    expect(paneChip(w)).toBe("feat-x");
  });

  it("off-repo → repo_label/branch from the window's own git fields", () => {
    const w = win({
      worktree_affiliation: { kind: "off-repo", label: "static" },
      repo_key: "/dev/periscope", repo_label: "periscope", branch: "main",
    });
    expect(paneChip(w)).toBe("periscope/main");
  });

  it("off-repo into a non-git dir → ~-relative cwd", () => {
    const w = win({
      worktree_affiliation: { kind: "off-repo", label: "x" },
      repo_key: "", repo_label: "", branch: "", cwd: "/Users/tom/tmp/x",
    });
    expect(paneChip(w)).toBe("~/tmp/x");
  });

  it("dev pane in a git cwd → repo_label/branch", () => {
    const w = win({
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_label: "fdy", branch: "master", repo_key: "/dev/fdy",
    });
    expect(paneChip(w, { isDev: true })).toBe("fdy/master");
  });

  it("folded ad-hoc session gets its session name as prefix", () => {
    const w = win({
      session: "scratch",
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_label: "fdy", branch: "master", repo_key: "/dev/fdy",
    });
    expect(paneChip(w, { isDev: true, sessionPrefix: "scratch" })).toBe("scratch: fdy/master");
  });
});
