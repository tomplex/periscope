# Project Model + New Project (Phase 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Verb 1 (new project) from `2026-05-15-workflow-management-design.md` plus the promote-tab-to-project subset of Verb 7. Add a `+ project` top-bar gesture that creates a fresh worktree, a tmux session pinned to it, and a 2-window (claude + shell) layout. Add a per-tab "promote to project" action on main-session tabs.

**Phase 2 ships v1 of the worktree-creation primitive.** The v1 worktree-integration spec (`2026-05-13-worktree-integration-design.md`) never landed in code — its `POST /api/window/new-worktree` endpoint doesn't exist. Phase 2 builds the primitive internally so Verb 1 (this phase) and Verb 2 (phase 3 — new worktree-tab inside an existing project) share a single spawn path.

**Architecture:** A new `periscope/worktree_spawn.py` module owns the `spawn_worktree(repo, branch, base_branch)` function. It acquires `repo_locks.repo_lock(repo)` (introduced in phase 1), runs `git fetch origin <base>` non-fatally, then `git worktree add -b <branch> <path> origin/<base>`. Returns the worktree path. Used by:
- `POST /api/projects` in `periscope/routes/projects.py` — extends the existing routes file with a new endpoint.
- (Future) phase 3's worktree-tab spawn.

Frontend: a new `static/new-project-modal.js` plus HTML/CSS slots in `index.html`/`styles.css`, modeled on the existing `commands-modal.js`. A `+ project` button in the top-bar opens the modal; submitting calls `POST /api/projects`.

Promote-tab-to-project: a `⋯`-style "promote" action on tabs in the `__main__` project. Resolves cwd to git toplevel, registers a project pinned there, `tmux move-window` the tab into the new session.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git. No test suite. Each task ends with a **Verify** step.

**Spec:** `docs/superpowers/specs/2026-05-15-workflow-management-design.md` §Verb 1, §Verb 7 (promote-tab subset).

**Design call recorded in conversation:** worktree spawn forks from `origin/<default>` after `git fetch`. Local `<default>` ref is NOT used (may be stale). Local main checkout's HEAD is NEVER mutated (no `pull`, no `checkout`).

**What's explicitly NOT in phase 2:**
- Worktree-layout config (sibling vs inline vs custom) — phase 7. Hardcode `sibling`: `~/dev/worktrees/<repo-basename>/<branch-slugged>`.
- `repos_dir` setting — phase 7. Hardcode `~/dev` directory scan + union of existing-project repos for the repo picker.
- New worktree-tab inside an existing project (Verb 2 worktree mode) — phase 3.
- PR review — phase 4.
- Window-layout configurability — phase 7. Hardcode 2-window (claude + shell).
- Branch-naming template `<initials>/<YYYYMMDD>-<slug>` — phase 3+. Phase 2 accepts free-form branch names.

---

## File Structure

**Created:**
- `periscope/worktree_spawn.py` — the spawn primitive. `spawn_worktree(repo, branch, base_branch=None) -> {path, base_branch, warning?}`.
- `static/new-project-modal.js` — the "+ project" form modal (open/close, repo/branch fetch, submit).

**Modified:**
- `periscope/routes/projects.py` — adds `POST /api/projects` and a `GET /api/projects/discoverable` helper that returns `{repos: [...], branches_by_repo: {...}}` to populate the modal's pickers.
- `static/grid.js` — adds `+ project` button + "promote to project" menu item on main-session tabs.
- `static/index.html` — adds the new-project-modal markup + the `+ project` top-bar button.
- `static/styles.css` — modal styles (reuses commands-modal patterns).
- `static/app.js` — wires `initNewProjectModal()` at boot.

---

## Task 1: worktree spawn primitive

**Files:**
- Create: `periscope/worktree_spawn.py`

- [ ] **Step 1: Write the module**

Create `periscope/worktree_spawn.py`:

```python
"""Worktree creation primitive.

Caller passes a repo path + new branch name + optional base. We:
  1. Acquire per-repo lock (repo_locks.repo_lock).
  2. Resolve default branch via `git symbolic-ref refs/remotes/origin/HEAD`,
     fallback `main`/`master`.
  3. `git fetch origin <base>` (non-fatal — proceed with stale tracking
     ref on failure, surface a warning).
  4. `git worktree add -b <branch> <path> origin/<base>`. Path layout
     is hardcoded sibling: `~/dev/worktrees/<repo-basename>/<branch>`
     (with `/` in branch → `-` for path safety).
  5. Invalidate the worktrees cache for the repo so the next
     /api/state poll re-runs `git worktree list`.

Local default-branch ref is NOT touched. Local main-checkout HEAD is
NOT touched. See the workflow-management spec §Verb 1 + the v1
worktree-integration spec §"Pre-spawn fetch".
"""

import os
import re
from pathlib import Path

from periscope import worktrees
from periscope.log import log
from periscope.repo_locks import repo_lock
from periscope.tmux import _run


WORKTREES_DIR = Path.home() / "dev" / "worktrees"


def _slug_for_path(branch: str) -> str:
    """`/` → `-` so `tc/foo` becomes `tc-foo` on disk. Strips any
    characters that aren't safe for a directory name; collapses repeats.
    """
    s = re.sub(r"[^A-Za-z0-9._/-]", "-", branch)
    s = s.replace("/", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "branch"


def _detect_default_branch(repo: str) -> str:
    """Returns 'main' / 'master' / similar. Falls back to 'main' if
    nothing matches — caller's fetch will then fail loudly."""
    code, ref = _run(
        ["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"]
    )
    if code == 0 and ref:
        # e.g. refs/remotes/origin/main → "main"
        return ref.rsplit("/", 1)[-1]
    # Fallback: probe local branches.
    code, out = _run(
        ["git", "-C", repo, "branch", "--format=%(refname:short)"]
    )
    branches = out.split("\n") if code == 0 else []
    for candidate in ("main", "master"):
        if candidate in branches:
            return candidate
    return "main"


def spawn_worktree(
    repo: str,
    branch: str,
    base_branch: str | None = None,
) -> dict:
    """Create a worktree of `repo` at branch `branch`, forked from
    `origin/<base_branch>` (or the detected default branch).

    Returns:
      {
        "path": <absolute worktree path>,
        "base_branch": <resolved base branch name>,
        "branch": <new branch name as created>,
        "warning": <optional message about non-fatal fetch failure>,
      }

    Raises:
      ValueError if `branch` is empty, `repo` doesn't exist, the
      computed worktree path already exists, or `git worktree add`
      fails.
    """
    if not branch:
        raise ValueError("branch is required")
    repo = os.path.realpath(repo)
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(
        os.path.join(repo, ".git")
    ):
        # .git can be a dir (normal checkout) or a file (worktree itself).
        # Either way it must exist.
        raise ValueError(f"not a git repo: {repo}")

    base = base_branch or _detect_default_branch(repo)

    repo_name = os.path.basename(repo.rstrip("/"))
    wt_path = WORKTREES_DIR / repo_name / _slug_for_path(branch)
    wt_path_str = str(wt_path)

    if wt_path.exists():
        raise ValueError(f"worktree path already exists: {wt_path_str}")

    # Branch-name safety: reject anything that would be interpreted as a
    # git flag. `--` after the flag/path positional arguments doesn't help
    # here because -b takes the branch as its value — a leading `-` in
    # the branch name still trips git. Reject it.
    if branch.startswith("-"):
        raise ValueError(f"branch name cannot start with '-': {branch!r}")

    warning: str | None = None

    # Fetch runs OUTSIDE the per-repo lock. It's a long-running network op
    # (up to 30s) and is idempotent vs. concurrent fetches — holding the
    # lock would block every other spawn on this repo for the duration.
    # Phase-1's repo_locks.py:33-35 documents this: "Callers should hold
    # the lock only across the git mutation itself, not surrounding work."
    fetch_code, fetch_out = _run(
        ["git", "-C", repo, "fetch", "origin", base], timeout=30.0
    )
    if fetch_code != 0:
        warning = f"fetch failed: origin/{base} may be stale ({fetch_out!r})"
        log.warning("worktree_spawn: %s", warning)

    with repo_lock(repo):
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        (WORKTREES_DIR / repo_name).mkdir(parents=True, exist_ok=True)

        # Create the worktree from origin/<base>. -b creates the new
        # branch; the base ref (origin/<base>) is what `git worktree add`
        # forks from. The local <base> branch ref is not touched.
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
        if code != 0:
            raise ValueError(f"git worktree add failed: {out}")

    worktrees.invalidate(repo)

    result = {"path": wt_path_str, "base_branch": base, "branch": branch}
    if warning:
        result["warning"] = warning
    return result
```

- [ ] **Step 2: Verify**

Run an end-to-end spawn against a throwaway repo:

```bash
cd /Users/tom/dev/periscope && uv run python3 << 'EOF'
import tempfile, subprocess, os, shutil
from periscope.worktree_spawn import spawn_worktree, _slug_for_path
from periscope.worktrees import _cached_worktrees

# Create a fake "repo with origin" — git init, add a fake remote pointing at
# a bare repo with a main branch.
with tempfile.TemporaryDirectory() as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    bare = os.path.join(tmpdir, "bare.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    # Make sure origin/HEAD points at main.
    subprocess.run(["git", "-C", repo, "remote", "set-head", "origin", "main"], check=True)

    res = spawn_worktree(repo, "tc/test-spawn")
    print("result:", res)
    assert os.path.isdir(res["path"]), "worktree path must exist"
    assert res["base_branch"] == "main"
    assert res["branch"] == "tc/test-spawn"

    # Confirm worktrees cache sees the new worktree on next call.
    wts = _cached_worktrees(repo)
    paths = [p for p, _b in wts]
    print("worktrees:", paths)
    assert res["path"] in paths

    # Cleanup the worktree (it lives in ~/dev/worktrees/, outside the tmpdir).
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", res["path"]])

    # Slug sanity.
    assert _slug_for_path("tc/foo/bar") == "tc-foo-bar"
    assert _slug_for_path("tc/foo bar!") == "tc-foo-bar"
    print("PASS")
EOF
```

Expected: `PASS`. The `~/dev/worktrees/repo/tc-test-spawn` is created and then removed. Slug coverage passes.

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-2 commit -am "worktree_spawn: spawn_worktree(repo, branch) primitive — fetch + worktree-add from origin/<default>"
```

---

## Task 2: POST /api/projects + discoverable helper

**Files:**
- Modify: `periscope/routes/projects.py` (add two endpoints)

- [ ] **Step 1: Add the create + discoverable endpoints**

Add to `periscope/routes/projects.py` (alongside the existing endpoints):

```python
# At the top, alongside existing imports. NOTE: `log` is already
# imported (line 21) and `_run`/`_tmux_mutate` are already imported
# (line 27); extend `_tmux_mutate`'s line to also import `tmux`:
from pathlib import Path

from periscope.worktree_spawn import spawn_worktree, _detect_default_branch

# Existing `from periscope.tmux import _run, _tmux_mutate` line becomes:
# from periscope.tmux import _run, _tmux_mutate, tmux

# Where the other body models live, add:
class CreateBody(BaseModel):
    repo: str
    branch: str
    name: str | None = None  # auto-fills to branch if absent


def _layout_two_window(tmux_session: str, pinned_dir: str) -> None:
    """Apply the trellis-style 2-window layout: window 1 'claude',
    window 2 'shell'. tmux session is created from scratch and ends with
    window 1 active. The user is NOT attached — periscope is a dashboard,
    not a terminal client.

    The 100ms sleep before each send-keys lets the shell finish loading
    its rc file before the command lands (see CLAUDE.md "Key invariants"
    note 5). Without it, `claude` can land mid-rc and either get echoed
    as text or fail silently.
    """
    import time

    # new-session creates window 0 (or whatever base-index is) with a bare
    # shell at cwd = pinned_dir.
    ok, msg = _tmux_mutate(
        "new-session", "-d", "-s", tmux_session, "-c", pinned_dir,
        "-n", "claude",
    )
    if not ok:
        raise HTTPException(500, f"tmux new-session failed: {msg}")

    # Send `claude` into window 1.
    time.sleep(0.1)
    _tmux_mutate(
        "send-keys", "-t", f"{tmux_session}:claude", "claude", "Enter",
    )

    # Window 2: shell.
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{tmux_session}:", "-c", pinned_dir,
        "-n", "shell",
    )
    if not ok:
        # Worktree + session + window 1 already exist; don't roll back.
        log.warning("new-project: failed to create shell window: %s", msg)

    # Park focus on window 1 (claude).
    _tmux_mutate("select-window", "-t", f"{tmux_session}:claude")

    # Stamp focus + action so the new project sorts to the top of the
    # grid + stream views on the next poll. Match the pattern in
    # routes/sessions.py:46-47 for `+ session`.
    from periscope.panes import note_focus, note_action
    # The claude window is the first one created; its tmux window index
    # depends on base-index. Resolve it by looking up the window-id.
    idx_out = tmux(
        "display-message", "-t", f"{tmux_session}:claude",
        "-p", "#{window_index}",
    ).strip()
    if idx_out.isdigit():
        target = f"{tmux_session}:{idx_out}"
        note_focus(target)
        note_action(target)


@router.post("/api/projects")
def projects_create(body: CreateBody):
    """Create a new project: spawn worktree if branch != default,
    create tmux session, apply 2-window layout, register project."""
    repo = os.path.realpath(body.repo)
    if not os.path.isdir(repo):
        raise HTTPException(400, f"repo does not exist: {body.repo}")
    code, toplevel = _run(["git", "-C", repo, "rev-parse", "--show-toplevel"])
    if code != 0 or not toplevel:
        raise HTTPException(400, f"not a git repo: {body.repo}")
    repo = os.path.realpath(toplevel)

    branch = body.branch.strip()
    if not branch:
        raise HTTPException(400, "branch is required")
    if branch.startswith("-"):
        raise HTTPException(400, f"branch name cannot start with '-': {branch!r}")

    default = _detect_default_branch(repo)

    # For the branch == default path the pinned_dir is the repo root.
    # We can detect that collision UP FRONT and 409 before doing any
    # tmux/git work, avoiding orphan state on failure.
    if branch == default and repo in all_projects():
        raise HTTPException(
            409, f"project already exists at {repo!r}"
        )

    pinned_dir: str
    warning: str | None = None
    if branch == default:
        # No worktree — project pins to repo root.
        pinned_dir = repo
    else:
        try:
            res = spawn_worktree(repo, branch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        pinned_dir = res["path"]
        warning = res.get("warning")

        # Belt-and-suspenders: after spawn, re-check the pinned_dir isn't
        # already adopted. spawn_worktree already rejected if the path
        # exists on disk, so this is mostly defensive against a racy
        # adoption during the fetch+add window. Path-on-disk + project-
        # row collision is essentially impossible in practice.
        if pinned_dir in all_projects():
            raise HTTPException(
                409, f"project already exists at {pinned_dir!r}"
            )

    name = (body.name or branch).strip()
    # Tmux session name: same as `name` by default. Collisions surface as
    # tmux errors below.
    tmux_session = name

    try:
        _layout_two_window(tmux_session, pinned_dir)
    except HTTPException:
        # tmux failed — leave the worktree on disk so the user can retry
        # adoption or clean up manually. Don't rollback the git side.
        raise

    try:
        row = create_project(
            pinned_dir,
            name=name,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch,
        )
    except ValueError as e:
        # Race: someone adopted between the 409-check and here. Rare.
        raise HTTPException(409, str(e))

    result = {"ok": True, "pinned_dir": pinned_dir, **row}
    if warning:
        result["warning"] = warning
    return result


@router.get("/api/projects/discoverable")
def projects_discoverable():
    """Return the union of (a) currently-known project repos and (b) git
    repos discovered under ~/dev (one level deep). Plus the local branch
    list per repo, capped at 100 each for sanity.

    Frontend uses this to populate the new-project modal's repo/branch
    pickers.
    """
    repos: set[str] = set()

    for p in all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"]))

    dev = Path.home() / "dev"
    if dev.is_dir():
        for child in dev.iterdir():
            if not child.is_dir():
                continue
            # Skip hidden and the worktrees container itself.
            if child.name.startswith(".") or child.name == "worktrees":
                continue
            if (child / ".git").exists():
                repos.add(str(child.resolve()))

    branches_by_repo: dict[str, list[str]] = {}
    for repo in sorted(repos):
        code, out = _run(
            ["git", "-C", repo, "branch", "--format=%(refname:short)"],
            timeout=3.0,
        )
        if code == 0:
            branches_by_repo[repo] = out.split("\n")[:100] if out else []
        else:
            branches_by_repo[repo] = []

    return {
        "repos": sorted(repos),
        "branches_by_repo": branches_by_repo,
    }
```

- [ ] **Step 2: Verify both endpoints**

```bash
cd /Users/tom/dev/periscope && XDG_CONFIG_HOME=/tmp/periscope-verify-task2 uv run --with httpx python3 << 'EOF'
import tempfile, subprocess, os
from fastapi.testclient import TestClient
from periscope.app import app

client = TestClient(app)

# /api/projects/discoverable — should include /Users/tom/dev/periscope and
# whatever else is under ~/dev.
r = client.get("/api/projects/discoverable")
data = r.json()
print(f"discoverable status: {r.status_code}, repo count: {len(data['repos'])}")
assert r.status_code == 200
assert "/Users/tom/dev/periscope" in data["repos"]
assert data["branches_by_repo"]["/Users/tom/dev/periscope"]

# POST /api/projects — create a project with a new branch.
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

    sess = "ptest-new-proj"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    try:
        r = client.post("/api/projects", json={
            "repo": repo, "branch": "tc/phase2-smoke", "name": sess,
        })
        print(f"create status: {r.status_code}, body: {r.json()}")
        assert r.status_code == 200
        # Worktree exists.
        assert os.path.isdir(r.json()["pinned_dir"])
        # Tmux session exists with 2 windows.
        code = subprocess.run(["tmux", "has-session", "-t", sess]).returncode
        assert code == 0
        out = subprocess.run(["tmux", "list-windows", "-t", sess, "-F", "#{window_name}"],
                              capture_output=True, text=True).stdout.strip().split("\n")
        print(f"tmux windows: {out}")
        assert "claude" in out and "shell" in out
        wt_path = r.json()["pinned_dir"]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        # Remove the worktree (lives outside tmpdir).
        try:
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt_path],
                           capture_output=True)
        except Exception:
            pass

print("PASS")
EOF
rm -rf /tmp/periscope-verify-task2
```

Expected: `PASS`. Discoverable returns ≥1 repo and a non-empty branch list for periscope. Create returns 200, the worktree directory exists, tmux session has both `claude` and `shell` windows.

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-2 commit -am "routes/projects: POST /api/projects (create) + GET /api/projects/discoverable"
```

---

## Task 3: index.html slots for the new-project modal + top-bar button

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add the `+ project` button to the filter bar**

In `static/index.html`, the existing `+ session` button at line 40 is the right model. Add a `+ project` button immediately after it, inside the same `<nav class="filters">` block:

```html
<button id="new-project-btn" class="filter-btn is-action" title="create a new project (new worktree + tmux session)">+ project</button>
```

Place it between `#new-session` and `#send-bulk` so the action-button cluster reads `+ session | + project | (bulk) | (toggle) | (gear)`.

- [ ] **Step 2: Add the modal markup**

Add after the existing `#commands-modal` div (around line 51 of `static/index.html`):

```html
  <div id="new-project-modal" class="hidden new-project-modal-overlay">
    <div class="new-project-modal-card">
      <header class="new-project-modal-head">
        <h2>+ project</h2>
        <button id="new-project-modal-close" title="close">×</button>
      </header>
      <p class="new-project-modal-sub">Creates a worktree off <code>origin/&lt;default&gt;</code>, a tmux session, and a 2-window claude+shell layout.</p>
      <form id="new-project-form">
        <label>
          Repo
          <input id="new-project-repo" list="new-project-repos" placeholder="/Users/tom/dev/foo" required>
          <datalist id="new-project-repos"></datalist>
        </label>
        <label>
          Branch
          <input id="new-project-branch" list="new-project-branches" placeholder="tc/new-feature" required>
          <datalist id="new-project-branches"></datalist>
        </label>
        <label>
          Name <span class="new-project-modal-hint">(defaults to branch)</span>
          <input id="new-project-name" placeholder="optional">
        </label>
        <div id="new-project-error" class="new-project-modal-error" hidden></div>
        <div class="new-project-modal-actions">
          <button type="button" id="new-project-cancel">cancel</button>
          <button type="submit" id="new-project-submit">create</button>
        </div>
      </form>
    </div>
  </div>
```

- [ ] **Step 3: Verify markup loads**

Just confirm the page still loads — visit http://127.0.0.1:8765/ after periscope picks up the change (Vite or full restart). The new button should show in the top-bar; clicking it does nothing until Task 4 wires it up.

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-2 commit -am "index.html: + project top-bar button + new-project-modal markup"
```

---

## Task 4: new-project-modal.js — open, populate, submit

**Files:**
- Create: `static/new-project-modal.js`
- Modify: `static/app.js` (wire `initNewProjectModal` at boot)

- [ ] **Step 1: Write the modal module**

Create `static/new-project-modal.js`:

```javascript
// New-project modal. Open/close + populate repo/branch pickers from
// /api/projects/discoverable, submit to /api/projects, close on success.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("new-project-modal");
const closeBtn = document.getElementById("new-project-modal-close");
const cancelBtn = document.getElementById("new-project-cancel");
const form = document.getElementById("new-project-form");
const repoInput = document.getElementById("new-project-repo");
const branchInput = document.getElementById("new-project-branch");
const nameInput = document.getElementById("new-project-name");
const reposListEl = document.getElementById("new-project-repos");
const branchesListEl = document.getElementById("new-project-branches");
const errorEl = document.getElementById("new-project-error");
const submitBtn = document.getElementById("new-project-submit");

// In-memory cache of the last /api/projects/discoverable response.
// Keyed lookups: when the user changes repo, we filter the branch
// datalist to that repo's branches.
let cached = { repos: [], branches_by_repo: {} };
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderRepoOptions() {
  reposListEl.innerHTML = cached.repos
    .map((r) => `<option value="${escapeHtml(r)}">`)
    .join("");
}

function renderBranchOptions() {
  const repo = repoInput.value.trim();
  const branches = cached.branches_by_repo[repo] || [];
  branchesListEl.innerHTML = branches
    .map((b) => `<option value="${escapeHtml(b)}">`)
    .join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/projects/discoverable");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    cached = await res.json();
    renderRepoOptions();
    renderBranchOptions();
  } catch (e) {
    showError(`failed to load repos: ${e.message}`);
  }
}

export async function openNewProjectModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  modal.classList.remove("hidden");
  document.body.classList.add("new-project-modal-open");
  pushEscape(closeNewProjectModal);
  repoInput.value = "";
  branchInput.value = "";
  nameInput.value = "";
  // Populate datalists.
  await refresh();
  // Focus the repo input so keyboard-only users can start typing.
  repoInput.focus();
}

export function closeNewProjectModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("new-project-modal-open");
  popEscape(closeNewProjectModal);
}

async function handleSubmit(e) {
  e.preventDefault();
  clearError();
  const repo = repoInput.value.trim();
  const branch = branchInput.value.trim();
  const name = nameInput.value.trim();
  if (!repo || !branch) {
    showError("repo and branch are required");
    return;
  }
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, branch, name: name || undefined }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    const result = await res.json();
    if (result.warning) {
      // Non-fatal — still close, but log so the dev console shows it.
      console.warn("new-project warning:", result.warning);
    }
    closeNewProjectModal();
  } catch (e) {
    showError(`request failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initNewProjectModal() {
  const openBtn = document.getElementById("new-project-btn");
  if (openBtn) openBtn.addEventListener("click", openNewProjectModal);
  closeBtn.addEventListener("click", closeNewProjectModal);
  cancelBtn.addEventListener("click", closeNewProjectModal);
  modal.addEventListener("click", (e) => {
    // Click on the overlay (not the card) closes.
    if (e.target === modal) closeNewProjectModal();
  });
  repoInput.addEventListener("change", renderBranchOptions);
  repoInput.addEventListener("input", renderBranchOptions);
  form.addEventListener("submit", handleSubmit);
}
```

- [ ] **Step 2: Wire `initNewProjectModal` in app.js**

`static/app.js` already imports + initializes the commands-modal. Add alongside:

- At line 9 (alongside the existing `import { initCommandsModal, openCommandsModal } from './commands-modal.js';`):
  ```javascript
  import { initNewProjectModal } from './new-project-modal.js';
  ```
- At line 134 (alongside the existing `initCommandsModal();`):
  ```javascript
  initNewProjectModal();
  ```

Place the new lines on the line *after* the existing ones for a clean diff.

- [ ] **Step 3: Add CSS**

Add to `static/styles.css` — clone the commands-modal pattern so visual treatment matches. Easiest: add a comma-separated selector to existing rules. Concrete:

```css
.new-project-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}
.new-project-modal-overlay.hidden { display: none; }
.new-project-modal-card {
  background: var(--bg-1);
  color: var(--fg-0);
  border-radius: 0.75em;
  padding: 1.25em 1.5em;
  width: min(520px, 90vw);
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
}
.new-project-modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.25em;
}
.new-project-modal-head h2 { margin: 0; font-size: 1.1em; }
.new-project-modal-head button {
  background: none;
  border: none;
  color: var(--fg-0);
  font-size: 1.5em;
  cursor: pointer;
}
.new-project-modal-sub {
  font-size: 0.85em;
  opacity: 0.75;
  margin: 0 0 1em 0;
}
.new-project-modal-sub code {
  font-family: var(--font-mono, ui-monospace, monospace);
  background: color-mix(in oklch, var(--fg-0) 8%, transparent);
  padding: 0.1em 0.3em;
  border-radius: 0.3em;
}
#new-project-form label {
  display: block;
  margin-bottom: 0.75em;
  font-size: 0.85em;
  opacity: 0.9;
}
#new-project-form input {
  display: block;
  width: 100%;
  margin-top: 0.25em;
  padding: 0.4em 0.6em;
  border: 1px solid color-mix(in oklch, var(--fg-0) 20%, transparent);
  border-radius: 0.4em;
  background: var(--bg-0);
  color: var(--fg-0);
  font-family: inherit;
  font-size: 0.95em;
}
.new-project-modal-hint {
  font-size: 0.8em;
  opacity: 0.6;
}
.new-project-modal-error {
  background: color-mix(in oklch, var(--warn, #d97706) 15%, transparent);
  color: var(--warn, #d97706);
  border-radius: 0.4em;
  padding: 0.5em 0.75em;
  margin-bottom: 0.75em;
  font-size: 0.85em;
}
.new-project-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5em;
  margin-top: 1em;
}
.new-project-modal-actions button {
  padding: 0.5em 1em;
  border: 1px solid color-mix(in oklch, var(--fg-0) 20%, transparent);
  border-radius: 0.4em;
  background: var(--bg-0);
  color: var(--fg-0);
  cursor: pointer;
  font-size: 0.9em;
}
#new-project-submit {
  background: var(--accent, #4a90e2);
  color: white;
  border-color: var(--accent, #4a90e2);
}
#new-project-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

If `--bg-1` / `--fg-0` etc. don't exist in your stylesheet, use whatever the commands-modal uses. Match the visual treatment.

- [ ] **Step 4: Verify end-to-end via browser**

Restart periscope to pick up the new module. Open http://127.0.0.1:8765/.

1. Click `+ project` in the top-bar → modal opens.
2. Click the repo input → datalist shows discovered repos.
3. Pick a repo → branch datalist populates with that repo's branches.
4. Type a new branch name `tc/phase2-test`, leave name blank.
5. Click `create` → modal closes, dashboard refreshes within ~3s with the new project visible.
6. Confirm the worktree exists: `ls ~/dev/worktrees/<repo>/tc-phase2-test`.
7. Confirm tmux session: `tmux ls | grep tc/phase2-test` (or whatever the auto-generated name was).
8. Cleanup: `⋯` → archive → archive triggers tmux kill (not yet — archive only sets archived_at). For now: `tmux kill-session -t <name>; git -C <repo> worktree remove ~/dev/worktrees/<repo>/tc-phase2-test`.

If you can't open the browser, fall back to a TestClient curl:

```bash
cd /Users/tom/dev/periscope && uv run --with httpx python3 -c "
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)
r = client.get('/api/projects/discoverable')
print('discoverable:', r.json()['repos'][:3])
"
```

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-2 commit -am "new-project-modal: open/close, populate from /api/projects/discoverable, submit to /api/projects"
```

---

## Task 5: promote-tab-to-project on main-session tabs

**Files:**
- Modify: `static/grid.js` (per-card "promote" affordance for `__main__` tabs)
- Modify: `periscope/routes/projects.py` (add `POST /api/projects/promote`)

This verb is a thin wrapper: resolve the tab's cwd → git toplevel, create the project pinned there, `tmux move-window` to move the tab into the new project's tmux session.

- [ ] **Step 1: Backend endpoint**

Add to `periscope/routes/projects.py`:

```python
class PromoteBody(BaseModel):
    # The window to promote (tmux addressing).
    session: str
    index: int
    # Optional override of the auto-derived name.
    name: str | None = None


@router.post("/api/projects/promote")
def projects_promote(body: PromoteBody):
    """Promote a tab in the main project to its own project. Resolves
    the tab's cwd to a git toplevel, creates a project pinned there
    (409 if one exists), creates a tmux session named after the project,
    and moves the window in via `tmux move-window`.
    """
    target = f"{body.session}:{body.index}"
    # Look up the window's cwd via tmux. Use `_run` instead of `tmux()`
    # so we can distinguish "window not found" (non-zero exit) from
    # "legitimately empty cwd" (zero exit, empty stdout — rare but
    # possible mid-tmux-startup).
    code, cwd_out = _run(
        ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"]
    )
    if code != 0:
        raise HTTPException(404, f"window not found: {target}")
    cwd_out = cwd_out.strip()
    if not cwd_out:
        raise HTTPException(400, f"window {target} has empty cwd")

    code, toplevel = _run(
        ["git", "-C", cwd_out, "rev-parse", "--show-toplevel"]
    )
    if code != 0 or not toplevel:
        raise HTTPException(
            400, f"tab cwd is not inside a git repo: {cwd_out}"
        )
    pinned_dir = os.path.realpath(toplevel)

    if pinned_dir in all_projects():
        raise HTTPException(
            409, f"project already exists at {pinned_dir!r}"
        )

    # Resolve repo via --git-common-dir (matches Task 1's migration +
    # adopt endpoints).
    code, common = _run(
        ["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"]
    )
    if code == 0 and common:
        common_abs = (
            common if os.path.isabs(common) else os.path.join(pinned_dir, common)
        )
        repo = os.path.realpath(os.path.dirname(common_abs))
    else:
        repo = pinned_dir

    _, branch = _run(
        ["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"]
    )
    if branch == "HEAD":
        branch = ""

    name = (body.name or os.path.basename(pinned_dir)).strip()
    tmux_session = name

    # Create the new tmux session and capture the auto-created window's
    # id so we can kill it after the move (rather than guessing by index,
    # which would break under `renumber-windows`). `-P -F '#{window_id}'`
    # matches the existing pattern in routes/sessions.py:121-122.
    ok, msg = _tmux_mutate(
        "new-session", "-d", "-s", tmux_session, "-c", pinned_dir,
        "-P", "-F", "#{window_id}",
    )
    if not ok:
        raise HTTPException(500, f"tmux new-session failed: {msg}")
    auto_window_id = msg.strip()  # e.g. "@42"

    # Move the source window in. Without a -t index, tmux picks the next
    # free slot.
    ok, msg = _tmux_mutate(
        "move-window", "-s", target, "-t", f"{tmux_session}:",
    )
    if not ok:
        # Rollback the empty session.
        _tmux_mutate("kill-session", "-t", tmux_session)
        raise HTTPException(500, f"tmux move-window failed: {msg}")

    # Kill the auto-created blank window by its window-id (NOT by index —
    # safe against `renumber-windows`).
    if auto_window_id:
        _tmux_mutate("kill-window", "-t", auto_window_id)

    try:
        row = create_project(
            pinned_dir,
            name=name,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch or None,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))

    return {"ok": True, "pinned_dir": pinned_dir, **row}
```

No new imports needed for this task — `_run` and `_tmux_mutate` are already imported by `routes/projects.py` (Task 2 may have also added `tmux`, which is harmless here).

- [ ] **Step 2: Frontend menu item**

Find the per-card menu (the `⋯` affordance lives on the session header for project-level actions per Phase 1; per-tab "promote" needs a SEPARATE affordance). The simplest approach: add a small button to the card itself, only rendered when the tab is in the `__main__` project AND its cwd resolves to a git toplevel (the latter is signaled by `worktree_affiliation.kind !== "no-repo"`).

In `static/grid.js`'s `renderCard`, near the existing `.card-kill` button (search for `card-kill`), add:

```javascript
  // Promote-to-project: only on tabs in the __main__ project, only
  // when the cwd is inside a git repo (worktree_affiliation tells us).
  const isMainTab = w.project_pinned_dir === "__main__";
  const aff = w.worktree_affiliation || {};
  const canPromote = isMainTab && aff.kind !== "no-repo";
  const promoteBtn = canPromote
    ? `<button class="card-promote" data-session="${escapeHtml(w.session)}" data-index="${w.index}" title="promote this tab to its own project">↗ promote</button>`
    : "";
```

And include `${promoteBtn}` in the card template wherever the other per-card buttons live. Search for the card kill-button or actions row; the promote button is the same shape (small button, top-right of the card).

In the click delegation cascade, add a handler:

```javascript
async function handlePromote(btn) {
  const session = btn.dataset.session;
  const index = parseInt(btn.dataset.index, 10);
  if (!session || !Number.isFinite(index)) return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/projects/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, index }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`promote failed: ${err.detail || res.status}`);
    }
  } finally {
    btn.disabled = false;
  }
}
```

And in the sync cascade:

```javascript
    const promoteBtn = e.target.closest(".card-promote");
    if (promoteBtn) {
      e.stopPropagation();
      handlePromote(promoteBtn);
      return;
    }
```

- [ ] **Step 3: CSS**

Add to `static/styles.css`:

```css
.card-promote {
  font-size: 0.7em;
  padding: 0.1em 0.4em;
  border-radius: 0.3em;
  border: 1px dashed currentColor;
  background: transparent;
  opacity: 0.6;
  cursor: pointer;
}
.card-promote:hover { opacity: 1; }
.card-promote:disabled { opacity: 0.3; cursor: not-allowed; }
```

- [ ] **Step 4: Verify**

```bash
cd /Users/tom/dev/periscope && XDG_CONFIG_HOME=/tmp/periscope-verify-promote uv run --with httpx python3 << 'EOF'
import subprocess, os, tempfile
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)

# Set up a fake "main" tab in a real git repo.
with tempfile.TemporaryDirectory() as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)

    # Spawn a window in the live `main` tmux session pointing at the repo.
    # If `main` doesn't exist, create it.
    code = subprocess.run(["tmux", "has-session", "-t", "main"]).returncode
    if code != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", "main", "-c", os.path.expanduser("~")], check=True)
    # Add a window in the repo.
    res = subprocess.run(
        ["tmux", "new-window", "-t", "main:", "-c", repo, "-P", "-F", "#{window_index}"],
        capture_output=True, text=True, check=True,
    )
    idx = int(res.stdout.strip())
    print(f"created main:{idx} at {repo}")

    try:
        # Adopt main first so promote sees it as the source.
        # (In real life main is auto-created on first migration.)
        # The migration already set up __main__ from your live tmux state,
        # so we can just call promote directly.
        r = client.post("/api/projects/promote", json={
            "session": "main", "index": idx,
        })
        print(f"promote status: {r.status_code}, body: {r.json()}")
        assert r.status_code == 200
        new_session = r.json()["tmux_session"]
        # Tmux: the new session exists, the moved window is in it, the main
        # session no longer has the source window.
        ls = subprocess.run(["tmux", "list-windows", "-t", new_session, "-F", "#{window_index}"],
                            capture_output=True, text=True).stdout.strip().split("\n")
        print(f"new session windows: {ls}")
        main_ls = subprocess.run(["tmux", "list-windows", "-t", "main", "-F", "#{window_index}"],
                                 capture_output=True, text=True).stdout.strip().split("\n")
        print(f"main windows after move: {main_ls}")
        assert str(idx) not in main_ls
    finally:
        subprocess.run(["tmux", "kill-session", "-t", r.json().get("tmux_session", "_nope")], capture_output=True)
print("PASS")
EOF
rm -rf /tmp/periscope-verify-promote
```

Expected: `PASS`. Promote returns 200, the source window vanishes from main, the new session has it.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-2 commit -am "promote-tab-to-project: backend endpoint + per-card affordance on main tabs"
```

---

## Task 6: end-to-end smoke test

Final integration check before merge.

- [ ] **Step 1: Backup state.json**

```bash
cp ~/.config/periscope/state.json ~/.config/periscope/state.json.before-phase-2
```

- [ ] **Step 2: Restart periscope on the new code**

```bash
launchctl kickstart -k gui/$(id -u)/com.tom.periscope
sleep 5
curl -s http://127.0.0.1:8765/api/healthz | python3 -m json.tool
```

- [ ] **Step 3: Browser smoke**

Visit http://127.0.0.1:8765/.

1. Click `+ project` → modal opens, repo + branch datalists populated.
2. Pick `/Users/tom/dev/periscope`, branch `tc/phase2-smoke-real`, leave name blank → submit.
3. Modal closes, dashboard refreshes; new project visible with the worktree path under its header.
4. Confirm `ls ~/dev/worktrees/periscope/tc-phase2-smoke-real` shows the worktree.
5. Confirm `tmux ls | grep tc/phase2-smoke-real` shows the new session with 2 windows.
6. Find a tab in the `main` project that's in a git repo (e.g. `vault-token-pr`). Click `↗ promote` on its card.
7. The tab vanishes from main and appears as a new project pinned to its cwd.
8. Cleanup: archive the smoke project + promoted project via `⋯` menu; or manually kill the tmux sessions and remove worktrees.

- [ ] **Step 4: Cleanup test artifacts**

```bash
tmux kill-session -t tc/phase2-smoke-real 2>/dev/null
git -C /Users/tom/dev/periscope worktree remove --force ~/dev/worktrees/periscope/tc-phase2-smoke-real 2>/dev/null
```

---

## What's deliberately NOT in phase 2

- **New tab in existing project** (Verb 2) — phase 3. Builds on `spawn_worktree` from this phase, forking off `project.base_branch`.
- **PR review** (Verb 3) — phase 4.
- **Conversation history** (Verb 4) — phase 5.
- **Cleanup view + auto-archive logic** (Verb 5) — phase 6.
- **Settings UI** (Verb 8) — phase 7. Worktree layout per-repo, repos_dir, cleanup thresholds.
- **Branch-name template** `<initials>/<YYYYMMDD>-<slug>` — phase 3+. Phase 2 takes free-form branch names; if the user wants the template they can type it manually.
- **Window-layout configurability** — phase 7. Hardcoded 2-window claude+shell.

The phase-2 endpoints (`POST /api/projects`, `GET /api/projects/discoverable`, `POST /api/projects/promote`) are stable; later phases extend, not modify, this surface.
