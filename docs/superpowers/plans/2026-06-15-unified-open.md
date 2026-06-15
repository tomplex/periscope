# Unified "open" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace periscope's four `+ new` actions (session / project / PR review) plus the missing "open an existing directory" case with one omnibox UI and one server endpoint `POST /api/open` that turns any target descriptor into a live, rail-placed session.

**Architecture:** A new non-route module `periscope/open_ops.py` holds the whole feature core as plain functions — `open_target` dispatches three descriptor variants (`{path}`, `{repo,branch}`, `{repo,pr}`), with branch/PR reducing to the path case (resolve → register-if-absent → idempotent create-or-focus → server-side rail placement). `routes/open.py` is a thin APIRouter shim. The frontend gets a pure `open/classify.js` classifier + an `OpenOmnibox` modal that writes the server-returned `ui` blob straight into `prefsSignal`, killing the legacy 3500ms `deferRailAdd` timer.

**Tech Stack:** Python 3 / FastAPI / Pydantic (server, `uv run`), Preact + @preact/signals + Vite (frontend), pytest + vitest, tmux + git subprocesses.

**Specs:** `docs/superpowers/specs/2026-06-15-unified-open-design.md`, `docs/superpowers/specs/2026-06-15-unified-open-structure.md`

**Execution context:** Per periscope's CLAUDE.md dev workflow, implement in a worktree on port 8766 (`git worktree add ../periscope-open -b feature/unified-open`), then merge to main + `bin/periscope restart`. Rebuild `static/dist/app.js` (`npm run build`) before any commit that touches `static/src/`.

**Test commands:** `uv run pytest -q` (full), `uv run pytest tests/test_open_ops.py -q`, `npx vitest run static/src/open` (frontend unit).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `periscope/open_ops.py` | Create | Feature core: `open_target`, `ensure_project`, `ensure_session`, `worktree_for_branch`, `place_in_rail`, `build_catalog`, descriptors, `OpenResult` |
| `periscope/routes/open.py` | Create | APIRouter: `POST /api/open`, `GET /api/open/catalog`; parse → call core → map errors |
| `periscope/worktree_spawn.py` | Modify | `_layout_two_window` stamps BOTH windows, returns `(claude_pid, shell_pid)` |
| `periscope/projects.py` | Modify | Extract `fetch_pr_into_worktree` + `_discard_pr_worktree` out of the route |
| `periscope/routes/projects.py` | Modify | Delete `POST /api/projects` (create) and `POST /api/projects/pr-review` bodies |
| `periscope/routes/sessions.py` | Modify | Delete `POST /api/session/new` |
| `periscope/app.py` | Modify | Add `open` to the router include list |
| `static/src/open/classify.js` | Create | Pure `query → candidate cards` classifier |
| `static/src/overlays/OpenOmnibox.jsx` | Create | The omnibox modal |
| `static/src/prefs.js` | Modify | Add `setUI(uiBlob)` (non-network half of `patchUI`); delete `addWorktreeToRail` |
| `static/src/chrome/Header.jsx` | Modify | `+ new` becomes one button opening the omnibox |
| `static/src/overlays/Overlays.jsx` | Modify | Drop `+session` handler; mount `<OpenOmnibox/>`; remove 3 modals |
| `static/src/overlays/{NewProjectModal,ReviewPrModal,OpenPickerModal}.jsx` | Delete | Subsumed by the omnibox |

Tests: `tests/test_open_ops.py`, `tests/routes/test_open.py`, `tests/test_worktree_spawn.py` (edit), `tests/routes/test_projects.py` (trim), `tests/routes/test_sessions.py` (delete/trim), `static/src/open/__tests__/classify.test.js`.

---

## Task 0: Test harness — tmux socket seam, CLAUDE_EXEC override, fixtures

**Why (load-bearing safety):** `open_ops` tests exercise the REAL `_layout_two_window` / `ensure_session` against real tmux (deliberate — mocking tmux reproduces the Q1-2026 mock-passes/prod-fails class). But today `periscope.tmux` shells to the *default* tmux server and `_layout_two_window` `send-keys` the literal `CLAUDE_EXEC` (`claude …`) — so running the suite as-is spawns real Claude sessions on the dev machine. This task adds two seams so tests run on an isolated `-L` socket with a harmless stub exec, mirroring `tests/test_tmux_mirror.py`'s `-L periscope-mirror-test` pattern.

**Files:**
- Modify: `periscope/tmux.py`, `periscope/config.py`
- Modify: `tests/conftest.py` (add fixtures)

- [ ] **Step 1: Add the tmux socket seam**

In `periscope/tmux.py`, add a dynamic argv builder (read env per-call so a test fixture's `monkeypatch.setenv` takes effect) and route `tmux()` + `_tmux_mutate()` through it:

```python
import os

def _tmux_argv(*args: str) -> list[str]:
    sock = os.environ.get("PERISCOPE_TMUX_SOCKET")
    return ["tmux", *(("-L", sock) if sock else ()), *args]

def tmux(*args: str) -> str:
    r = subprocess.run(_tmux_argv(*args), capture_output=True, text=True, timeout=5)
    return r.stdout

def _tmux_mutate(*args: str) -> tuple[bool, str]:
    r = subprocess.run(_tmux_argv(*args), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "tmux failed")
    return True, r.stdout.strip()
```

`open_ops` will use `_tmux_mutate("has-session", "-t", name)[0]` for liveness (Task 4), so it inherits the socket — never raw `_run(["tmux", ...])`.

- [ ] **Step 2: Make `CLAUDE_EXEC` overridable**

In `periscope/config.py`, keep the constant as the default and add a reader:

```python
CLAUDE_EXEC = "claude --dangerously-load-development-channels server:periscope"

def claude_exec() -> str:
    return os.environ.get("PERISCOPE_CLAUDE_EXEC", CLAUDE_EXEC)
```

In `periscope/worktree_spawn.py:_layout_two_window`, change `from periscope.config import CLAUDE_EXEC` to `from periscope.config import claude_exec`, bind `exec_cmd = claude_exec()` once, and use `exec_cmd` in both the `send-keys` and the `"--dangerously-load-development-channels" in exec_cmd` check.

- [ ] **Step 3: Add fixtures to `tests/conftest.py`**

```python
import os, subprocess, uuid
from pathlib import Path

@pytest.fixture
def tmux_test_server(monkeypatch):
    """Isolated tmux server (-L) + a harmless CLAUDE_EXEC stub, so spawns
    don't touch the default server or launch real Claude."""
    sock = f"periscope-open-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PERISCOPE_TMUX_SOCKET", sock)
    monkeypatch.setenv("PERISCOPE_CLAUDE_EXEC", "cat")   # sits on stdin; window stays alive
    yield sock
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

@pytest.fixture
def tmp_git_repo(tmp_path):
    """Real git repo with one commit. Returns a realpath'd Path (macOS
    /var → /private/var, so callers compare against realpath)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "init"],
                   cwd=repo, env=env, check=True)
    return Path(os.path.realpath(repo))
```

- [ ] **Step 4: Verify the seams are inert in prod and active under the fixture**

Run: `uv run pytest -q` (existing suite must stay green — the env vars are unset, so `_tmux_argv` returns `["tmux", ...]` and `claude_exec()` returns the constant).
Expected: PASS (baseline unchanged).

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux.py periscope/config.py periscope/worktree_spawn.py tests/conftest.py
git commit -m "test(open): tmux -L socket seam + CLAUDE_EXEC override + git/tmux fixtures"
```

---

## Task 1: `_layout_two_window` stamps both windows

**Files:**
- Modify: `periscope/worktree_spawn.py:208-281`
- Test: `tests/test_worktree_spawn.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worktree_spawn.py` (uses the real-tmux pattern already in the file — match its existing session-name/teardown fixtures):

```python
def test_layout_two_window_stamps_both_windows(tmp_git_repo, tmux_test_server):
    from periscope.worktree_spawn import _layout_two_window
    from periscope.tmux import tmux
    session = "open-test-both-stamp"
    claude_pid, shell_pid = _layout_two_window(session, str(tmp_git_repo))
    assert claude_pid and shell_pid and claude_pid != shell_pid
    # Both windows carry an @periscope_id.
    for win in ("claude", "shell"):
        out = tmux("display-message", "-t", f"{session}:{win}",
                   "-p", "#{@periscope_id}").strip()
        assert out, f"{win} window not stamped"
```

> If `tmp_git_repo` / `tmux_test_server` fixtures don't exist, reuse whatever real-tmux setup the existing tests in this file use (grep the file for `new-session` / `kill-session`); the assertion is the point.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worktree_spawn.py::test_layout_two_window_stamps_both_windows -v`
Expected: FAIL — `_layout_two_window` returns a single `str`, so the tuple unpack raises `ValueError: not enough values to unpack`.

- [ ] **Step 3: Modify `_layout_two_window`**

Change the return type annotation to `tuple[str, str]`. After the shell window is created (the `new-window ... -n "shell"` block), resolve and stamp it. Replace the final block (from `# Park focus...` to `return pid`) with:

```python
    # Stamp the shell window too — server-side rail placement needs the
    # complete pane list synchronously (it would otherwise only learn the
    # shell pid on the next /api/state poll's resolve_pids).
    shell_idx = tmux("display-message", "-t", f"{tmux_session}:shell",
                     "-p", "#{window_index}").strip()
    shell_pid = stamp_new_window(f"{tmux_session}:{shell_idx}") if shell_idx.isdigit() else ""

    # Park focus on window 1 (claude).
    _tmux_mutate("select-window", "-t", f"{tmux_session}:claude")

    idx_out = tmux("display-message", "-t", f"{tmux_session}:claude",
                   "-p", "#{window_index}").strip()
    if not idx_out.isdigit():
        raise HTTPException(500, "could not resolve claude window index")
    target = f"{tmux_session}:{idx_out}"
    note_focus(target)
    note_action(target)
    claude_pid = stamp_new_window(target)
    return claude_pid, shell_pid
```

Update the docstring's "Returns the claude window's stamped @periscope_id" line to "Returns `(claude_pid, shell_pid)` — both windows stamped."

> Interim caller fix (both are deleted in Task 10, but must stay correct until then): `projects_create` (routes/projects.py:281) **ignores** the return (`_layout_two_window(tmux_session, pinned_dir)  # ignored`) — no change needed, a bare tuple-returning call is fine. `projects_pr_review` (routes/projects.py:613) does `claude_pid = _layout_two_window(...)` — change it to `claude_pid, _ = _layout_two_window(...)`, or its `set_window_fields(claude_pid, ...)` at :637 silently stamps a tuple-keyed window. Edit only :613.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worktree_spawn.py::test_layout_two_window_stamps_both_windows -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/worktree_spawn.py periscope/routes/projects.py tests/test_worktree_spawn.py
git commit -m "feat(open): _layout_two_window stamps both windows, returns both pids"
```

---

## Task 2: Extract `fetch_pr_into_worktree` from the route

**Files:**
- Modify: `periscope/projects.py` (add functions), `periscope/routes/projects.py:504-637` (call the new function)
- Test: `tests/test_open_ops.py` (new; PR-fetch cases migrate here from `tests/routes/test_projects.py`)

**Why:** The PR-fetch + worktree-add + rollback orchestration is ~90 inline lines in `projects_pr_review`. `open_target`'s PR variant needs it as a callable that also returns `is_fork` (for the post-convergence `linked_pr` stamp).

- [ ] **Step 1: Write the failing test**

Create `tests/test_open_ops.py`:

```python
import pytest
from periscope import projects

def test_fetch_pr_into_worktree_returns_metadata(tmp_git_repo, monkeypatch):
    # Mock `gh pr view` + `git fetch`; real `git worktree add`.
    monkeypatch.setattr(projects, "_resolve_pr_metadata",
        lambda repo, pr: {"headRefName": "pr-7", "isCrossRepository": False,
                          "baseRefName": "main", "state": "OPEN"})
    monkeypatch.setattr(projects, "_fetch_pr_branch", lambda *a, **k: None)
    res = projects.fetch_pr_into_worktree(str(tmp_git_repo), 7)
    assert res.path and res.base_branch == "main" and res.is_fork is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_open_ops.py::test_fetch_pr_into_worktree_returns_metadata -v`
Expected: FAIL — `AttributeError: module 'periscope.projects' has no attribute 'fetch_pr_into_worktree'`.

- [ ] **Step 3: Move the logic into `projects.py`**

Move `_resolve_pr_metadata`, `_fetch_pr_branch`, `_discard_pr_worktree` (currently `routes/projects.py:440-501`) into `periscope/projects.py` unchanged. Add a `PRWorktree` result and `fetch_pr_into_worktree` that lifts the body of `projects_pr_review` (projects.py route lines 504-637) **up to but not including** the `_layout_two_window` call and the `set_window_fields` stamp (those stay in the caller / move to `open_target`):

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PRWorktree:
    path: str
    base_branch: str
    is_fork: bool
    local_branch: str

def fetch_pr_into_worktree(repo: str, pr: int) -> PRWorktree:
    """Fetch PR #pr into a fresh worktree under `repo`. Preserves the
    route's rollback semantics: on any failure after the worktree exists,
    `_discard_pr_worktree` force-removes it and deletes the orphan branch.
    Raises ValueError on bad input, HTTPException on gh/git failures
    (mapped by the caller). Returns the worktree metadata.
    """
    # ... verbatim from routes/projects.py:504-637, minus the
    # _layout_two_window + set_window_fields tail, returning PRWorktree(...).
```

> Keep the `repo_lock` usage, the 409 collision branches, and every `_discard_pr_worktree` rollback call exactly as they are in the route today. This is a move, not a rewrite.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_open_ops.py::test_fetch_pr_into_worktree_returns_metadata -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/projects.py periscope/routes/projects.py tests/test_open_ops.py
git commit -m "refactor(open): extract fetch_pr_into_worktree from pr-review route"
```

---

## Task 3: `open_ops.py` scaffolding — descriptors, `OpenResult`, `ensure_project`

**Files:**
- Create: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_open_ops.py`:

```python
from periscope import open_ops, projects

def test_ensure_project_registers_when_absent(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    assert proj["tmux_session"] and proj["repo"] == repo
    assert repo in projects.all_projects()

def test_ensure_project_idempotent_no_409(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    first = open_ops.ensure_project(repo, repo)
    again = open_ops.ensure_project(repo, repo)   # must NOT raise
    assert again["tmux_session"] == first["tmux_session"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_open_ops.py -k ensure_project -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.open_ops'`.

- [ ] **Step 3: Create `periscope/open_ops.py`**

```python
"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""
import os
from dataclasses import dataclass

from periscope import projects
from periscope.gitutil import resolve_repo, resolve_repo_and_branch
from periscope.tmux import _run


@dataclass(frozen=True)
class PathTarget:
    path: str

@dataclass(frozen=True)
class BranchTarget:
    repo: str
    branch: str

@dataclass(frozen=True)
class PRTarget:
    repo: str
    pr: int

Descriptor = PathTarget | BranchTarget | PRTarget


@dataclass(frozen=True)
class OpenResult:
    tmux_session: str
    repo: str
    claude_pid: str
    ui: dict


def _git_toplevel(path: str) -> str:
    """Resolve a directory to its git toplevel. Raises ValueError if not
    inside a git repo (the boundary check for the path variant)."""
    code, out = _run(["git", "-C", path, "rev-parse", "--show-toplevel"])
    if code != 0 or not out.strip():
        raise ValueError(f"not inside a git repo: {path}")
    return os.path.realpath(out.strip())


def ensure_project(toplevel: str, repo: str) -> projects.Project:
    """Return the project row pinned at `toplevel`, registering it if absent.
    Idempotent — never raises on an existing project (unlike the legacy
    create/adopt routes' 409)."""
    existing = projects.all_projects().get(os.path.realpath(toplevel))
    if existing:
        return existing
    _, branch = resolve_repo_and_branch(toplevel)
    name = os.path.basename(toplevel)
    return projects.create_project(
        toplevel, name=name, tmux_session=name, repo=repo,
        base_branch=branch or None,
    )
```

> `projects.all_projects()` keys are `_canonical_key` (realpath); `ensure_project` matches on `os.path.realpath`. `create_project` defaults `tmux_session` to the dir basename; `ensure_session` (Task 4) resolves collisions on that name.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_open_ops.py -k ensure_project -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): open_ops scaffolding — descriptors, OpenResult, ensure_project"
```

---

## Task 4: `ensure_session` — idempotent create-or-focus

**Files:**
- Modify: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

**Three outcomes (spec):** recorded `tmux_session` name live AND ours → focus; dead → spawn under that name; live but foreign → spawn under a deduped name and update the project row. Liveness is `tmux has-session` on the **name**, never a cwd scan.

- [ ] **Step 1: Write the failing tests**

```python
from periscope.tmux import _tmux_mutate

def test_ensure_session_spawns_when_dead(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)                       # already realpath'd by the fixture
    proj = open_ops.ensure_project(repo, repo)
    session, claude_pid = open_ops.ensure_session(proj, repo)
    assert session == proj["tmux_session"] and claude_pid
    assert _tmux_mutate("has-session", "-t", session)[0] is True

def test_ensure_session_focuses_when_live_and_ours(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    s1, pid1 = open_ops.ensure_session(proj, repo)
    s2, pid2 = open_ops.ensure_session(proj, repo)   # must NOT spawn a 2nd session
    assert s1 == s2 and pid1 == pid2

def test_ensure_session_dedupes_foreign_name(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    # Occupy the recorded name with an unrelated session in a different cwd
    # (socket-aware so it lands on the test server, not the default one).
    _tmux_mutate("new-session", "-d", "-s", proj["tmux_session"], "-c", "/tmp")
    session, claude_pid = open_ops.ensure_session(proj, repo)
    assert session != proj["tmux_session"]      # deduped
    assert projects.get_project(repo)["tmux_session"] == session  # row updated
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_open_ops.py -k ensure_session -v`
Expected: FAIL — `AttributeError: ... 'ensure_session'`.

- [ ] **Step 3: Implement `ensure_session`**

```python
from periscope.panes import list_windows
from periscope.tmux import _tmux_mutate
from periscope.worktree_spawn import _layout_two_window


def _session_live(name: str) -> bool:
    return _tmux_mutate("has-session", "-t", name)[0]   # socket-aware; False when missing


def _session_owns_dir(name: str, pinned_dir: str) -> bool:
    """True if any window of session `name` sits at `pinned_dir` (realpath)."""
    return any(w["session"] == name
               and os.path.realpath(w.get("cwd") or "") == pinned_dir
               for w in list_windows())


def _claude_pid_for_session(name: str) -> str:
    """The @periscope_id (pid_raw) of the session's claude window — matched by
    window NAME ('claude'; set in _layout_two_window), since list_windows()
    carries no is_claude flag. Falls back to the first window. '' if none."""
    wins = [w for w in list_windows() if w["session"] == name]
    if not wins:
        return ""
    claude = next((w for w in wins if w["name"] == "claude"), wins[0])
    return claude.get("pid_raw") or ""


def _dedupe_name(base: str) -> str:
    n, candidate = 2, f"{base}-2"
    while _session_live(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def ensure_session(project: projects.Project, pinned_dir: str) -> tuple[str, str]:
    """Idempotent create-or-focus. `pinned_dir` is the project's key (the
    caller already has it — Project is a TypedDict with no self-key, so we
    take it explicitly rather than reverse-lookup). Returns (session, claude_pid)."""
    name = project["tmux_session"]
    if _session_live(name):
        if _session_owns_dir(name, pinned_dir):
            return name, _claude_pid_for_session(name)
        name = _dedupe_name(name)                      # live but foreign
        projects.update_project(pinned_dir, tmux_session=name)
    claude_pid, _ = _layout_two_window(name, pinned_dir)
    return name, claude_pid
```

> `list_windows()` keys verified against `panes.py:283-294`: `session, index, name, active, cwd, pid_raw, pane_id, activity`. The `@periscope_id` is **`pid_raw`** (not `pid`); there is **no `is_claude`** — claude is identified by `name == "claude"`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_open_ops.py -k ensure_session -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): ensure_session — name-based idempotent create-or-focus + foreign dedupe"
```

---

## Task 5: `worktree_for_branch`

**Files:**
- Modify: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

**Why:** The `{repo, branch}` variant must match an **enumerated** worktree (authoritative `git worktree list` via `_cached_worktrees`), not a recomputed slug path (`tc/foo` ↔ `tc-foo` collide; `_resolve_layout` writes settings on read).

- [ ] **Step 1: Write the failing test**

```python
def test_worktree_for_branch_matches_enumerated(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    # main checkout is git's first worktree; its branch is the default.
    from periscope.gitutil import detect_default_branch
    default = detect_default_branch(repo)
    assert open_ops.worktree_for_branch(repo, default) == os.path.realpath(repo)
    assert open_ops.worktree_for_branch(repo, "no-such-branch") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_open_ops.py -k worktree_for_branch -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

```python
from periscope import worktrees

def worktree_for_branch(repo: str, branch: str) -> str | None:
    """Path of an existing worktree checked out on `branch`, or None.
    Authoritative source is `git worktree list` (via the 60s cache), not a
    recomputed path."""
    for path, wt_branch in worktrees._cached_worktrees(repo):
        if wt_branch == branch:
            return os.path.realpath(path)
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_open_ops.py -k worktree_for_branch -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): worktree_for_branch via authoritative git worktree list"
```

---

## Task 6: `place_in_rail` — server-side rail pref write

**Files:**
- Modify: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

**Keys (verified against railTree.js):** group key = `project["repo"]` (`groupKeyForWindow` returns `row.repo`); worktree key = the tmux **session name** (`worktrees_by_repo[repo]` holds session names); pane list = the session's pids. The `"review"` sentinel is auto-added by `mergeLiveAndPrefs` for repo-backed projects (railTree.js:115) — omit it here, don't duplicate the rule.

- [ ] **Step 1: Write the failing test**

```python
from periscope import store

def test_place_in_rail_writes_keys(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    ui = open_ops.place_in_rail(proj["tmux_session"], proj, ["%1", "%2"])
    assert proj["repo"] in ui["repo_order"]
    assert proj["tmux_session"] in ui["worktrees_by_repo"][proj["repo"]]
    assert ui["panes_by_worktree"][proj["tmux_session"]] == ["%1", "%2"]
    assert store.get_ui() == ui          # returns exactly what was persisted
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_open_ops.py -k place_in_rail -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

```python
from periscope import store

def place_in_rail(tmux_session: str, project: projects.Project,
                  pane_pids: list[str]) -> dict:
    """Append the session to the rail prefs (idempotent) and return the
    full ui blob for the client to write into prefsSignal."""
    ui = store.get_ui()
    repo = project["repo"]
    order = list(ui.get("repo_order", []))
    if repo not in order:
        order.append(repo)
    wts = {k: list(v) for k, v in ui.get("worktrees_by_repo", {}).items()}
    wt_list = wts.setdefault(repo, [])
    if tmux_session not in wt_list:
        wt_list.append(tmux_session)
    panes = {k: list(v) for k, v in ui.get("panes_by_worktree", {}).items()}
    if tmux_session not in panes:
        panes[tmux_session] = list(pane_pids)
    patch = {"repo_order": order, "worktrees_by_repo": wts,
             "panes_by_worktree": panes}
    store.update_ui(patch)
    return store.get_ui()
```

> Mirrors `addWorktreeToRail` (prefs.js) but server-side and without the `"review"` sentinel. `pane_pids` come from `_layout_two_window`'s `(claude_pid, shell_pid)`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_open_ops.py -k place_in_rail -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): place_in_rail writes rail pref server-side, returns ui blob"
```

---

## Task 7: `build_catalog`

**Files:**
- Modify: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

**Reuse:** the repo-discovery + `git branch` logic from `projects_discoverable` (projects.py route 397-438) and `worktrees._cached_worktrees` for the worktree list (incl. the main checkout, git's first entry).

- [ ] **Step 1: Write the failing test**

```python
def test_build_catalog_lists_repo_and_main_worktree(tmp_git_repo, clean_state, monkeypatch):
    repo = str(tmp_git_repo)
    monkeypatch.setattr(open_ops, "_discover_repos", lambda: {repo})
    cat = open_ops.build_catalog()
    assert any(r["repo"] == repo for r in cat["repos"])
    assert any(w["path"] == os.path.realpath(repo) and w["is_main"]
               for w in cat["worktrees"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_open_ops.py -k build_catalog -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Factor the repo-discovery half of `projects_discoverable` into `open_ops._discover_repos() -> set[str]` (known-project repos + `~/dev` one-level git dirs — copy from projects.py route 406-419), then:

```python
from pathlib import Path
from periscope.gitutil import detect_default_branch

def _discover_repos() -> set[str]:
    repos: set[str] = set()
    for p in projects.all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"]))
    dev = Path.home() / "dev"
    if dev.is_dir():
        for child in dev.iterdir():
            if child.is_dir() and not child.name.startswith(".") \
               and child.name != "worktrees" and (child / ".git").exists():
                repos.add(str(child.resolve()))
    return repos

def build_catalog() -> dict:
    repos_out, worktrees_out = [], []
    for repo in sorted(_discover_repos()):
        code, out = _run(["git", "-C", repo, "branch", "--format=%(refname:short)"])
        branches = (out.split("\n")[:100] if (code == 0 and out) else [])
        repos_out.append({"repo": repo, "label": os.path.basename(repo),
                          "default_branch": detect_default_branch(repo),
                          "branches": branches})
        wts = worktrees._cached_worktrees(repo)
        for i, (path, branch) in enumerate(wts):
            worktrees_out.append({"path": os.path.realpath(path), "repo": repo,
                                  "branch": branch, "is_main": i == 0})
    return {"repos": repos_out, "worktrees": worktrees_out}
```

> Confirm `_cached_worktrees` returns the main checkout as index 0 (it wraps `git worktree list`, which lists the main worktree first). If ordering isn't guaranteed, derive `is_main` by comparing `realpath(path) == realpath(repo)` instead.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_open_ops.py -k build_catalog -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): build_catalog — repos + worktrees for the omnibox"
```

---

## Task 8: `open_target` dispatch + PR linkage

**Files:**
- Modify: `periscope/open_ops.py`
- Test: `tests/test_open_ops.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_open_target_path_spawns_dormant_then_focuses(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    r1 = open_ops.open_target(open_ops.PathTarget(path=repo))
    assert r1.repo == repo and r1.claude_pid and r1.tmux_session
    assert r1.tmux_session in r1.ui["worktrees_by_repo"][repo]
    # idempotent: second open focuses, no new session
    r2 = open_ops.open_target(open_ops.PathTarget(path=repo))
    assert r2.tmux_session == r1.tmux_session

def test_open_target_non_git_path_raises(tmp_path, clean_state):
    with pytest.raises(ValueError):
        open_ops.open_target(open_ops.PathTarget(path=str(tmp_path)))

def test_open_target_pr_stamps_linked_pr(tmp_git_repo, clean_state, tmux_test_server, monkeypatch):
    repo = str(tmp_git_repo)
    monkeypatch.setattr(projects, "fetch_pr_into_worktree",
        lambda r, pr: projects.PRWorktree(path=repo, base_branch="main",
                                          is_fork=False, local_branch="pr-9"))
    res = open_ops.open_target(open_ops.PRTarget(repo=repo, pr=9))
    assert store.get_window(res.claude_pid).get("linked_pr") == 9
```

> `store.get_window(pid)` (store.py:329) is the reader; `store.get_window_fields` does NOT exist. The write side `store.set_window_fields(..., linked_pr=, is_fork=)` (store.py:335) is correct.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_open_ops.py -k open_target -v`
Expected: FAIL — `AttributeError: ... 'open_target'`.

- [ ] **Step 3: Implement**

```python
def open_target(descriptor: Descriptor) -> OpenResult:
    if isinstance(descriptor, PathTarget):
        toplevel = _git_toplevel(descriptor.path)        # ValueError if non-git
        repo = resolve_repo(toplevel)                     # --git-common-dir → parent
        project = ensure_project(toplevel, repo)
        session, claude_pid = ensure_session(project, toplevel)
        # Rebuild the full pane list from the now-live session. list_windows()
        # is a live shell-out (panes.py:254), so freshly-stamped windows are
        # visible synchronously; pid_raw is the @periscope_id, "" for unmanaged.
        pane_pids = [w["pid_raw"] for w in list_windows()
                     if w["session"] == session and w["pid_raw"]]
        ui = place_in_rail(session, projects.get_project(toplevel),
                           pane_pids or [claude_pid])
        return OpenResult(tmux_session=session, repo=repo, claude_pid=claude_pid, ui=ui)

    if isinstance(descriptor, BranchTarget):
        wt = worktree_for_branch(descriptor.repo, descriptor.branch)
        if wt is None:
            from periscope.worktree_spawn import spawn_worktree
            wt = spawn_worktree(descriptor.repo, descriptor.branch)["path"]
        return open_target(PathTarget(path=wt))

    if isinstance(descriptor, PRTarget):
        prwt = projects.fetch_pr_into_worktree(descriptor.repo, descriptor.pr)
        result = open_target(PathTarget(path=prwt.path))
        store.set_window_fields(result.claude_pid, linked_pr=descriptor.pr,
                                is_fork=prwt.is_fork)
        return result

    raise ValueError(f"unknown descriptor: {descriptor!r}")
```

> The path case rebuilds the pane list from the now-live session (both windows are stamped after `_layout_two_window`, and a focus-existing session already has its pids), so `place_in_rail` gets the complete list. `spawn_worktree`'s exact signature/return is in `worktree_spawn.py` — confirm the branch argument name and that it returns `{"path": ...}` (Task 1 read confirms the dict shape).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_open_ops.py -k open_target -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/open_ops.py tests/test_open_ops.py
git commit -m "feat(open): open_target dispatch — path/branch/pr converge; PR linkage stamp"
```

---

## Task 9: `routes/open.py` + register + route tests

**Files:**
- Create: `periscope/routes/open.py`
- Modify: `periscope/app.py` (router include list)
- Test: `tests/routes/test_open.py`

- [ ] **Step 1: Write the failing test**

Create `tests/routes/test_open.py`:

```python
def test_post_open_path_returns_contract(client, tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    r = client.post("/api/open", json={"path": repo})
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == repo and body["claude_pid"]
    assert repo in body["ui"]["worktrees_by_repo"]

def test_post_open_non_git_400(client, tmp_path, clean_state):
    r = client.post("/api/open", json={"path": str(tmp_path)})
    assert r.status_code == 400

def test_post_open_bad_descriptor_400(client, clean_state):
    assert client.post("/api/open", json={}).status_code == 400
    assert client.post("/api/open", json={"branch": "x"}).status_code == 400  # repo missing

def test_get_catalog(client, clean_state):
    r = client.get("/api/open/catalog")
    assert r.status_code == 200 and "repos" in r.json() and "worktrees" in r.json()
```

> `client` fixture: match the existing TestClient fixture used across `tests/routes/`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/routes/test_open.py -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement the route + register it**

Create `periscope/routes/open.py`:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import open_ops

router = APIRouter()


class OpenBody(BaseModel):
    path: str | None = None
    repo: str | None = None
    branch: str | None = None
    pr: int | None = None


def _to_descriptor(b: OpenBody) -> open_ops.Descriptor:
    if b.path and not (b.repo or b.branch or b.pr):
        return open_ops.PathTarget(path=b.path)
    if b.repo and b.branch and b.pr is None and not b.path:
        return open_ops.BranchTarget(repo=b.repo, branch=b.branch)
    if b.repo and b.pr is not None and not (b.path or b.branch):
        return open_ops.PRTarget(repo=b.repo, pr=b.pr)
    raise HTTPException(400, "exactly one of {path | repo+branch | repo+pr} required")


@router.post("/api/open")
def open_endpoint(body: OpenBody):
    descriptor = _to_descriptor(body)
    try:
        result = open_ops.open_target(descriptor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tmux_session": result.tmux_session, "repo": result.repo,
            "claude_pid": result.claude_pid, "ui": result.ui}


@router.get("/api/open/catalog")
def open_catalog():
    return open_ops.build_catalog()
```

In `periscope/app.py`, import the route module **aliased** so it doesn't shadow the builtin `open()` — match the existing `lgtm as lgtm_route` / `projects as projects_routes` pattern (app.py:28-31): `from periscope.routes import open as open_route`, then add `open_route` to the `include_router` loop tuple (app.py:120-122).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/routes/test_open.py -v`
Expected: PASS (4 tests). Then `uv run pytest -q` to confirm nothing else broke.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/open.py periscope/app.py tests/routes/test_open.py
git commit -m "feat(open): POST /api/open + GET /api/open/catalog route"
```

---

## Task 10: Retire legacy routes + migrate/trim tests

**Files:**
- Modify: `periscope/routes/sessions.py` (delete `POST /api/session/new`), `periscope/routes/projects.py` (delete create + pr-review bodies)
- Modify: `tests/routes/test_sessions.py`, `tests/routes/test_projects.py`

- [ ] **Step 1: Delete the route handlers**

Remove `@router.post("/api/session/new")` + `session_new` + `NewSessionBody` from `routes/sessions.py` (lines ~38-55). Remove `@router.post("/api/projects")` (`projects_create`) and `@router.post("/api/projects/pr-review")` (`projects_pr_review`) from `routes/projects.py`. Keep `adopt`, `patch`, `archive`, `promote`, `discoverable`, and the now-relocated PR helpers' imports clean (the helpers live in `projects.py` after Task 2).

- [ ] **Step 2: Trim/migrate the tests**

In `tests/routes/test_sessions.py`: delete the `session/new` cases (keep rename/delete window/session cases). In `tests/routes/test_projects.py`: delete the `pr-review` route cases (their rollback coverage now lives in `tests/test_open_ops.py` from Task 2) and the `POST /api/projects` create cases; keep adopt/patch/archive/promote/discoverable.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — green suite with the retired-endpoint tests gone and `open` tests covering the replacement.

- [ ] **Step 4: Verify no stragglers reference the deleted endpoints**

Run: `grep -rn "api/session/new\|api/projects\"\|projects/pr-review" periscope/ tests/`
Expected: no server-side or test references (frontend references die in Task 14).

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/sessions.py periscope/routes/projects.py tests/routes/test_sessions.py tests/routes/test_projects.py
git commit -m "refactor(open): retire /api/session/new, /api/projects create, /api/projects/pr-review"
```

---

## Task 11: Frontend classifier `open/classify.js`

**Files:**
- Create: `static/src/open/classify.js`, `static/src/open/__tests__/classify.test.js`

- [ ] **Step 1: Write the failing test**

Create `static/src/open/__tests__/classify.test.js`:

```js
import { describe, it, expect } from "vitest";
import { parsePrRef, classify } from "../classify.js";

describe("parsePrRef", () => {
  it("parses a github PR url", () => {
    expect(parsePrRef("https://github.com/fdy/repo/pull/1234"))
      .toEqual({ repo: "fdy/repo", pr: 1234 });
  });
  it("parses a bare #N", () => {
    expect(parsePrRef("#42")).toEqual({ repo: null, pr: 42 });
  });
  it("returns null for non-pr", () => {
    expect(parsePrRef("splash")).toBeNull();
  });
});

describe("classify", () => {
  const catalog = {
    repos: [{ repo: "/d/splash", label: "splash", default_branch: "main", branches: ["main", "feat-x"] }],
    worktrees: [{ path: "/d/splash", repo: "/d/splash", branch: "main", is_main: true }],
  };
  it("surfaces an open-dir card for a matching worktree", () => {
    const cards = classify("splash", catalog);
    expect(cards.some(c => c.kind === "open" && c.descriptor.path === "/d/splash")).toBe(true);
  });
  it("offers a new-worktree card for a matching repo", () => {
    expect(classify("splash", catalog).some(c => c.kind === "worktree")).toBe(true);
  });
  it("offers a PR card for a #N query", () => {
    expect(classify("#7", catalog).some(c => c.kind === "pr")).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run static/src/open`
Expected: FAIL — cannot resolve `../classify.js`.

- [ ] **Step 3: Implement `classify.js`**

```js
// Pure: query string (+ catalog) → ranked candidate cards. No DOM, no signals.
// Each card: { kind: 'open'|'worktree'|'pr', label, descriptor }.

const PR_URL = /github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/;
const PR_HASH = /^#?(\d+)$/;

export function parsePrRef(q) {
  const u = q.match(PR_URL);
  if (u) return { repo: u[1], pr: Number(u[2]) };
  const h = q.trim().match(PR_HASH);
  if (h) return { repo: null, pr: Number(h[1]) };
  return null;
}

function match(hay, needle) {
  return hay.toLowerCase().includes(needle.toLowerCase());
}

export function classify(query, catalog) {
  const q = (query || "").trim();
  const cards = [];
  if (!q) return cards;

  // open-dir cards (worktrees + repo roots)
  for (const w of catalog.worktrees || []) {
    if (match(w.path, q) || match(w.branch || "", q)) {
      const repoLabel = (catalog.repos.find(r => r.repo === w.repo) || {}).label || w.repo;
      cards.push({ kind: "open", label: `${repoLabel} · ${w.branch || "detached"}`,
                   descriptor: { path: w.path } });
    }
  }
  // new-worktree cards (per matching repo)
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "worktree", label: `${r.label} · new worktree…`,
                   repo: r.repo, branches: r.branches, descriptor: null });
    }
  }
  // PR card
  const pr = parsePrRef(q);
  if (pr) {
    cards.push({ kind: "pr", label: pr.repo ? `review PR #${pr.pr} in ${pr.repo}`
                                             : `review PR #${pr.pr}…`,
                 needsRepo: !pr.repo, descriptor: pr.repo ? null : null, pr });
  }
  return cards;
}
```

> `worktree` and `pr` cards carry drill-in metadata (`branches`, `needsRepo`); the component fills the final `descriptor` once the user picks a branch / repo. `open` cards are terminal.

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run static/src/open`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/src/open/classify.js static/src/open/__tests__/classify.test.js
git commit -m "feat(open): pure input→descriptor classifier + unit tests"
```

---

## Task 12: `prefs.setUI`

**Files:**
- Modify: `static/src/prefs.js`

- [ ] **Step 1: Add `setUI` (no test — trivial assignment; covered by the omnibox browser-verify)**

After `patchUI`, add:

```js
// Write a server-authoritative ui blob into the cache WITHOUT re-POSTing —
// used by the open flow, where the server already persisted the pref.
export function setUI(uiBlob) {
  prefsSignal.value = { ...P(), ui: uiBlob };
}
```

- [ ] **Step 2: Commit**

```bash
git add static/src/prefs.js
git commit -m "feat(open): prefs.setUI — write server-returned ui blob without re-POST"
```

> `addWorktreeToRail` deletion happens in Task 14 (after its last callers are removed).

---

## Task 13: `OpenOmnibox.jsx`

**Files:**
- Create: `static/src/overlays/OpenOmnibox.jsx`

- [ ] **Step 1: Implement the component** (browser-verified, no unit test)

```jsx
// Unified open omnibox. Opened by the +new button via window.__periscopeOpenOmnibox.
// Loads GET /api/open/catalog once on open; classify() ranks cards per keystroke.
// open cards POST immediately; worktree/pr cards drill in (same field) to fill
// the descriptor, then POST. On success: write response.ui into prefsSignal via
// prefs.setUI (the deferRailAdd replacement) and close.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";
import { setUI } from "../prefs.js";
import { classify } from "../open/classify.js";

const open = signal(false);
function openOmnibox() { open.value = true; track("overlay.open", { which: "open" }); }
function close() { open.value = false; }

export function OpenOmnibox() {
  useEscape(close, open.value);
  const [catalog, setCatalog] = useState({ repos: [], worktrees: [] });
  const [query, setQuery] = useState("");
  const [drill, setDrill] = useState(null);   // { card } when drilling into worktree/pr
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    window.__periscopeOpenOmnibox = openOmnibox;
    return () => { if (window.__periscopeOpenOmnibox === openOmnibox) delete window.__periscopeOpenOmnibox; };
  }, []);

  useEffect(() => {
    if (!open.value) return;
    setQuery(""); setDrill(null); setError("");
    (async () => {
      const data = await apiCall("open catalog", "/api/open/catalog");
      if (data) setCatalog(data);
    })();
  }, [open.value]);

  if (!open.value) return null;

  async function post(descriptor) {
    setBusy(true); setError("");
    const data = await apiCall("open", "/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(descriptor),
    });
    setBusy(false);
    if (!data) { setError("open failed"); return; }
    setUI(data.ui);     // synchronous rail placement — no deferRailAdd
    close();
  }

  function pick(card) {
    if (card.kind === "open") return post(card.descriptor);
    setDrill({ card });   // worktree → branch entry; pr → repo picker
  }

  const cards = drill ? [] : classify(query, catalog);

  return (
    <div id="open-omnibox" class="open-omnibox-overlay"
         onClick={(e) => { if (e.target.id === "open-omnibox") close(); }}>
      <div class="open-omnibox-card">
        {!drill && (
          <>
            <input class="open-omnibox-input" autofocus placeholder="repo, path, #PR…"
                   value={query} onInput={(e) => setQuery(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && cards[0]) pick(cards[0]); }} />
            <div class="open-omnibox-list">
              {cards.map((c, i) => (
                <button key={i} class={`open-omnibox-row kind-${c.kind}`} onClick={() => pick(c)}>
                  {c.label}
                </button>
              ))}
            </div>
          </>
        )}
        {drill && drill.card.kind === "worktree" && (
          <BranchDrill card={drill.card} onPick={(branch) =>
            post({ repo: drill.card.repo, branch })} onBack={() => setDrill(null)} />
        )}
        {drill && drill.card.kind === "pr" && (
          <RepoDrill repos={catalog.repos} onPick={(repo) =>
            post({ repo, pr: drill.card.pr })} onBack={() => setDrill(null)} />
        )}
        {error && <div class="open-omnibox-error">{error}</div>}
        {busy && <div class="open-omnibox-busy">opening…</div>}
      </div>
    </div>
  );
}

function BranchDrill({ card, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (card.branches || []).filter((b) => b.includes(val));
  return (
    <>
      <input class="open-omnibox-input" autofocus placeholder={`branch in ${card.repo}…`}
             value={val} onInput={(e) => setVal(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") onPick(val || matches[0]); }} />
      <div class="open-omnibox-list">
        {matches.map((b) => (
          <button key={b} class="open-omnibox-row" onClick={() => onPick(b)}>{b}</button>
        ))}
        {val && !matches.includes(val) && (
          <button class="open-omnibox-row kind-new" onClick={() => onPick(val)}>
            new branch “{val}”
          </button>
        )}
      </div>
      <button class="open-omnibox-back" onClick={onBack}>← back</button>
    </>
  );
}

function RepoDrill({ repos, onPick, onBack }) {
  const [val, setVal] = useState("");
  const matches = (repos || []).filter((r) => r.label.includes(val) || r.repo.includes(val));
  return (
    <>
      <input class="open-omnibox-input" autofocus placeholder="repo for this PR…"
             value={val} onInput={(e) => setVal(e.target.value)} />
      <div class="open-omnibox-list">
        {matches.map((r) => (
          <button key={r.repo} class="open-omnibox-row" onClick={() => onPick(r.repo)}>{r.label}</button>
        ))}
      </div>
      <button class="open-omnibox-back" onClick={onBack}>← back</button>
    </>
  );
}
```

> CSS classes (`open-omnibox-*`) are new — add minimal styles to the stylesheet the other overlays use (grep an existing overlay class like `launcher-modal-card` to find the file). Styling is browser-verified, not specced here.

- [ ] **Step 2: Commit** (after the build in Task 14 — this file isn't wired yet)

---

## Task 14: Wire `+ new`, delete the three modals, build, browser-verify

**Files:**
- Modify: `static/src/chrome/Header.jsx`, `static/src/overlays/Overlays.jsx`, `static/src/prefs.js`
- Delete: `static/src/overlays/{NewProjectModal,ReviewPrModal,OpenPickerModal}.jsx`

- [ ] **Step 1: Collapse the `+ new` dropdown to one button**

In `Header.jsx`, replace the `<Dropdown id="new-dd-toggle" ...>` block (the three `<button>` items: `new-session`, `new-project-btn`, `review-pr-btn`) with a single button:

```jsx
<button type="button" class="filter-btn is-action" id="new-open-btn"
        title="open a repo, worktree, or PR"
        onClick={() => window.__periscopeOpenOmnibox?.()}>
  + new
</button>
```

- [ ] **Step 2: Rewire `Overlays.jsx`**

Remove the `+session` imperative handler (the `getElementById("new-session")` block + `onNewSession`). Remove the `NewProjectModal` / `ReviewPrModal` / `OpenPickerModal` imports and their `<.../>` mounts. Add `import { OpenOmnibox } from "./OpenOmnibox.jsx";` and mount `<OpenOmnibox />`.

- [ ] **Step 3: Delete the modals + dead pref mutator**

```bash
git rm static/src/overlays/NewProjectModal.jsx static/src/overlays/ReviewPrModal.jsx static/src/overlays/OpenPickerModal.jsx
```

Confirm `addWorktreeToRail` has no remaining callers, then delete it from `prefs.js`:

Run: `grep -rn "addWorktreeToRail\|__periscopeOpenPicker\|__periscopeOpenLauncher" static/src`
Expected: only `Rail.jsx`'s `+ New tab` launcher bridge survives (that's the per-group launcher, unrelated — leave it). Delete `addWorktreeToRail` and its `deferRailAdd` references; if `Detail.jsx:553` calls `__periscopeOpenPicker`, repoint that empty-state button to `__periscopeOpenOmnibox`.

- [ ] **Step 4: Build the bundle**

Run: `npm run build`
Expected: `static/dist/app.js` rebuilt with no errors.

- [ ] **Step 5: Browser-verify (the UI oracle — per project norm)**

Start dev: `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`, open `http://localhost:8766/`. Verify:
- `+ new` opens the omnibox; typing a repo name shows open-dir + new-worktree cards.
- Opening a **dormant** project (e.g. a registered repo whose session you killed) spawns it and it appears in the rail **immediately** (no ~3.5s delay).
- Opening an already-live dir focuses it (no duplicate session).
- `#1234` → PR card → repo drill-in → opens.
- New-worktree drill-in lists branches + offers "new branch".

- [ ] **Step 6: Commit**

```bash
npm run build
git add static/
git commit -m "feat(open): unified omnibox replaces +new menu; retire 3 modals"
```

---

## Task 15: Full regression + merge

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest -q`
Expected: PASS (green; ~630 baseline minus retired-endpoint tests plus new open tests). Paste the last ~20 lines.

- [ ] **Step 2: Frontend unit**

Run: `npx vitest run static/src/open`
Expected: PASS.

- [ ] **Step 3: Channel smoke (touched nothing here, but cheap insurance)**

Run: `uv run tests/test_channel_smoke.py`
Expected: PASS.

- [ ] **Step 4: Merge + restart prod**

```bash
cd ~/dev/periscope
git merge feature/unified-open
bin/periscope restart
git worktree remove ../periscope-open
```

---

## Self-Review Notes

- **Spec coverage:** `/api/open` + descriptor variants (T3,T4,T5,T8,T9); idempotent focus + no-409 (T3,T4,T8); revive name-collision (T4); catalog (T7); server-side rail placement + ui-blob return (T6,T8,T9); client `setUI` refresh kills `deferRailAdd` (T12,T13); both-windows stamping (T1); PR linkage post-convergence (T2,T8); omnibox + classifier + drill-ins (T11,T13); retire legacy routes + 3 modals + `+session` (T10,T14); non-git→400 (T8,T9). OpenPickerModal retired with accepted capability loss (T14).
- **Test-harness safety (Task 0):** real-tmux tests run on an isolated `-L` socket (`PERISCOPE_TMUX_SOCKET`) with a stub exec (`PERISCOPE_CLAUDE_EXEC=cat`) so `pytest` never touches the default tmux server or spawns real Claude — both seams inert in prod (env unset).
- **Plan-reviewer fixes applied:** `list_windows()` keys corrected to `pid_raw` + name-match for claude (no `is_claude`); `ensure_session(project, pinned_dir)` takes the key explicitly (no fragile reverse-lookup); `store.get_window` not `get_window_fields`; `app.py` import aliased `open as open_route`; Task 1 edits `projects_pr_review:613` only; realpath-safe assertions via the `tmp_git_repo` fixture.
- **Verified-sound (no action):** `spawn_worktree(repo, branch) -> {"path",...}`, `_cached_worktrees` main-checkout-first, `list_windows()` live-not-cached, rail keys + `"review"` auto-add.
