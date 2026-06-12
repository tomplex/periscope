# Session-Anchored Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rail membership anchored to tmux-session→project (stable across cd); cwd becomes an affiliation chip; "Other" is replaced by a flat "dev" catch-all (`__main__`).

**Architecture:** Re-key the pure rail merge (`railTree.js`) from cwd-derived `repo_key` to the project model already shipped in `/api/state` (`project_pinned_dir` per window + the `projects` list). Server gets a 3-line resolver fold (unknown session → `MAIN_KEY`) and a `~/dev` + auto-create tweak in `/api/window/new`. No new modules.

**Tech Stack:** Preact + @preact/signals, vitest (`npm test`), FastAPI + pytest (`uv run pytest -q`).

**Reference docs:** spec `docs/superpowers/specs/2026-06-12-session-anchored-rail-design.md`, structure `docs/superpowers/specs/2026-06-12-session-anchored-rail-structure.md`.

---

## Task 1: Server — fold unknown sessions to `MAIN_KEY`

**Files:**
- Create: `tests/test_projects.py`
- Modify: `periscope/projects.py:144-157` (`resolve_project_for_window`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projects.py`. **`clean_state` is NOT autouse** (`tests/conftest.py:54` — plain fixture); without it these tests would write fake project rows into the real `~/.config/periscope/state.json`. Wrap it in a local autouse fixture (the `tests/routes/test_projects.py:8` pattern).

```python
"""Direct tests for periscope/projects.py (resolve_project_for_window).

CLAUDE.md flags projects.py as indirectly-covered-only; this starts the
direct mirror file. Route-level behavior stays in tests/routes/test_projects.py.
"""

import pytest

from periscope.projects import (
    MAIN_KEY,
    archive_project,
    create_project,
    resolve_project_for_window,
)


@pytest.fixture(autouse=True)
def _state(clean_state):
    # Isolation: without this, create_project persists into the REAL
    # state.json (clean_state is not autouse in tests/conftest.py).
    return clean_state


def test_resolve_matched_session_returns_pinned_dir():
    create_project("/Users/foo/dev/myproj", name="myproj", tmux_session="myproj")
    assert resolve_project_for_window({"session": "myproj"}) == "/Users/foo/dev/myproj"


def test_resolve_unknown_session_folds_to_main():
    # The fold rule: every non-empty session resolves to SOMETHING.
    assert resolve_project_for_window({"session": "adhoc-scratch"}) == MAIN_KEY


def test_resolve_empty_session_returns_none():
    assert resolve_project_for_window({"session": ""}) is None
    assert resolve_project_for_window({}) is None


def test_resolve_archived_project_still_matches():
    # Archived rows still resolve (the frontend folds them to dev via the
    # no-row-in-projects_view fallback; the resolver itself doesn't filter).
    create_project("/Users/foo/dev/oldproj", name="old", tmux_session="oldproj")
    archive_project("/Users/foo/dev/oldproj")
    assert resolve_project_for_window({"session": "oldproj"}) == "/Users/foo/dev/oldproj"
```

- [ ] **Step 2: Run to verify the fold test fails**

Run: `uv run pytest tests/test_projects.py -q`
Expected: `test_resolve_unknown_session_folds_to_main` FAILS (returns `None`); the other three PASS (current behavior).

- [ ] **Step 3: Implement the fold**

In `periscope/projects.py`, replace `resolve_project_for_window`:

```python
def resolve_project_for_window(window: dict) -> Optional[str]:
    """Map a tmux window (with `session` field) to its owning project key.

    Returns the pinned_dir key for a session owned by a project, MAIN_KEY
    for everything else (the fold-to-dev rule: unmanaged sessions belong
    to main). Only an empty/missing session returns None. Lookup is by
    `tmux_session` match; archived rows still match — the frontend folds
    them to dev via its no-row fallback.
    """
    session = window.get("session", "")
    if not session:
        return None
    with _store._STATE_LOCK:
        for key, row in _store._STATE.get("projects", {}).items():
            if row.get("tmux_session") == session:
                return key
    return MAIN_KEY
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. If `tests/test_window_view.py` or `tests/routes/` fail on a `project_pinned_dir is None` assertion for unmanaged windows, update that assertion to `== "__main__"` (grep confirmed no such assertions exist today, but the suite is the oracle).

- [ ] **Step 5: Commit**

```bash
git add periscope/projects.py tests/test_projects.py
git commit -m "feat(projects): resolve_project_for_window folds unknown sessions to MAIN_KEY"
```

## Task 2: Server — `/api/window/new` dev cwd + session auto-create

**Files:**
- Modify: `periscope/routes/sessions.py:203-234` (`_window_new_plain`)
- Modify: `tests/routes/test_sessions.py` (add 2 tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/routes/test_sessions.py` after `test_window_new_archived_project_falls_through_to_cwd` (reuse the `_patch` helper defined at the top of that file):

```python
def test_window_new_main_key_defaults_to_dev_dir(client, mocker):
    # MAIN_KEY (dev / folded unmanaged sessions) → ~/dev, not pane-cwd.
    import os
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    _patch(mocker, "get_project", return_value={"name": "main", "tmux_session": "main"})
    _patch(mocker, "tmux", return_value="/tmp")          # pane cwd must NOT win
    _patch(mocker, "_run", return_value=(0, ""))          # has-session: exists
    new_window = _patch(mocker, "_tmux_mutate", return_value=(True, "3"))
    r = client.post("/api/window/new?session=main&mode=shell")
    assert r.status_code == 200
    call = next(c for c in new_window.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == os.path.expanduser("~/dev")


def test_window_new_auto_creates_missing_session(client, mocker):
    # Dev's "+ New tab" can target a dead "main" session — auto-create it
    # instead of letting new-window 500.
    import os
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    _patch(mocker, "get_project", return_value={"name": "main", "tmux_session": "main"})
    _patch(mocker, "tmux", return_value="")
    _patch(mocker, "_run", return_value=(1, ""))          # has-session: missing
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "1"))
    r = client.post("/api/window/new?session=main&mode=shell")
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "main:1"
    new_session = next(c for c in mutate.call_args_list if c.args[0] == "new-session")
    cwd_idx = list(new_session.args).index("-c") + 1
    assert new_session.args[cwd_idx] == os.path.expanduser("~/dev")
    # No new-window after creating the session — new-session's window IS the tab.
    assert not any(c.args[0] == "new-window" for c in mutate.call_args_list)


def test_window_new_no_auto_create_for_project_sessions(client, mocker):
    # Auto-create is gated on MAIN_KEY: a typo'd session= on a project
    # call must error, not silently mint a session.
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj", "archived_at": None,
    })
    _patch(mocker, "_run", return_value=(1, ""))          # has-session: missing
    mutate = _patch(mocker, "_tmux_mutate", return_value=(False, "no such session"))
    r = client.post("/api/window/new?session=myproj-typo&mode=shell")
    assert r.status_code == 500
    assert not any(c.args[0] == "new-session" for c in mutate.call_args_list)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/routes/test_sessions.py -q -k "main_key_defaults or auto_creates"`
Expected: both FAIL (first: cwd is `/tmp`; second: 500 or new-window called).

- [ ] **Step 3: Implement**

Replace the body of `_window_new_plain` (`periscope/routes/sessions.py:203-234`):

```python
def _window_new_plain(session: str, exec_cmd: str, mode: str) -> dict:
    """Non-resume window spawn: resolve cwd, open a new window in `session`,
    optionally run `exec_cmd`."""
    # Project pin always wins over active-pane cwd. If the target session
    # is owned by a non-archived non-main project, new tabs land in the
    # project's pinned_dir — even if the user has cd'd away in the active
    # pane. MAIN_KEY (dev, incl. folded unmanaged sessions) lands in ~/dev;
    # pane-cwd inheritance survives only for archived-project sessions.
    project_key = resolve_project_for_window({"session": session})
    project = get_project(project_key) if project_key else {}
    if project_key == MAIN_KEY:
        cwd = os.path.expanduser("~/dev")
    elif project_key and not project.get("archived_at"):
        cwd = project_key  # the projects dict's key IS the pinned_dir path
        # (see _lookup_key in periscope/projects.py for the realpath normalization).
    else:
        cwd = tmux(
            "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
        ).strip() or os.path.expanduser("~")

    # Dev's "+ New tab" can target __main__'s tmux_session while it doesn't
    # exist (dev can be populated purely by folded ad-hoc sessions). Create
    # it instead of letting `new-window -t` error. Gated on MAIN_KEY so a
    # typo'd session= on any other call still errors instead of silently
    # minting a session. Same -P -F rationale as _window_new_resume:
    # base-index 1 makes hardcoded :0 targets no-op.
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0 and project_key == MAIN_KEY:
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", session, "-c", cwd,
            "-P", "-F", "#{window_index}",
        )
        if not ok:
            raise HTTPException(500, f"failed to create session '{session}': {msg}")
    else:
        ok, msg = _tmux_mutate(
            "new-window", "-t", f"{session}:", "-c", cwd,
            "-P", "-F", "#{window_index}",
        )
        if not ok:
            raise HTTPException(500, msg)
    try:
        index = int(msg)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {msg!r}")
    target = f"{session}:{index}"

    cmd = exec_cmd.strip()
    _send_and_stamp(target, cmd)
    return {"ok": True, "session": session, "index": index, "target": target, "mode": mode, "exec": cmd}
```

Note: `MAIN_KEY` and `_run` are already imported in this module (line 26 and the tmux import block).

- [ ] **Step 4: Run the file's tests**

First, **unconditionally** add `_patch(mocker, "_run", return_value=(0, ""))` to the three existing tests that now hit the has-session call but don't patch `_run`: `test_window_new_simple_shell` (line ~37), `test_window_new_uses_project_pinned_dir` (~93), `test_window_new_archived_project_falls_through_to_cwd` (~111). Without the patch they shell out to REAL tmux — `test_window_new_simple_shell` targets session `main`, which exists on this machine, so it would pass green-but-live-tmux-dependent rather than fail.

Run: `uv run pytest tests/routes/test_sessions.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "feat(sessions): /api/window/new — MAIN_KEY lands in ~/dev, auto-create missing session"
```

## Task 3: Server — fold `window_new_worktree`'s two 400s

**Files:**
- Modify: `periscope/routes/sessions.py:292-303`
- Modify: `tests/routes/test_sessions.py:155-168`

- [ ] **Step 1: Update the two tests to the folded contract**

Replace `test_new_worktree_rejects_main` and `test_new_worktree_rejects_unowned_session`:

```python
def test_new_worktree_rejects_main_and_unmanaged(client, mocker):
    # Both folds of the same rule: worktree-tab needs a pinned project.
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    r = client.post("/api/window/new-worktree?session=main&branch=tc/x")
    assert r.status_code == 400
    assert "pinned project" in r.json()["detail"]

    _patch(mocker, "resolve_project_for_window", return_value=None)
    r = client.post("/api/window/new-worktree?session=ghost&branch=tc/x")
    assert r.status_code == 400
    assert "pinned project" in r.json()["detail"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/routes/test_sessions.py -q -k "rejects_main_and_unmanaged"`
Expected: FAIL (current messages are "not owned by a project" / "main project").

- [ ] **Step 3: Implement the fold**

In `window_new_worktree` (`periscope/routes/sessions.py:292-303`), replace:

```python
    project_key = resolve_project_for_window({"session": session})
    if not project_key:
        raise HTTPException(
            400, f"session {session!r} is not owned by a project; adopt it first"
        )
    if project_key == MAIN_KEY:
        raise HTTPException(
            400, "worktree-tab is not supported in the main project"
        )
```

with:

```python
    project_key = resolve_project_for_window({"session": session})
    if not project_key or project_key == MAIN_KEY:
        raise HTTPException(
            400,
            f"worktree-tab requires a session owned by a pinned project; "
            f"{session!r} is unmanaged or main",
        )
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "refactor(sessions): fold window_new_worktree's two 400 guards into one"
```

## Task 4: `railTree.js` — re-key the pure core

**Files:**
- Rewrite: `static/src/split/railTree.js`
- Create: `static/src/split/__tests__/railTree.test.js`

- [ ] **Step 1: Write the failing tests**

Create `static/src/split/__tests__/railTree.test.js` (style of `attention.test.js`: tiny factories):

```js
import { describe, it, expect } from "vitest";
import {
  MAIN_KEY, groupKeyForWindow, mergeLiveAndPrefs, indexProjects,
  projectLabel, groupLabel, paneChip, maxSeverity,
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- --run railTree`
Expected: FAIL — `indexProjects`, `groupKeyForWindow`, etc. not exported; `mergeLiveAndPrefs` has the old arity.

- [ ] **Step 3: Rewrite `railTree.js`**

Full replacement of `static/src/split/railTree.js`:

```js
// Pure tree-building for the split-view rail. Kept pure (no signals, no DOM)
// because it's consumed TWICE: once to render <Rail>, and once as
// `currentMergedOrder()`'s seed for the drag-reorder splices (so a drag
// operates on the order the user actually sees, not raw prefs).
//
// Membership is SESSION-ANCHORED (2026-06-12 spec): a window belongs to its
// tmux session's project (`project_pinned_dir`, resolved server-side), and
// the project's `repo` field keys the top-level group. cd never moves a row;
// the cwd-derived repo_key/branch fields are display-only (chips).
//
// MAIN_KEY ("dev") replaces OTHER_REPO_KEY: the catch-all for __main__'s own
// session, folded unmanaged sessions, and no-row pins (archived projects /
// delete races). Dev renders as a FLAT pane list — worktreesByRepo[MAIN_KEY]
// is always [] and panesByWorktree[MAIN_KEY] holds the unified pid order
// (that pref key IS persisted, unlike Other's was). Pinned to the bottom at
// the same enforcement points Other had: merge (here), isValidDropTarget,
// reorderRepos, the RepoRow drag-attr gate, and syncRailPrefs (Rail.jsx).

export const MAIN_KEY = "__main__";

// Severity ranking for status rollup: higher index = higher priority.
const SEVERITY = ["shell", "idle", "done", "working", "needs-input"];

export function maxSeverity(states) {
  let best = -1;
  for (const s of states) {
    const i = SEVERITY.indexOf(s);
    if (i > best) best = i;
  }
  return best >= 0 ? SEVERITY[best] : "shell";
}

// { pinned_dir: projectRow } from the /api/state projects payload.
export function indexProjects(projects) {
  const out = {};
  for (const p of (projects || [])) out[p.pinned_dir] = p;
  return out;
}

// Window → top-level group key. Folds to MAIN_KEY when the window has no
// project pin, is pinned to main, or its pin has no row in the payload
// (archived project or just-deleted race — projects_view filters archived).
// Null-repo projects key their own group by pinned_dir.
export function groupKeyForWindow(w, projectsByPin) {
  const pin = w.project_pinned_dir;
  if (!pin || pin === MAIN_KEY) return MAIN_KEY;
  const row = projectsByPin[pin];
  if (!row) return MAIN_KEY;
  return row.repo || pin;
}

// Merge live /api/state windows + projects with persisted ordering prefs to
// produce the rail tree. Live windows ARE the membership; prefs are ordering
// hints — pref entries come first (in pref position), then new live entries
// append. Pref entries no longer live are silently dropped.
//
// Return shape (unchanged from the cwd-keyed era, so drag descriptors,
// reorder splices, and syncRailPrefs keep working on key substitution):
//   repoOrder        — group keys, MAIN_KEY last iff dev has windows
//   worktreesByRepo  — group key → ordered session list ([] for MAIN_KEY)
//   panesByWorktree  — session → ordered child keys (pids + "review");
//                      panesByWorktree[MAIN_KEY] = flat dev pid order
export function mergeLiveAndPrefs(windows, projects, prefRepoOrder, prefWtByRepo, prefPanesByWt) {
  const projectsByPin = indexProjects(projects);
  const liveByRepo = {};       // group key → ordered session list (first-seen)
  const livePanesByWt = {};    // session → ordered pane pids (first-seen)
  const liveDevPids = [];      // flat dev membership (cross-session)
  for (const w of (windows || [])) {
    const g = groupKeyForWindow(w, projectsByPin);
    if (g === MAIN_KEY) {
      if (!liveDevPids.includes(w.pid)) liveDevPids.push(w.pid);
      continue;
    }
    const s = w.session;
    if (!liveByRepo[g]) liveByRepo[g] = [];
    if (!liveByRepo[g].includes(s)) liveByRepo[g].push(s);
    if (!livePanesByWt[s]) livePanesByWt[s] = [];
    if (!livePanesByWt[s].includes(w.pid)) livePanesByWt[s].push(w.pid);
  }

  // Repo order: prefs first (filtered to live), then live-new appended.
  // Dev always lands at the bottom regardless of pref order.
  const liveRepoSet = new Set(Object.keys(liveByRepo));
  const realRepos = [...prefRepoOrder.filter(r => liveRepoSet.has(r) && r !== MAIN_KEY),
                     ...Object.keys(liveByRepo).filter(r => !prefRepoOrder.includes(r) && r !== MAIN_KEY)];
  const repoOrder = liveDevPids.length ? [...realRepos, MAIN_KEY] : realRepos;

  // Session order per repo group: same pref-first logic.
  const worktreesByRepo = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY) { worktreesByRepo[r] = []; continue; }
    const live = liveByRepo[r] || [];
    const liveSet = new Set(live);
    const pref = (prefWtByRepo[r] || []).filter(w => liveSet.has(w));
    const prefSet = new Set(pref);
    worktreesByRepo[r] = [...pref, ...live.filter(w => !prefSet.has(w))];
  }

  // Pane-children order per session: prefs first (filtered), then new live
  // pids. The "review" sentinel is auto-added for repo-backed project
  // sessions only — a null-repo project's group gets none (LGTM review of
  // a non-git dir is a dead row; LGTM just degrades silently).
  const panesByWorktree = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY) continue;
    const own = projectsByPin[r];           // set iff r is a null-repo project's own group
    const hasReview = !(own && !own.repo);
    for (const w of worktreesByRepo[r]) {
      const live = livePanesByWt[w] || [];
      const liveSet = new Set(live);
      const pref = prefPanesByWt[w] || [];
      const prefKept = pref.filter(c => (c === "review" && hasReview) || liveSet.has(c));
      const prefSet = new Set(prefKept);
      const merged = [...prefKept, ...live.filter(p => !prefSet.has(p))];
      if (hasReview && !merged.includes("review")) merged.push("review");
      panesByWorktree[w] = merged;
    }
  }
  // Dev: flat unified pid order under the synthetic MAIN_KEY child key —
  // this is what makes cross-session drag inside dev satisfy the existing
  // same-worktreeKey drop rule, and what syncRailPrefs persists.
  if (liveDevPids.length) {
    const liveSet = new Set(liveDevPids);
    const pref = (prefPanesByWt[MAIN_KEY] || []).filter(p => liveSet.has(p));
    const prefSet = new Set(pref);
    panesByWorktree[MAIN_KEY] = [...pref, ...liveDevPids.filter(p => !prefSet.has(p))];
  }

  return { repoOrder, worktreesByRepo, panesByWorktree };
}

// Build a quick { worktreeKey: [windowObj, ...] } map from /api/state windows.
export function indexWindowsByWorktree(windows) {
  const out = {};
  for (const w of (windows || [])) {
    const key = w.session;  // worktree_key = session name
    (out[key] = out[key] || []).push(w);
  }
  return out;
}

// Project-row label: stable (never cwd-derived — the first-pane branch
// churned on cd, the exact instability this design kills).
export function projectLabel(project, session) {
  return (project && (project.name || project.base_branch)) || session;
}

// Top-level group label: "dev" for MAIN_KEY; a null-repo project's own group
// uses its name; repo groups use the path basename.
export function groupLabel(groupKey, projectsByPin) {
  if (groupKey === MAIN_KEY) return "dev";
  const own = projectsByPin[groupKey];
  if (own && !own.repo && own.name) return own.name;
  const parts = String(groupKey || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || groupKey;
}

// ~-relative path for chips. Pure string transform (the frontend doesn't
// know $HOME): collapses a leading /Users/<u> or /home/<u>.
function tildify(p) {
  return String(p || "").replace(/^\/(?:Users|home)\/[^/]+/, "~");
}

// Chip text for a pane row, or null (at-pin / nothing to say). Built from
// aff.kind + the window's OWN git/cwd fields — aff.label is only trusted for
// the sibling case (off-repo's label is basename(cwd), and dev panes always
// get {kind: no-repo, label: null} because __main__ is unpinned).
export function paneChip(w, { isDev = false, sessionPrefix = null } = {}) {
  const aff = w.worktree_affiliation || {};
  let text = null;
  if (isDev || aff.kind === "no-repo" || aff.kind === "off-repo") {
    if (w.repo_key && w.repo_label) {
      text = w.branch ? `${w.repo_label}/${w.branch}` : w.repo_label;
    } else if (w.cwd) {
      text = tildify(w.cwd);
    }
  } else if (aff.kind === "sibling") {
    text = aff.label || null;
  }
  // at-pin (non-dev) falls through with text = null.
  if (aff.kind === "at-pin" && !isDev) text = null;
  if (!text) return sessionPrefix || null;
  return sessionPrefix ? `${sessionPrefix}: ${text}` : text;
}
```

Note the deliberate simplification vs. the test list: `off-repo` and `no-repo` share one branch (both render from the window's own git fields, cwd fallback). `repoLabelFor` and `OTHER_REPO_KEY` are deleted — Task 6 fixes the imports.

- [ ] **Step 4: Run the vitest file**

Run: `npm test -- --run railTree`
Expected: all PASS. (Other vitest files still pass: `npm test -- --run`.)

- [ ] **Step 5: Commit**

```bash
git add static/src/split/railTree.js static/src/split/__tests__/railTree.test.js
git commit -m "feat(rail): session-anchored railTree core — project grouping, MAIN_KEY dev fold, paneChip"
```

(The app is temporarily broken — Rail.jsx still imports `OTHER_REPO_KEY`/`repoLabelFor` and calls the old arity. Tasks 5–6 land before any rebuild of `static/dist`, so the committed bundle never breaks.)

## Task 5: `RailRows.jsx` — chip slot + `isDev`

**Files:**
- Modify: `static/src/split/RailRows.jsx:109-156` (PaneRow), `:265-286` (RepoRow), `:232-263` (WorktreeRow)
- Modify: `static/styles.css` (one new rule)

- [ ] **Step 1: PaneRow chip prop**

In `PaneRow`, add `chip` to the destructured props and render it after `RailLabel` (before the burn flame so the name+chip read as one phrase):

```jsx
export function PaneRow({ w, chip, selectedKey, onSelect, onClose, onRename, dim, dragProps, dropPos, pinned, onTogglePin }) {
```

```jsx
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {chip && <span class="rail-chip" title={w.cwd}>⧉ {chip}</span>}
```

- [ ] **Step 2: RepoRow `isOther` → `isDev`**

Rename the prop and keep both gates (draggable + glyph). The dev glyph stays `◇`:

```jsx
export function RepoRow({ repoKey, label, collapsed, rolledUp, dim, isDev, onToggle, dragProps, dropPos }) {
```

…and replace the three `isOther` uses inside with `isDev` (comment: `// "dev" is pinned to the bottom — never draggable; omit the drag props.`).

- [ ] **Step 3: WorktreeRow drops `isOther`**

Dev has no worktree rows anymore, so `WorktreeRow`'s `isOther` handling is dead. Remove `isOther` from the props and replace its three uses with the non-other branch:
- glyph: always `<span class="rail-icon icon-worktree">⎇</span>`
- `renameable={!isOther}` → `renameable`
- the close button renders unconditionally (drop the `!isOther &&` wrapper)

- [ ] **Step 4: Chip CSS**

In `static/styles.css`, next to the existing `.rail-status` rule (grep for `.rail-status`), add:

```css
.rail-chip {
  font-size: 10px;
  color: var(--fg-3);  /* matches the adjacent .rail-status rule */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 11em;
  flex: 0 1 auto;
}
```

(If the stylesheet doesn't define `--muted`, use the literal color the `.rail-status` rule uses.)

- [ ] **Step 5: Commit**

```bash
git add static/src/split/RailRows.jsx static/styles.css
git commit -m "feat(rail): PaneRow affiliation chip + RepoRow isDev, WorktreeRow sheds isOther"
```

## Task 6: `Rail.jsx` — wire projects in; dev flat branch

**Files:**
- Modify: `static/src/split/Rail.jsx`
- Modify: `static/src/overlays/NewProjectModal.jsx:41`, `static/src/overlays/ReviewPrModal.jsx:36`

- [ ] **Step 1: Imports + signals**

```js
import { windows, projects, currentFilter, railSelection, dragState } from "../store.js";
import {
  mergeLiveAndPrefs, indexWindowsByWorktree, indexProjects,
  projectLabel, groupLabel, paneChip, maxSeverity, MAIN_KEY,
} from "./railTree.js";
```

(`repoLabelFor` and `OTHER_REPO_KEY` imports are gone.)

- [ ] **Step 2: Thread `projects` through the three `mergeLiveAndPrefs` call sites**

All three (syncRailPrefs at ~line 60, `currentMergedOrder()` at ~line 101, the render at ~line 188) become:

```js
mergeLiveAndPrefs(windows.value, projects.value, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree())
```

(in `Rail()` itself, use the already-read `live` for the first arg and add `const projs = projects.value;`).

- [ ] **Step 3: syncRailPrefs — persist dev pane order**

Replace the `nextRepoOrder`/`nextWtByRepo`/`nextPanesByWt` block (~lines 67-75):

```js
  const nextRepoOrder = merged.repoOrder.filter((r) => r !== MAIN_KEY);
  const nextWtByRepo = { ...merged.worktreesByRepo };
  delete nextWtByRepo[MAIN_KEY];
  const nextPanesByWt = {};
  for (const r of nextRepoOrder) {
    for (const wt of (nextWtByRepo[r] || [])) {
      nextPanesByWt[wt] = merged.panesByWorktree[wt] || [];
    }
  }
  // Dev's flat order IS persisted (unlike Other's was) — it's the only
  // ordering state dev has.
  if (merged.panesByWorktree[MAIN_KEY]) {
    nextPanesByWt[MAIN_KEY] = merged.panesByWorktree[MAIN_KEY];
  }
```

- [ ] **Step 4: reorderRepos / isValidDropTarget — sentinel swap**

In `reorderRepos` (~line 109) and `isValidDropTarget` (~lines 152-153), replace every `OTHER_REPO_KEY` with `MAIN_KEY`. No logic changes — the pane/review branch's `drag.worktreeKey === target.worktreeKey` rule already admits dev-internal drags because both descriptors carry `MAIN_KEY` (next step).

- [ ] **Step 5: Render — labels, dev branch**

Inside `Rail()`:

```js
  const projsByPin = indexProjects(projs);
  const projectsBySession = {};
  for (const p of projs) if (p.tmux_session) projectsBySession[p.tmux_session] = p;
  const mainProject = projsByPin[MAIN_KEY] || {};
```

`wtLabelUniverse` (~lines 193-195) switches source:

```js
  const wtLabelUniverse = repoOrder
    .filter((r) => r !== MAIN_KEY)
    .flatMap((r) => (worktreesByRepo[r] || []).map((wt) => projectLabel(projectsBySession[wt], wt)));
```

In the `repoOrder.map` body:
- `const isDev = repoKey === MAIN_KEY;`
- `const repoLabel = groupLabel(repoKey, projsByPin);`
- repo rollup/dim for dev compute over dev's panes (see below) instead of `worktrees` (which is `[]`).
- `<RepoRow … isDev={isDev} …/>`
- Worktree label (~line 393): `const label = shortestUniqueSuffix(projectLabel(projectsBySession[wtKey], wtKey), wtLabelUniverse);` (the `isOther ? wtKey :` branch is gone).
- `<WorktreeRow>` call site drops `isOther`; `<WorktreeMeta>` renders unconditionally (`{!isOther && …}` → always); pane drag descriptors inside project worktrees are unchanged.
- Pane chips in project worktrees: `<PaneRow … chip={paneChip(w)} …/>`.

Then the dev branch — after the `{!repoCollapsed && worktrees.map(…)}` block, add a sibling branch rendered when `isDev && !repoCollapsed`:

```jsx
            {isDev && !repoCollapsed && (() => {
              const byPid = Object.fromEntries(live.map((w) => [w.pid, w]));
              const rows = (panesByWorktree[MAIN_KEY] || []).flatMap((pid) => {
                const w = byPid[pid];
                if (!w) return [];
                const sessionPrefix = w.session !== mainProject.tmux_session ? w.session : null;
                return [
                  <PaneRow
                    key={`pane:${w.pid}`}
                    w={w}
                    chip={paneChip(w, { isDev: true, sessionPrefix })}
                    selectedKey={selectedKey}
                    dim={passesFilter(w, filter)}
                    onSelect={selectKey}
                    onClose={() => closePane(w)}
                    onRename={(next) => renamePane(w, next)}
                    dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: MAIN_KEY })}
                    dropPos={dropPosFor(`pane:${w.pid}`)}
                    pinned={prefs.getPinnedPids().includes(w.pid)}
                    onTogglePin={() => prefs.togglePin(w.pid)}
                  />,
                ];
              });
              rows.push(
                <NewTabRow
                  key={`newtab:${MAIN_KEY}`}
                  worktreeKey={mainProject.tmux_session || "main"}
                  onOpen={openLauncher}
                />
              );
              return rows;
            })()}
```

For dev's repo-row rollup + dim (the existing `repoChildStates`/`repoDim` compute over `worktrees`, which is empty for dev):

```js
        const devWindows = isDev
          ? (panesByWorktree[MAIN_KEY] || []).map((pid) => live.find((w) => w.pid === pid)).filter(Boolean)
          : [];
        const repoChildStates = isDev
          ? devWindows.map((w) => w.state || "shell")
          : worktrees.flatMap((wt) => (byWorktree[wt] || []).map((w) => w.state || "shell"));
        const repoDim = isDev
          ? devWindows.some((w) => passesFilter(w, filter))
          : worktrees.some((wt) => (byWorktree[wt] || []).some((w) => passesFilter(w, filter)));
```

- [ ] **Step 6: Empty-state copy**

The rail empty state (~line 304) is now only reachable with zero live windows. Update the copy:

```jsx
        <div class="rail-empty">
          No tmux windows found. Use <code>+ project</code> or <code>review PR</code> to start one.
        </div>
```

- [ ] **Step 7: Modal repoKey flips**

`static/src/overlays/NewProjectModal.jsx:41` and `static/src/overlays/ReviewPrModal.jsx:36`: the rail pref key for a new project's group is now the project's repo, so the project-row value must win over the window's cwd-derived key:

```js
      repoKey: result.repo || wins[0].repo_key,
```

- [ ] **Step 8: Run all frontend tests + build**

Run: `npm test -- --run && npm run build`
Expected: vitest green; Vite build succeeds (catches any leftover `OTHER_REPO_KEY`/`repoLabelFor` import).

Then grep for stragglers: `grep -rn "OTHER_REPO_KEY\|repoLabelFor" static/src/` — expected: no hits.

- [ ] **Step 9: Commit (including the rebuilt bundle)**

```bash
git add static/src static/dist
git commit -m "feat(rail): session-anchored grouping — projects join, dev flat list, chips wired"
```

## Task 7: Full-suite verification

- [ ] **Step 1: Server suite**

Run: `uv run pytest -q`
Expected: all pass (paste the tail).

- [ ] **Step 2: Frontend suite**

Run: `npm test -- --run`
Expected: all pass.

- [ ] **Step 3: Browser verification (dev server)**

```sh
npm run dev    # FastAPI :8765 + vite :5174
```

Checklist (against real tmux sessions):
1. Project session panes group under their project's repo; `cd /tmp` in one — **row does not move**, chip `⧉ ~/…` appears within ~15s (git-cache TTL), detail pane stays open.
2. cd into a sibling worktree → chip shows that worktree's branch.
3. Main session panes (fdy/master tabs included) appear under **dev** at the bottom, each with a location chip; an ad-hoc `tmux new -s scratch -d` session's window appears in dev with `scratch:` prefix.
4. Drag panes within dev (across folded sessions) — reorder sticks across reload (prefs `panes_by_worktree.__main__`).
5. dev repo-row is not draggable; real repo groups still reorder.
6. dev "+ New tab" spawns into the main session at `~/dev`; kill the main session entirely (`tmux kill-session -t main`) with a scratch session alive, then "+ New tab" again — session auto-creates, no error toast.
7. Review row appears under project sessions only; LGTM chip unaffected.
8. Rail filter (FilterBar) still dims correctly inside dev.

- [ ] **Step 4: Deploy to prod periscope**

```bash
bin/periscope restart
```

Watch the live dashboard for one poll cycle; `bin/periscope tail` for tracebacks.

---

## Self-review notes

- Spec coverage: fold rule (T1), `~/dev` + auto-create (T2), folded 400 (T3), grouping/no-row fallback/labels/chips/dev flat+drag/prefs (T4-6), bug workstream (tracked separately, out of this plan per spec).
- Type consistency: `mergeLiveAndPrefs(windows, projects, prefRepoOrder, prefWtByRepo, prefPanesByWt)` arity used identically in T4 tests and T6 call sites; `paneChip(w, {isDev, sessionPrefix})` matches between T4 and T6; `isDev` prop name matches T5/T6.
- Known temporary breakage window: T4 commits a railTree.js incompatible with Rail.jsx — acceptable because `static/dist` is only rebuilt in T6 Step 8; the committed bundle stays consistent at every commit.
