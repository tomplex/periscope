// Render-path smoke test for <Rail> — the vitest equivalent of "does the
// rail actually paint" for the session-anchored tree. Pure-tree logic is
// covered in railTree.test.js; this catches wiring errors in Rail.jsx /
// RailRows.jsx (bad prop names, unscoped vars in the dev branch) that a
// build alone can't, since this repo browser-verifies instead of mounting
// components elsewhere. prefs stays at its unloaded default, so the
// syncRailPrefs side effect is guarded off (loaded:false write guard).
import { describe, it, expect } from "vitest";
import render from "preact-render-to-string";
import { windows, projects } from "../../store.js";
import { MAIN_KEY } from "../railTree.js";
import { Rail } from "../Rail.jsx";

const aff = (kind, label = null) => ({ kind, label });

describe("<Rail> render smoke", () => {
  it("renders project groups, chips, and the flat dev group", () => {
    projects.value = [
      { pinned_dir: MAIN_KEY, name: "main", tmux_session: "main", repo: null },
      { pinned_dir: "/dev/myproj", name: "myproj", tmux_session: "myproj", repo: "/dev/myproj" },
    ];
    windows.value = [
      // project pane, at pin — no chip
      { pid: "aa11", session: "myproj", index: 0, target: "myproj:0", name: "claude",
        is_claude: true, state: "working", project_pinned_dir: "/dev/myproj",
        repo_key: "/dev/myproj", repo_label: "myproj", branch: "master",
        cwd: "/dev/myproj", worktree_affiliation: aff("at-pin") },
      // project pane cd'd off-repo — chip from its own git fields
      { pid: "bb22", session: "myproj", index: 1, target: "myproj:1", name: "shell",
        is_claude: false, state: "shell", project_pinned_dir: "/dev/myproj",
        repo_key: "/dev/other", repo_label: "other", branch: "main",
        cwd: "/dev/other", worktree_affiliation: aff("off-repo", "other") },
      // main-session pane → dev, git cwd chip
      { pid: "cc33", session: "main", index: 0, target: "main:0", name: "fdy-work",
        is_claude: true, state: "idle", project_pinned_dir: MAIN_KEY,
        repo_key: "/dev/fdy", repo_label: "fdy", branch: "master",
        cwd: "/dev/fdy", worktree_affiliation: aff("no-repo") },
      // folded ad-hoc session pane → dev with session prefix
      { pid: "dd44", session: "scratch", index: 0, target: "scratch:0", name: "zsh",
        is_claude: false, state: "shell", project_pinned_dir: MAIN_KEY,
        repo_key: "", repo_label: "", branch: "",
        cwd: "/Users/tom/tmp", worktree_affiliation: aff("no-repo") },
    ];

    const html = render(<Rail />);

    expect(html).toContain("myproj");                 // repo group + project row
    expect(html).toContain("⧉ other/main");          // off-repo chip
    expect(html).toContain(">dev<");                  // dev group label, last
    expect(html).toContain("⧉ fdy/master");          // dev git chip
    expect(html).toContain("⧉ scratch: ~/tmp");      // folded-session prefix chip
    expect(html).toContain("review");                 // review row under project
    // dev renders after the project group (pinned bottom)
    expect(html.indexOf(">dev<")).toBeGreaterThan(html.indexOf("myproj"));
  });
});
