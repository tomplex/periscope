import { describe, expect, it } from "vitest";
import { mergeLiveAndPrefs, paneChip, reviewRowsByTrack, trackKind, trackLabel } from "../railTree.js";

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
describe("mergeLiveAndPrefs — track grouping", () => {
  it("groups windows under their track_id", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a" }),
      win({ pid: "b", track_id: "tk_b" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { trackOrder: [], tabsByTrack: {} });
    expect(new Set(m.trackOrder)).toEqual(new Set(["tk_a", "tk_b"]));
    expect(m.tabsByTrack.tk_a).toEqual(["a"]);
    expect(m.tabsByTrack.tk_b).toEqual(["b"]);
  });

  it("a window with a track_id but no explicit pref still groups", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_x" })], [], [], {});
    expect(m.trackOrder).toEqual(["tk_x"]);
    expect(m.tabsByTrack.tk_x).toEqual(["a"]);
  });

  it("a track on one branch lists its tabs flat", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "master" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    expect(m.rowsByTrack.tk_a).toEqual([{ pid: "a" }, { pid: "b" }]);
    expect(m.tabsByTrack.tk_a).toEqual(["a", "b"]);
  });

  it("clusters a shared branch where its first tab sits, and keeps a lone branch's tab flat", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "feat-x" }),
      win({ pid: "c", track_id: "tk_a", branch: "master" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    expect(m.rowsByTrack.tk_a).toEqual([{ branch: "master", pids: ["a", "c"] }, { pid: "b" }]);
    // tabsByTrack is the order the card lists, a cluster's tabs together
    expect(m.tabsByTrack.tk_a).toEqual(["a", "c", "b"]);
  });

  it("a drag inside a cluster keeps the cluster where it sits", () => {
    // Rail.reorderTabs splices tabsByTrack. Splicing a flat order where the
    // cluster's tabs were apart ([a, b, c]) moved the whole cluster below b.
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "feat-x" }),
      win({ pid: "c", track_id: "tk_a", branch: "master" }),
    ];
    const order = [...mergeLiveAndPrefs(wins, [], [], {}).tabsByTrack.tk_a];
    order.splice(order.indexOf("a"), 1);
    order.splice(order.indexOf("c") + 1, 0, "a");   // drop a below c
    const m = mergeLiveAndPrefs(wins, [], [], { tabsByTrack: { tk_a: order } });
    expect(m.rowsByTrack.tk_a).toEqual([{ branch: "master", pids: ["c", "a"] }, { pid: "b" }]);
  });

  it("a track where every tab has its own branch has no clusters", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "td/one" }),
      win({ pid: "b", track_id: "tk_a", branch: "td/two" }),
      win({ pid: "c", track_id: "tk_a", branch: "td/three" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    expect(m.rowsByTrack.tk_a).toEqual([{ pid: "a" }, { pid: "b" }, { pid: "c" }]);
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

  it("clusters honor the pref tab order", () => {
    // A drag inside a cluster writes tabsByTrack; building the cluster from
    // raw poll order snapped the row back on the next poll.
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: "master" }),
      win({ pid: "c", track_id: "tk_a", branch: "feat-x" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { tabsByTrack: { tk_a: ["c", "b", "a"] } });
    expect(m.rowsByTrack.tk_a).toEqual([{ pid: "c" }, { branch: "master", pids: ["b", "a"] }]);
  });

  it("a pane new since the last pref still lands in its cluster", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "c", track_id: "tk_a", branch: "feat-x" }),
      win({ pid: "new", track_id: "tk_a", branch: "master" }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], { tabsByTrack: { tk_a: ["a", "c"] } });
    expect(m.rowsByTrack.tk_a).toEqual([{ branch: "master", pids: ["a", "new"] }, { pid: "c" }]);
  });

  it("tabs without a branch never cluster", () => {
    const wins = [
      win({ pid: "a", track_id: "tk_a", branch: "master" }),
      win({ pid: "b", track_id: "tk_a", branch: undefined }),
      win({ pid: "c", track_id: "tk_a", branch: undefined }),
    ];
    const m = mergeLiveAndPrefs(wins, [], [], {});
    expect(m.rowsByTrack.tk_a).toEqual([{ pid: "a" }, { pid: "b" }, { pid: "c" }]);
  });
});

describe("mergeLiveAndPrefs — empty goal tracks (registry rows)", () => {
  const goal = { id: "tk_fresh", name: "Fresh goal", repo: "/dev/fdy" };
  const repoDefault = { id: "/dev/fdy", name: "fdy", repo: "/dev/fdy" };

  it("an empty goal track renders (appended after live tracks)", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_a" })], [], [], {}, [goal]);
    expect(m.trackOrder).toEqual(["tk_a", "tk_fresh"]);
    expect(m.tabsByTrack.tk_fresh).toEqual([]);      // no tabs yet
    expect(m.rowsByTrack.tk_fresh).toEqual([]);      // nothing to list
  });

  it("an empty repo-default track stays hidden (lazy catchall, id == repo)", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_a" })], [], [], {}, [repoDefault]);
    expect(m.trackOrder).toEqual(["tk_a"]);
  });

  it("an empty goal track keeps its pref position", () => {
    const m = mergeLiveAndPrefs(
      [win({ pid: "a", track_id: "tk_a" })], [], [],
      { trackOrder: ["tk_fresh", "tk_a"] }, [goal],
    );
    expect(m.trackOrder).toEqual(["tk_fresh", "tk_a"]);
  });

  it("a populated goal track isn't duplicated by its registry row", () => {
    const m = mergeLiveAndPrefs([win({ pid: "a", track_id: "tk_fresh" })], [], [], {}, [goal]);
    expect(m.trackOrder).toEqual(["tk_fresh"]);
    expect(m.tabsByTrack.tk_fresh).toEqual(["a"]);
  });

  it("trackLabel resolves an empty track's name from the registry", () => {
    expect(trackLabel("tk_fresh", [], [goal])).toBe("Fresh goal");
    // windows win over the registry when present
    expect(trackLabel("tk_fresh", [win({ track_id: "tk_fresh", track_name: "live name" })], [goal]))
      .toBe("live name");
    // no row, no window → basename fallback unchanged
    expect(trackLabel("/dev/myproj", [], [])).toBe("myproj");
  });

  it("trackKind separates a repo-default from a same-named goal track", () => {
    // The reported bug: a goal track named after its repo renders the SAME
    // label as that repo's default track, and both offered dissolve/teardown.
    const rows = [
      { id: "/dev/myproj", name: "myproj", repo: "/dev/myproj" },
      { id: "tk_myproj", name: "myproj", repo: "/dev/myproj" },
    ];
    expect(trackLabel("/dev/myproj", [], rows)).toBe(trackLabel("tk_myproj", [], rows));
    expect(trackKind("/dev/myproj", [], rows)).toBe("repo");
    expect(trackKind("tk_myproj", [], rows)).toBe("goal");
  });

  it("trackKind prefers the window's server-derived kind, then the registry", () => {
    expect(trackKind("tk_fresh", [win({ track_id: "tk_fresh", track_kind: "goal" })], [])).toBe("goal");
    expect(trackKind("tk_fresh", [], [goal])).toBe("goal");
    expect(trackKind("loose", [], [])).toBe("loose");
    // unknown id → "repo", so the rail hides the menu rather than offering
    // actions that refuse server-side
    expect(trackKind("/dev/unseen", [], [])).toBe("repo");
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

  it("no-repo (unpinned) pane in a git cwd → repo_label/branch", () => {
    const w = win({
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_label: "fdy", branch: "master", repo_key: "/dev/fdy",
    });
    expect(paneChip(w)).toBe("fdy/master");
  });
});

describe("reviewRowsByTrack", () => {
  const rows = [
    { repo: "myproj", branch: "feat/a", slug: "feat--a", walkthrough: { stops: 2 } },
    { repo: "myproj", branch: "feat/b", slug: "feat--b", walkthrough: null },
    { repo: "other", branch: "feat/a", slug: "feat--a", walkthrough: { stops: 1 } },
  ];
  const windowsByPid = {
    p1: win({ pid: "p1", track_id: "tk_a", branch: "feat/a" }),
    p2: win({ pid: "p2", track_id: "tk_b", branch: "feat/a" }),
    p3: win({ pid: "p3", track_id: "tk_a", branch: "feat/b" }),
  };
  it("places a row in the first track holding a window on that (repo, branch)", () => {
    const out = reviewRowsByTrack(["tk_a", "tk_b"], { tk_a: ["p1", "p3"], tk_b: ["p2"] }, windowsByPid, rows);
    expect(out).toEqual({ tk_a: { "feat/a": [rows[0]] } });
  });
  it("skips rows without a walkthrough and repos with no window", () => {
    const out = reviewRowsByTrack(["tk_b"], { tk_b: ["p2"] }, windowsByPid, rows);
    expect(out).toEqual({ tk_b: { "feat/a": [rows[0]] } });
  });
});
