# Project Model + New Worktree Tab (Phase 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Verb 2 from `2026-05-15-workflow-management-design.md` — new worktree-tab inside an existing project (forks a sub-branch off `project.base_branch` locally; no fetch). Plus a small backend-only fix to plain new-tab semantics so tabs spawned in a project start at `project.pinned_dir`, not whatever the active pane's cwd happens to be.

**Architecture:** `periscope/worktree_spawn.py`'s `spawn_worktree` gains a `fetch: bool = True` parameter — when False, skips `git fetch` and forks from the local `base_branch` ref directly. New endpoint `POST /api/window/new-worktree` resolves the target project from its session, calls `spawn_worktree(repo, branch, base_branch=project.base_branch, fetch=False)`, then `tmux new-window -c <wt_path>` into the project's existing tmux session, and optionally sends an exec command. The existing `POST /api/window/new` learns to prefer `project.pinned_dir` over `pane_current_path` when its target session is owned by a project. Frontend extends the existing `⋯` menu with a "new worktree tab" item that prompts for a sub-branch name.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git. The pytest suite at `tests/` is the source of truth for behavior — all existing tests must continue to pass.

**Spec:** `docs/superpowers/specs/2026-05-15-workflow-management-design.md` §Verb 2 (worktree mode + plain mode).

**Design calls (confirmed in conversation):**
- Worktree-tab forks from the LOCAL `project.base_branch` ref. No `git fetch`. Reason: `base_branch` is typically the project's own feature branch (`tc/feat-X`), unpushed work-in-progress. Fetching `origin/tc/feat-X` would either fail or skip the user's local commits.
- Fallback: if `project.base_branch` is null (legacy projects from before phase 1 stamped it), fall back to the repo's detected default branch, AND fetch in that case (defaults are pushed).
- Project pin always wins over active pane cwd. Plain new-tab in a project-owned tmux session starts at `project.pinned_dir`, full stop. No "inherit from currently-active pane" path.
- UI lives in the existing `⋯` menu via `prompt()`. Phase 7 will replace `prompt()` with a proper modal across all per-project actions.

**What's explicitly NOT in phase 3:**
- **Change branch** (Verb 7) — needs stash-or-abort dirty-state UX. Punt to phase 6 alongside cleanup verbs.
- **Edit repo override** (Verb 7) — niche; punt to phase 7 settings UI.
- **Polished modal UX** for new worktree tab — phase 7.
- **PR review** (Verb 3) — phase 4.
- **Conversation history** (Verb 4) — phase 5.

---

## File Structure

**Modified:**
- `periscope/worktree_spawn.py` — `spawn_worktree` gains `fetch: bool = True` parameter. When False, skips `git fetch origin <base>` and forks from `<base>` directly (local ref). Updated docstring.
- `periscope/routes/sessions.py` — `POST /api/window/new` resolves project from session; if a non-archived non-main project owns the session, cwd defaults to `project.pinned_dir` instead of `pane_current_path`. Plus new endpoint `POST /api/window/new-worktree`.
- `static/grid.js` — `handleProjectMenu` adds `"worktree-tab"` to the actions list. When chosen, prompts for sub-branch name and POSTs to `/api/window/new-worktree`.

**Not modified (deliberately):**
- No new modules. The whole phase reuses existing infrastructure.
- No frontend module additions or HTML changes. The `⋯` menu's `prompt()` is the UI.

---

## Task 1: `spawn_worktree(..., fetch=True)` parameter

**Files:**
- Modify: `periscope/worktree_spawn.py` (`spawn_worktree` signature + body around the fetch + worktree-add block)

- [ ] **Step 1: Add the `fetch` parameter**

Update the function signature and body in `periscope/worktree_spawn.py`. Current signature (around line 63):

```python
def spawn_worktree(
    repo: str,
    branch: str,
    base_branch: str | None = None,
) -> dict:
```

New signature:

```python
def spawn_worktree(
    repo: str,
    branch: str,
    base_branch: str | None = None,
    fetch: bool = True,
) -> dict:
```

Update the docstring's "Returns" section to mention that `fetch=False` skips the network call and forks from the local `<base_branch>` ref directly, intended for new-worktree-tab callers where `base_branch` is the project's own (typically unpushed) feature branch.

- [ ] **Step 2: Make the fetch call conditional**

Around line 115-122 of the current file (the fetch block — search for `fetch_code, fetch_out`):

```python
    warning: str | None = None

    # Fetch runs OUTSIDE the per-repo lock. It's a long-running network op
    # ...
    fetch_code, fetch_out = _run(
        ["git", "-C", repo, "fetch", "origin", base], timeout=30.0
    )
    if fetch_code != 0:
        warning = f"fetch failed: origin/{base} may be stale ({fetch_out!r})"
        log.warning("worktree_spawn: %s", warning)
```

Wrap the fetch in `if fetch:`. Adjust the comment to acknowledge both modes:

```python
    warning: str | None = None

    # Fetch runs OUTSIDE the per-repo lock (network op, idempotent vs.
    # concurrent fetches). Skipped when `fetch=False` — callers spawning
    # off a local-only ref (e.g. an unpushed project branch) don't want
    # to fetch and don't need the remote to be up to date.
    if fetch:
        fetch_code, fetch_out = _run(
            ["git", "-C", repo, "fetch", "origin", base], timeout=30.0
        )
        if fetch_code != 0:
            warning = f"fetch failed: origin/{base} may be stale ({fetch_out!r})"
            log.warning("worktree_spawn: %s", warning)
```

- [ ] **Step 3: Make the `git worktree add` base-ref conditional**

Around line 130-138 of the current file (the `git worktree add` call):

```python
        code, out = _run(
            [
                "git", "-C", repo,
                "worktree", "add",
                "-b", branch,
                wt_path_str,
                f"origin/{base}",
            ],
            timeout=30.0,
        )
```

Switch the base ref based on `fetch`:

```python
        # With fetch=True the fresh remote ref is the source of truth.
        # With fetch=False the local ref is what we want — typically
        # the project's own feature branch with the user's unpushed work.
        base_ref = f"origin/{base}" if fetch else base
        code, out = _run(
            [
                "git", "-C", repo,
                "worktree", "add",
                "-b", branch,
                wt_path_str,
                base_ref,
            ],
            timeout=30.0,
        )
```

- [ ] **Step 4: Verify both modes**

End-to-end test against a fake bare-repo + clone, exercising both modes:

```bash
cd /Users/tom/dev/periscope && uv run python3 << 'EOF'
import tempfile, subprocess, os
from periscope.worktree_spawn import spawn_worktree

with tempfile.TemporaryDirectory() as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    bare = os.path.join(tmpdir, "bare.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "set-head", "origin", "main"], check=True)

    # Create a LOCAL-only branch (no remote ref).
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "tc/local-feat"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "local work", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)

    # fetch=True (default) forks from origin/main. Worktree's HEAD should
    # NOT have the local-only commit.
    r1 = spawn_worktree(repo, "tc/sub-fetched", base_branch="main")
    log_out = subprocess.run(["git", "-C", r1["path"], "log", "--oneline"],
                              capture_output=True, text=True).stdout
    print(f"fetch=True path: {log_out.strip().splitlines()[0]}")
    assert "local work" not in log_out, "fetch=True should fork from origin, not local"

    # fetch=False with base_branch=tc/local-feat. Worktree's HEAD SHOULD
    # contain the local-only commit (since we fork from local).
    r2 = spawn_worktree(repo, "tc/sub-local", base_branch="tc/local-feat", fetch=False)
    log_out2 = subprocess.run(["git", "-C", r2["path"], "log", "--oneline"],
                               capture_output=True, text=True).stdout
    print(f"fetch=False path:\n{log_out2}")
    assert "local work" in log_out2, "fetch=False should fork from local ref"
    # No "warning" key on fetch=False (we never tried to fetch).
    assert "warning" not in r2

    # Cleanup
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", r1["path"]])
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", r2["path"]])

print("PASS")
EOF
```

Expected: PASS. fetch=True path doesn't contain the local-only commit; fetch=False path does.

- [ ] **Step 5: Run the existing pytest suite**

```bash
cd /Users/tom/dev/periscope && uv run pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all tests pass. Phase 2's tests don't exercise `fetch=False` but the default `fetch=True` should be unchanged.

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 commit -am "worktree_spawn: add fetch=True param; fetch=False forks from local base ref (for unpushed project branches)"
```

---

## Task 2: `POST /api/window/new-worktree` endpoint

**Files:**
- Modify: `periscope/routes/sessions.py`

- [ ] **Step 1: Add imports**

`periscope/routes/sessions.py` currently imports `APIRouter, Query` from FastAPI but NOT `HTTPException`. The new `window_new_worktree` endpoint uses HTTPException for error signaling (a deliberate convention deviation from this file's other handlers, which use `{"ok": False, "error": ...}`). HTTPException matches `routes/projects.py`'s conventions and gives cleaner status codes for the new surface; existing handlers stay on their pattern.

Extend the FastAPI import to include `HTTPException`:

```python
from fastapi import APIRouter, HTTPException, Query
```

And add the project / spawn imports:

```python
from periscope.projects import resolve_project_for_window, get_project
from periscope.worktree_spawn import spawn_worktree
```

Note: `_detect_default_branch` is NOT needed in this module — when `base_branch` is None, `spawn_worktree` calls `_detect_default_branch` internally. The plan-reviewer caught this would have been an unused import.

- [ ] **Step 2: Add the endpoint**

Append to `periscope/routes/sessions.py` (after the existing `window_new` handler, around line 180):

```python
@router.post("/api/window/new-worktree")
def window_new_worktree(
    session: str,
    branch: str,
    exec_cmd: str = Query("claude", alias="exec"),
):
    """Spawn a new worktree-tab in `session`'s owning project.

    Forks a sub-worktree off the project's `base_branch` (local ref,
    no fetch), opens a new tmux window in it, and optionally runs
    `exec_cmd` (defaults to `claude` — matches trellis's `t` hotkey).

    Body shape mirrors `/api/window/new`: session + exec are query
    params; `branch` is the new sub-branch name. Slugging for the
    on-disk worktree path is handled by `spawn_worktree`.

    Errors:
      400 — session not owned by a project, or branch invalid, or
            worktree-add failed.
      404 — session doesn't exist in tmux.
      409 — sub-worktree path or branch already exists.
    """
    branch = branch.strip()
    if not branch:
        raise HTTPException(400, "branch is required")
    if branch.startswith("-"):
        raise HTTPException(400, f"branch name cannot start with '-': {branch!r}")

    # Confirm the tmux session exists. The `has-session` invariant
    # mirrors the phase-2 create endpoint's pre-check.
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0:
        raise HTTPException(404, f"tmux session {session!r} not found")

    # Resolve project. The session must be owned by a non-main project
    # (the worktree-tab verb doesn't apply to __main__ — there's no
    # base_branch to fork from).
    project_key = resolve_project_for_window({"session": session})
    if not project_key:
        raise HTTPException(
            400, f"session {session!r} is not owned by a project; adopt it first"
        )
    if project_key == "__main__":
        raise HTTPException(
            400, "worktree-tab is not supported in the main project"
        )
    project = get_project(project_key)
    if not project.get("repo"):
        raise HTTPException(
            400, f"project at {project_key!r} has no repo recorded"
        )

    repo = project["repo"]
    base_branch = project.get("base_branch")
    # Two paths based on base_branch presence:
    #   - base_branch set (typical): fork from LOCAL ref (no fetch).
    #     The user's unpushed work on the project's branch is included.
    #   - base_branch null (legacy projects): fall back to repo default
    #     branch with fetch=True (defaults are pushed, fetch is safe).
    if base_branch:
        spawn_kwargs = {"base_branch": base_branch, "fetch": False}
    else:
        spawn_kwargs = {}  # uses detected default + fetch=True

    try:
        res = spawn_worktree(repo, branch, **spawn_kwargs)
    except ValueError as e:
        # spawn_worktree raises ValueError in these cases (see
        # periscope/worktree_spawn.py):
        #   - "branch name cannot start with '-'"        → 400 (caught above too)
        #   - "not a git repo: <path>"                    → 400
        #   - "worktree path already exists: <path>"      → 409
        #   - "git worktree add failed: <git stderr>"     → 409 if stderr
        #     contains "already exists" (branch collision from git), else 400
        # The "already exists" substring catches both the path-collision
        # path AND the branch-collision-from-git path. Other failures
        # (network, disk full, etc.) fall through to 400.
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        raise HTTPException(status, msg)
    wt_path = res["path"]
    warning = res.get("warning")

    # Spawn the new window in the project's tmux session, rooted at the
    # new worktree's path. -P -F captures the freshly-created window's
    # index so we know the target for note_focus / send-keys.
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:",
        "-c", wt_path,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        # The worktree is on disk but the window failed. Leave the
        # worktree — the user can try again or `tmux new-window` manually.
        raise HTTPException(500, f"tmux new-window failed: {msg}")
    try:
        index = int(msg)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {msg!r}")
    target = f"{session}:{index}"

    cmd = exec_cmd.strip()
    if cmd:
        # Shell-rc race window — match the existing /api/window/new and
        # /api/projects flows (CLAUDE.md "Key invariants" note 5).
        time.sleep(0.1)
        tmux("send-keys", "-t", target, cmd, "Enter")

    note_focus(target)
    note_action(target)

    result = {
        "ok": True,
        "session": session,
        "index": index,
        "target": target,
        "worktree_path": wt_path,
        "branch": branch,
        "base_branch": res["base_branch"],
        "exec": cmd,
    }
    if warning:
        result["warning"] = warning
    return result
```

You may need to add `from periscope.tmux import tmux` to the imports (the existing imports already include `_run`, `_tmux_mutate`, and probably `tmux` — verify).

- [ ] **Step 3: Verify the endpoint**

```bash
cd /Users/tom/dev/periscope && XDG_CONFIG_HOME=/tmp/p3-verify-t2 uv run --with httpx python3 << 'EOF'
import os, tempfile, subprocess
from fastapi.testclient import TestClient
from periscope.app import app
from periscope.projects import create_project
client = TestClient(app)

# Set up a fake project: bare + clone + tmux session + project row in state.
with tempfile.TemporaryDirectory(prefix="p3-t2-") as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    bare = os.path.join(tmpdir, "bare.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "set-head", "origin", "main"], check=True)

    # Local-only project branch with a commit, then switch back to main so
    # tc/feat is NOT checked out anywhere — otherwise `git worktree add -b
    # <new> path tc/feat` errors with "tc/feat is already checked out".
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "tc/feat"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "feat work", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)

    sess = "p3-t2-proj"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-c", repo], check=True)

    # Register the project (skipping the full +project flow for test setup).
    create_project(repo, name=sess, tmux_session=sess, repo=repo, base_branch="tc/feat")

    wt_path = None
    try:
        # Spawn a worktree-tab. exec=claude is the default.
        r = client.post(f"/api/window/new-worktree?session={sess}&branch=tc/sub-feat&exec=")
        print(f"new-worktree status: {r.status_code}, body: {r.json()}")
        assert r.status_code == 200, r.text
        body = r.json()
        wt_path = body["worktree_path"]

        # Worktree exists and contains the local-only "feat work" commit
        # (proves we forked from local tc/feat, not origin/main).
        log_out = subprocess.run(["git", "-C", wt_path, "log", "--oneline"],
                                  capture_output=True, text=True).stdout
        print(f"worktree log:\n{log_out}")
        assert "feat work" in log_out

        # tmux window count for the session is now 2 (original + new).
        out = subprocess.run(["tmux", "list-windows", "-t", sess, "-F", "#{window_index}"],
                              capture_output=True, text=True, check=True).stdout.strip().split("\n")
        print(f"windows: {out}")
        assert len(out) == 2

        # Branch validation
        r = client.post(f"/api/window/new-worktree?session={sess}&branch=-bad")
        assert r.status_code == 400, r.text

        # Session not owned by a project
        r = client.post(f"/api/window/new-worktree?session=__nonexistent__&branch=tc/x")
        assert r.status_code == 404, r.text

    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        if wt_path:
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt_path],
                           capture_output=True)
print("PASS")
EOF
rm -rf /tmp/p3-verify-t2
```

Expected: PASS. Worktree contains the local `feat work` commit (proving local-fork), tmux session has 2 windows, validation errors return the right status codes.

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 commit -am "routes/sessions: add POST /api/window/new-worktree — sub-worktree off project.base_branch"
```

---

## Task 3: plain-tab cwd defaults to `project.pinned_dir`

**Files:**
- Modify: `periscope/routes/sessions.py` (the `window_new` handler)

- [ ] **Step 1: Replace the cwd-resolution branch**

In `window_new` (around line 143-146), the existing `else:` branch that handles non-resume tab spawns does:

```python
    else:
        cwd = tmux(
            "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
        ).strip() or os.path.expanduser("~")
```

Replace with project-aware resolution. Note: `get_project` returns the project's row fields but NOT a `pinned_dir` field (the pinned_dir is the lookup key, not a row column — see `periscope/projects.py`). So `project_key` IS the pinned_dir path.

```python
    else:
        # Project pin always wins over active-pane cwd. If the target
        # session is owned by a non-archived non-main project, new tabs
        # land in the project's pinned_dir — even if the user has cd'd
        # away in the active pane. Fall back to the pane's cwd only
        # when no project owns the session (e.g. an unmanaged session
        # the user hasn't adopted, or __main__ which is unpinned).
        project_key = resolve_project_for_window({"session": session})
        project = get_project(project_key) if project_key else {}
        if project_key and project_key != "__main__" and not project.get("archived_at"):
            cwd = project_key  # pinned_dir IS the key
        else:
            cwd = tmux(
                "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
            ).strip() or os.path.expanduser("~")
```

- [ ] **Step 2: Verify**

```bash
cd /Users/tom/dev/periscope && XDG_CONFIG_HOME=/tmp/p3-verify-t3 uv run --with httpx python3 << 'EOF'
import os, tempfile, subprocess
from fastapi.testclient import TestClient
from periscope.app import app
from periscope.projects import create_project
client = TestClient(app)

with tempfile.TemporaryDirectory(prefix="p3-t3-") as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subdir = os.path.join(repo, "sub")
    os.mkdir(subdir)

    sess = "p3-t3-proj"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    # Start the session with cwd = subdir (so display-message would return subdir)
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-c", subdir], check=True)
    # Register the project pinned to the REPO ROOT (not subdir).
    create_project(repo, name=sess, tmux_session=sess, repo=repo, base_branch="main")

    try:
        # Active pane cwd is subdir. POST /api/window/new should still
        # land the new tab at repo root (project.pinned_dir wins).
        r = client.post(f"/api/window/new?session={sess}&mode=shell")
        print(f"new-window status: {r.status_code}, body: {r.json()}")
        assert r.status_code == 200, r.text
        idx = r.json()["index"]
        cwd_out = subprocess.run(
            ["tmux", "display-message", "-t", f"{sess}:{idx}", "-p", "#{pane_current_path}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # The new pane's cwd should be repo (realpath), not subdir.
        print(f"new pane cwd: {cwd_out}")
        assert os.path.realpath(cwd_out) == os.path.realpath(repo)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
print("PASS — pinned_dir wins over active-pane cwd")
EOF
rm -rf /tmp/p3-verify-t3
```

Expected: PASS. The new tab opens at the project's pinned_dir (repo root) even though the existing pane was in a subdir.

- [ ] **Step 3: Confirm `__main__` and unmanaged sessions still inherit cwd**

For a session not owned by any project (or owned by `__main__`), the new tab should still inherit cwd from `display-message`. This is the legacy behavior:

```bash
cd /Users/tom/dev/periscope && uv run --with httpx python3 << 'EOF'
import os, subprocess, tempfile
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)

with tempfile.TemporaryDirectory() as tmpdir:
    sess = "p3-t3-unmanaged"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-c", tmpdir], check=True)
    try:
        r = client.post(f"/api/window/new?session={sess}&mode=shell")
        idx = r.json()["index"]
        cwd_out = subprocess.run(
            ["tmux", "display-message", "-t", f"{sess}:{idx}", "-p", "#{pane_current_path}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        print(f"unmanaged: cwd = {cwd_out}, expected dir under {tmpdir}")
        assert os.path.realpath(cwd_out) == os.path.realpath(tmpdir)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
print("PASS — unmanaged session inherits cwd")
EOF
```

Expected: PASS. The unmanaged session's new tab inherits the cwd from `display-message`.

- [ ] **Step 4: Run the existing pytest suite**

```bash
cd /Users/tom/dev/periscope && uv run pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 commit -am "routes/sessions: new plain tabs in a project's session default to project.pinned_dir, not active-pane cwd"
```

---

## Task 4: ⋯ menu — "new worktree tab" entry

**Files:**
- Modify: `static/grid.js` (`handleProjectMenu`)

- [ ] **Step 1: Add the action**

Find `handleProjectMenu` in `static/grid.js`. The existing implementation handles two actions: `rename` and `archive`, via a top-level `prompt()`. Add `worktree-tab` (or similar) as a third option, and a branch input.

Replace the function body. Current shape (approximate):

```javascript
async function handleProjectMenu(btn) {
  const pinnedDir = btn.dataset.pinnedDir;
  if (!pinnedDir) return;
  const action = prompt(
    "Project action — type one of: rename, archive\n(blank = cancel)",
    ""
  );
  if (!action) return;
  if (action === "rename") {
    // ...
  } else if (action === "archive") {
    // ...
  }
}
```

Extended:

```javascript
async function handleProjectMenu(btn) {
  const pinnedDir = btn.dataset.pinnedDir;
  if (!pinnedDir) return;
  const action = prompt(
    "Project action — type one of: rename, archive, worktree-tab\n(blank = cancel)",
    ""
  );
  if (!action) return;
  if (action === "rename") {
    const name = prompt("New project name:");
    if (!name) return;
    const res = await fetch("/api/projects/patch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir, name, tmux_session: name }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`rename failed: ${err.detail || res.status}`);
    }
  } else if (action === "archive") {
    if (!confirm(`Archive project at ${pinnedDir}?`)) return;
    const res = await fetch("/api/projects/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`archive failed: ${err.detail || res.status}`);
    }
  } else if (action === "worktree-tab") {
    // Look up the project's tmux_session from the most recent /api/state.
    const project = (state.lastProjects || []).find((p) => p.pinned_dir === pinnedDir);
    if (!project || !project.tmux_session) {
      alert("project session not found — refresh and retry");
      return;
    }
    const branch = prompt(
      "New worktree branch name:\n(forked off this project's base_branch, locally)",
    );
    if (!branch) return;
    const params = new URLSearchParams({
      session: project.tmux_session,
      branch,
      exec: "claude",
    });
    const res = await fetch(`/api/window/new-worktree?${params}`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`new worktree tab failed: ${err.detail || res.status}`);
      return;
    }
    const body = await res.json();
    if (body.warning) console.warn("new-worktree warning:", body.warning);
  }
}
```

The `state.lastProjects` lookup is the join key Phase 1 introduced (Task 9 step 1 in the phase-1 plan; `state.js` line ~8 declares the slot). `project.pinned_dir` and `project.tmux_session` come from the existing `/api/state` payload shape.

`exec=claude` is the default. If the user wants something else (`shell`, `vim`), they have to use a different verb — phase 3 doesn't expose that. Trellis's behavior was claude-only for `t`; we match.

- [ ] **Step 2: Verify via browser**

Restart periscope on the worktree's code (or just hard-refresh once the change is served).

1. Click `⋯` on a non-main project (e.g. periscope).
2. Type `worktree-tab` at the action prompt.
3. Type a branch name like `tc/test-new-worktree-tab`.
4. Expect: a new window appears in that project's tmux session within ~3s.
5. Confirm: `ls ~/dev/worktrees/<repo>/<branch-slugged>` shows the new worktree.
6. Cleanup: `tmux kill-window -t <project_session>:<idx>` and `git -C <repo> worktree remove --force ~/dev/worktrees/<repo>/<branch-slugged>`.

If no browser available, the curl form:

```bash
# Replace <SESSION> with one of your active project tmux sessions.
curl -s -X POST "http://127.0.0.1:8765/api/window/new-worktree?session=<SESSION>&branch=tc/curl-test&exec=" | python3 -m json.tool
```

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 commit -am "grid: ⋯ menu — add worktree-tab action (sub-worktree off project's base_branch)"
```

---

## Task 5: end-to-end smoke + final commit

- [ ] **Step 1: Run the existing pytest suite once more**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 2: TestClient round-trip exercising all three phase-3 surfaces**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-3 && XDG_CONFIG_HOME=/tmp/p3-smoke uv run --with httpx python3 << 'EOF'
"""End-to-end phase-3 smoke: spawn project, spawn worktree-tab in it, spawn plain tab."""
import os, tempfile, subprocess
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)

with tempfile.TemporaryDirectory(prefix="p3-smoke-") as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    bare = os.path.join(tmpdir, "bare.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "set-head", "origin", "main"], check=True)

    # 1. Phase-2 + project (uses fetch=True default, branch != default → spawn).
    proj_sess = "p3-smoke-proj"
    subprocess.run(["tmux", "kill-session", "-t", proj_sess], capture_output=True)
    r = client.post("/api/projects", json={"repo": repo, "branch": "tc/main-feat", "name": proj_sess})
    assert r.status_code == 200, r.text
    pinned = r.json()["pinned_dir"]
    print(f"phase-2 + project OK: pinned={pinned}")

    # 2. Phase-3 worktree-tab: new sub-worktree off the project's tc/main-feat
    # branch, no fetch.
    # First add a local-only commit to the project's worktree so we can prove
    # the sub-worktree picks it up.
    subprocess.run(["git", "-C", pinned, "commit", "--allow-empty", "-m", "local feat work", "-q"], check=True)

    r = client.post(f"/api/window/new-worktree?session={proj_sess}&branch=tc/sub-feat&exec=")
    assert r.status_code == 200, r.text
    sub_path = r.json()["worktree_path"]
    print(f"phase-3 new-worktree-tab OK: sub_path={sub_path}")

    # The sub-worktree's HEAD should include "local feat work".
    log_out = subprocess.run(["git", "-C", sub_path, "log", "--oneline"],
                              capture_output=True, text=True).stdout
    print(f"sub log: {log_out}")
    assert "local feat work" in log_out

    # 3. Phase-3 plain tab in the project's session — should land at pinned_dir.
    # The project's tmux session currently has 3 windows (claude, shell, sub-feat).
    # The active pane is claude, which started at pinned_dir. To make this
    # interesting, cd one window away from pinned_dir then check that the
    # NEW tab still lands at pinned_dir.
    subprocess.run(["tmux", "send-keys", "-t", f"{proj_sess}:0", "cd /tmp", "Enter"])
    import time; time.sleep(0.2)
    r = client.post(f"/api/window/new?session={proj_sess}&mode=shell")
    assert r.status_code == 200, r.text
    new_idx = r.json()["index"]
    new_cwd = subprocess.run(
        ["tmux", "display-message", "-t", f"{proj_sess}:{new_idx}", "-p", "#{pane_current_path}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f"plain tab cwd: {new_cwd}")
    assert os.path.realpath(new_cwd) == os.path.realpath(pinned), \
        f"expected pinned_dir {pinned}, got {new_cwd}"

    # Cleanup
    subprocess.run(["tmux", "kill-session", "-t", proj_sess], capture_output=True)
    for path in (pinned, sub_path):
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", path], capture_output=True)

print("=== PHASE 3 SMOKE PASSED ===")
EOF
rm -rf /tmp/p3-smoke
```

Expected: `=== PHASE 3 SMOKE PASSED ===`.

- [ ] **Step 3: Commit if any cleanup or doc edits accumulated**

If the smoke surfaced anything needing a tweak, commit it. Otherwise this step is a no-op.

---

## What's deliberately NOT in phase 3

- **Change branch in pinned worktree** (Verb 7) — phase 6.
- **Edit repo override / pinned_repo** (Verb 7) — phase 7.
- **Polished modal UX for new worktree tab** — phase 7.
- **PR review verb** (Verb 3) — phase 4.
- **Conversation history** (Verb 4) — phase 5.
- **Cleanup view + auto-archive** (Verb 5) — phase 6.
- **Settings UI** — phase 7.

The phase-3 endpoint (`POST /api/window/new-worktree`) is stable; later phases extend, not modify, this surface.
