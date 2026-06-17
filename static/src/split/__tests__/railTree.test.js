import { describe, it, expect } from "vitest";
import {
  MAIN_KEY, groupKeyForWindow, mergeLiveAndPrefs, indexProjects,
  projectLabel, groupLabel, paneChip,
} from "../railTree.js";

// Window factory. project_pinned_dir simulates the server-side resolver
// (session-anchored); repo_key/branch/cwd are the cwd-derived display fields.
const win = (over = {}) => ({
  pid: "p1", session: "myproj", project_pinned_dir: "/dev/myproj",
  repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
  cwd: "/dev/myproj", state: "idle",
  worktree_affiliation: { kind: "at-pin", label: null },
  ...over,
});
const proj = (over = {}) => ({
  pinned_dir: "/dev/myproj", name: "myproj", tmux_session: "myproj",
  repo: "/dev/myproj", base_branch: null, ...over,
});

const MAIN_PROJ = { pinned_dir: MAIN_KEY, name: "main", tmux_session: "main", repo: null };

describe("groupKeyForWindow", () => {
  const byPin = indexProjects([proj(), MAIN_PROJ]);

  it("groups by the project row's repo, not the window's cwd repo", () => {
    const w = win({ repo_key: "/dev/elsewhere", cwd: "/dev/elsewhere" }); // cd'd away
    expect(groupKeyForWindow(w, byPin)).toBe("/dev/myproj");
  });

  it("folds missing project_pinned_dir to MAIN_KEY", () => {
    expect(groupKeyForWindow(win({ project_pinned_dir: null }), byPin)).toBe(MAIN_KEY);
    expect(groupKeyForWindow(win({ project_pinned_dir: undefined }), byPin)).toBe(MAIN_KEY);
  });

  it("folds MAIN_KEY pinned_dir to MAIN_KEY", () => {
    expect(groupKeyForWindow(win({ project_pinned_dir: MAIN_KEY }), byPin)).toBe(MAIN_KEY);
  });

  it("folds no-row pins (archived / delete race) to MAIN_KEY", () => {
    expect(groupKeyForWindow(win({ project_pinned_dir: "/dev/ghost" }), byPin)).toBe(MAIN_KEY);
  });

  it("a null-repo project is its own top-level group keyed by pinned_dir", () => {
    const byPin2 = indexProjects([proj({ pinned_dir: "/notes", repo: null, tmux_session: "notes" })]);
    expect(groupKeyForWindow(win({ project_pinned_dir: "/notes" }), byPin2)).toBe("/notes");
  });
});

describe("mergeLiveAndPrefs", () => {
  const projects = [
    proj(),
    proj({ pinned_dir: "/dev/wt/feat", tmux_session: "feat", repo: "/dev/myproj", base_branch: "feat-x" }),
    MAIN_PROJ,
  ];

  it("two projects of one repo share a repo group; sessions are the sub-rows", () => {
    const ws = [
      win({ pid: "a", session: "myproj" }),
      win({ pid: "b", session: "feat", project_pinned_dir: "/dev/wt/feat" }),
    ];
    const m = mergeLiveAndPrefs(ws, projects, [], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj"]);
    expect(m.worktreesByRepo["/dev/myproj"]).toEqual(["myproj", "feat"]);
  });

  it("a cd'd-away pane stays in its project group", () => {
    const ws = [win({ pid: "a", repo_key: "/dev/other", cwd: "/dev/other" })];
    const m = mergeLiveAndPrefs(ws, projects, [], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj"]);
    expect(m.panesByWorktree["myproj"]).toEqual(["a", "review"]);
  });

  it("dev windows land in a flat panesByWorktree[MAIN_KEY], no review sentinel, dev last", () => {
    const ws = [
      win({ pid: "a" }),
      win({ pid: "x", session: "main", project_pinned_dir: MAIN_KEY }),
      win({ pid: "y", session: "scratch", project_pinned_dir: MAIN_KEY }),
    ];
    const m = mergeLiveAndPrefs(ws, projects, [], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj", MAIN_KEY]);
    expect(m.worktreesByRepo[MAIN_KEY]).toEqual([]);
    expect(m.panesByWorktree[MAIN_KEY]).toEqual(["x", "y"]);
  });

  it("no dev group when no dev windows", () => {
    const m = mergeLiveAndPrefs([win()], projects, [], {}, {});
    expect(m.repoOrder).not.toContain(MAIN_KEY);
  });

  it("null-repo project sessions get no review sentinel", () => {
    const notesProjects = [proj({ pinned_dir: "/notes", repo: null, tmux_session: "notes", name: "Notes" })];
    const ws = [win({ pid: "n1", session: "notes", project_pinned_dir: "/notes" })];
    const m = mergeLiveAndPrefs(ws, notesProjects, [], {}, {});
    expect(m.repoOrder).toEqual(["/notes"]);
    expect(m.panesByWorktree["notes"]).toEqual(["n1"]);  // no "review"
  });

  it("the bridge session renders as its own 'bridge' group, not folded into dev", () => {
    const bridgeProjects = [proj({ pinned_dir: "/Users/tom", repo: null, tmux_session: "bridge", name: "bridge" })];
    const ws = [win({ pid: "fm", session: "bridge", project_pinned_dir: "/Users/tom" })];
    const m = mergeLiveAndPrefs(ws, bridgeProjects, [], {}, {});
    expect(m.repoOrder).toEqual(["/Users/tom"]);                 // own group, not MAIN_KEY
    expect(m.worktreesByRepo["/Users/tom"]).toEqual(["bridge"]);
    expect(m.panesByWorktree["bridge"]).toEqual(["fm"]);         // the first-mate pane, no review row
    expect(groupLabel("/Users/tom", indexProjects(bridgeProjects))).toBe("bridge");
  });

  it("dev pane order persists via prefs panes_by_worktree[MAIN_KEY]", () => {
    const ws = [
      win({ pid: "x", session: "main", project_pinned_dir: MAIN_KEY }),
      win({ pid: "y", session: "scratch", project_pinned_dir: MAIN_KEY }),
    ];
    const m = mergeLiveAndPrefs(ws, projects, [], {}, { [MAIN_KEY]: ["y"] });
    expect(m.panesByWorktree[MAIN_KEY]).toEqual(["y", "x"]);  // pref first, new appended
  });

  it("stale repo_order pref keys (old cwd-repo paths) are dropped", () => {
    const m = mergeLiveAndPrefs([win()], projects, ["/old/cwd/key", "/dev/myproj"], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj"]);
  });

  it("MAIN_KEY in repo_order prefs never floats above real repos", () => {
    const ws = [
      win({ pid: "a" }),
      win({ pid: "x", session: "main", project_pinned_dir: MAIN_KEY }),
    ];
    const m = mergeLiveAndPrefs(ws, projects, [MAIN_KEY, "/dev/myproj"], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj", MAIN_KEY]);
  });
});

describe("labels", () => {
  it("projectLabel: name, then base_branch, then session", () => {
    expect(projectLabel(proj({ name: "nice" }), "s")).toBe("nice");
    expect(projectLabel(proj({ name: "", base_branch: "feat-x" }), "s")).toBe("feat-x");
    expect(projectLabel(undefined, "sess")).toBe("sess");
  });

  it("groupLabel: dev for MAIN_KEY, name for null-repo own group, basename otherwise", () => {
    const byPin = indexProjects([proj({ pinned_dir: "/notes", repo: null, name: "Notes" })]);
    expect(groupLabel(MAIN_KEY, byPin)).toBe("dev");
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
      worktree_affiliation: { kind: "off-repo", label: "static" },  // basename(cwd) — unused
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
      project_pinned_dir: MAIN_KEY,
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_label: "fdy", branch: "master", repo_key: "/dev/fdy",
    });
    expect(paneChip(w, { isDev: true })).toBe("fdy/master");
  });

  it("dev pane in a non-git cwd → ~-relative cwd", () => {
    const w = win({
      project_pinned_dir: MAIN_KEY,
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_key: "", repo_label: "", branch: "", cwd: "/Users/tom/Downloads",
    });
    expect(paneChip(w, { isDev: true })).toBe("~/Downloads");
  });

  it("folded ad-hoc session gets its session name as prefix", () => {
    const w = win({
      project_pinned_dir: MAIN_KEY, session: "scratch",
      worktree_affiliation: { kind: "no-repo", label: null },
      repo_label: "fdy", branch: "master", repo_key: "/dev/fdy",
    });
    expect(paneChip(w, { isDev: true, sessionPrefix: "scratch" })).toBe("scratch: fdy/master");
  });
});
