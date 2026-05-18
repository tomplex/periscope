# Project Model + Cleanup (Phase 6) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Verb 5 (cleanup) from `2026-05-15-workflow-management-design.md`. A user clicks `🧹 cleanup` in the top bar; periscope walks every known worktree, evaluates four staleness signals per worktree, and presents a checklist for bulk archive. Selected rows are archived (project row → `archived_at`, tmux session killed, `git worktree remove --force`, optionally `git branch -D`).

**Architecture:** Two new server-side pieces plus one frontend modal.

1. **`periscope/cleanup.py`** — new module owning staleness computation. Walks repos via `worktrees._cached_worktrees`, evaluates four signals per worktree (PR merged/closed, branch merged into default, remote branch deleted, idle > 14 days), aggregates into Candidate rows. Maintains a per-process 5-minute TTL cache on `(repo, pr_number) → state` for the gh calls — Tom uses cleanup infrequently but actively, so caching matters within a session.

2. **`periscope/routes/cleanup.py`** — two endpoints. `GET /api/cleanup/candidates` returns the list. `POST /api/cleanup/archive` takes `{candidates: [{pinned_dir, delete_branch?}], delete_branches?}` and bulk-archives. Mirrors the body-carried `pinned_dir` convention used by phase 1-4.

3. **`static/cleanup-modal.js` + index.html slots + CSS** — frontend modal listing candidates with signal badges + checkboxes. Top-level "also delete local branches" checkbox flips `delete_branch=true` on all selected. Dirty worktrees rendered with a `⚠ dirty` warning and NOT auto-selected.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git / gh. Pytest at `tests/` — phase 6 adds coverage for the new endpoints + the cleanup module's signal logic.

**Spec:** `docs/superpowers/specs/2026-05-15-workflow-management-design.md` §Verb 5.

**Design calls (confirmed in conversation):**
- Cache PR state for 5 minutes per `(repo, pr_number)`. Cleanup modal re-opens within a session are instant; cold opens (or stale cache) cost up to ~7s for a repo with 14 worktrees.
- Untracked worktrees (on disk via `git worktree list` with no matching project row) appear in the candidate list. Common cleanup target.
- "Also delete local branches" is a top-level opt-in checkbox, off by default. Flips `delete_branch=true` on all selected rows.
- Dirty worktrees never auto-selected; user must explicitly check.
- Idle threshold is 14 days, hardcoded. Phase 7's settings make it configurable.

**What's explicitly NOT in phase 6:**
- **Auto-archive logic** (silent archive when conditions hold) — surfaces candidates only; user confirmation always required.
- **Configurable thresholds** — phase 7.
- **`change branch`** verb (per-project) — independently deferred.
- **`edit repo override`** verb — independently deferred.
- **`linked_pr` cleanup on archive** — `linked_pr` persists via the phase-4 immunity rule. Un-archiving restores the PR badge.
- **Per-project cleanup view** scoped to one project — phase 6 ships the global view only. Per-project scope is a phase-7 polish item.

---

## File Structure

**Created:**
- `periscope/cleanup.py` — `Candidate` TypedDict, signal helpers, `compute_candidates(repo_filter=None)`.
- `periscope/routes/cleanup.py` — `GET /api/cleanup/candidates`, `POST /api/cleanup/archive`.
- `static/cleanup-modal.js` — modal open/close/render/submit.
- `tests/routes/test_cleanup.py` — pytest for both endpoints.

**Modified:**
- `periscope/app.py` — wire the new `routes/cleanup.py` router.
- `static/index.html` — `🧹 cleanup` button + modal markup.
- `static/styles.css` — modal styles + signal-badge styles + dirty-row styling.
- `static/app.js` — wire `initCleanupModal()` at boot.

**Not modified (deliberately):**
- `periscope/projects.py` — `archive_project` already does exactly what we need.
- `periscope/git_pr.py` — its `_pr_cache` is `--state open` scoped, doesn't help here; cleanup needs `state,mergedAt` which is a different gh call. New cache in `cleanup.py`.

---

## Task 1: `periscope/cleanup.py` — signal computation + candidate aggregation

**Files:**
- Create: `periscope/cleanup.py`

- [ ] **Step 1: Write the module**

Create `periscope/cleanup.py`:

```python
"""Cleanup candidate computation.

Walks all repos that periscope knows about (from registered projects),
lists their worktrees via `git worktree list`, evaluates four staleness
signals per worktree, and returns Candidate rows. Used by Verb 5
(cleanup) in routes/cleanup.py.

Caching:
  - PR state: 5-minute TTL on (repo, pr_number) → state string.
    Justification: Tom opens cleanup infrequently but actively. A 5-min
    TTL makes the modal feel instant when re-opened during a session,
    and stale within ~5min is fine for "should I archive this?"
    decisions.
  - Branch state (merged-into-default + remote-exists): 60s TTL on
    (repo, branch). Shorter because branch state changes faster on
    active work.

The walk is read-only — no state mutations. The archive verb in
routes/cleanup.py is what actually mutates.
"""

import os
import threading
import time
from typing import TypedDict, Optional

from periscope.log import log
from periscope.projects import all_projects, MAIN_KEY
from periscope.store import get_window
from periscope.tmux import _run
from periscope import worktrees


# === Caches ================================================================

_PR_STATE_TTL = 300.0  # 5 minutes
_BRANCH_TTL = 60.0

_lock = threading.Lock()
_pr_state_cache: dict[tuple[str, int], tuple[float, Optional[str]]] = {}
_branch_merged_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_remote_branch_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_default_branch_cache: dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.time()


# === Signal helpers ========================================================

def _detect_default_branch(repo: str) -> str:
    """Cached `git symbolic-ref refs/remotes/origin/HEAD` fallback to
    main/master. 5-min TTL — repo defaults don't churn."""
    with _lock:
        cached = _default_branch_cache.get(repo)
        if cached and _now() - cached[0] < _PR_STATE_TTL:
            return cached[1]
    code, ref = _run(
        ["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"],
        timeout=3.0,
    )
    if code == 0 and ref:
        branch = ref.rsplit("/", 1)[-1]
    else:
        code, out = _run(
            ["git", "-C", repo, "branch", "--format=%(refname:short)"],
            timeout=3.0,
        )
        branches = out.split("\n") if code == 0 else []
        branch = "main" if "main" in branches else ("master" if "master" in branches else "main")
    with _lock:
        _default_branch_cache[repo] = (_now(), branch)
    return branch


def _pr_state(repo: str, pr_number: int) -> Optional[str]:
    """Return 'OPEN' / 'CLOSED' / 'MERGED' / None. Cached 5min."""
    key = (repo, pr_number)
    with _lock:
        cached = _pr_state_cache.get(key)
        if cached and _now() - cached[0] < _PR_STATE_TTL:
            return cached[1]
    code, out = _run(
        ["gh", "pr", "view", str(pr_number), "--json", "state"],
        cwd=repo,
        timeout=10.0,
    )
    state: Optional[str] = None
    if code == 0 and out:
        try:
            import json
            state = (json.loads(out).get("state") or None)
        except Exception as e:
            log.warning("cleanup: gh pr view %s parse error: %s", pr_number, e)
    with _lock:
        _pr_state_cache[key] = (_now(), state)
    return state


def _is_branch_merged(repo: str, branch: str, default: str) -> bool:
    """Cached `git branch --merged <default>` membership check. Misses
    squash-merges (which is why PR state is the primary signal — this
    is the fallback for projects with no linked_pr)."""
    key = (repo, branch)
    with _lock:
        cached = _branch_merged_cache.get(key)
        if cached and _now() - cached[0] < _BRANCH_TTL:
            return cached[1]
    code, out = _run(
        ["git", "-C", repo, "branch", "--merged", default, "--format=%(refname:short)"],
        timeout=3.0,
    )
    merged = code == 0 and branch in out.split("\n")
    with _lock:
        _branch_merged_cache[key] = (_now(), merged)
    return merged


def _remote_branch_exists(repo: str, branch: str) -> bool:
    """Cached `git ls-remote --heads origin <branch>` non-empty check."""
    key = (repo, branch)
    with _lock:
        cached = _remote_branch_cache.get(key)
        if cached and _now() - cached[0] < _BRANCH_TTL:
            return cached[1]
    code, out = _run(
        ["git", "-C", repo, "ls-remote", "--heads", "origin", branch],
        timeout=5.0,
    )
    exists = code == 0 and bool(out.strip())
    with _lock:
        _remote_branch_cache[key] = (_now(), exists)
    return exists


def _is_dirty(wt_path: str) -> bool:
    """`git status --porcelain` non-empty → dirty."""
    if not os.path.isdir(wt_path):
        return False
    code, out = _run(["git", "-C", wt_path, "status", "--porcelain"], timeout=3.0)
    return code == 0 and bool(out.strip())


def _last_commit_age_days(wt_path: str) -> int:
    """Days since the worktree HEAD's last commit. Returns 9999 on
    failure (treat as 'very idle')."""
    if not os.path.isdir(wt_path):
        return 9999
    code, out = _run(
        ["git", "-C", wt_path, "log", "-1", "--format=%ct"], timeout=3.0
    )
    if code != 0 or not out.strip().isdigit():
        return 9999
    try:
        commit_ts = int(out.strip())
        return max(0, int((_now() - commit_ts) / 86400))
    except (ValueError, OverflowError):
        return 9999


# === Candidate types =======================================================

class Signal(TypedDict):
    kind: str  # "pr_merged" | "pr_closed" | "branch_merged" | "remote_gone" | "idle"
    label: str  # human-readable, e.g. "PR #1234 merged"


class Candidate(TypedDict):
    pinned_dir: str  # the worktree's absolute realpath
    project_name: Optional[str]  # null if untracked
    tmux_session: Optional[str]  # null if untracked
    repo: str  # the repo's main-checkout path
    branch: str  # the worktree's current branch (or "(detached)")
    is_fork: bool  # from state.windows[pid].is_fork on the project's claude window
    signals: list[Signal]
    dirty: bool
    untracked: bool  # true if no project row exists for this pinned_dir
    idle_days: int  # days since last commit on the worktree's branch


# === Main entry point ======================================================

IDLE_THRESHOLD_DAYS = 14


def compute_candidates(repo_filter: Optional[str] = None) -> list[Candidate]:
    """Walk every repo periscope knows about and return the cleanup
    candidate list. A worktree appears as a candidate if ANY signal
    fires.

    `repo_filter` (optional realpath): scope to a single repo. Used by
    a future per-project cleanup view (not in phase 6).
    """
    projects = all_projects()
    # Build (repo → project_rows) so we can correlate worktrees with
    # their owning project rows. We also need to surface UNTRACKED
    # worktrees: ones on disk but with no project row.
    project_by_pinned: dict[str, dict] = {
        k: v for k, v in projects.items() if k != MAIN_KEY
    }
    project_by_repo: dict[str, list[tuple[str, dict]]] = {}
    repos: set[str] = set()
    for pinned, row in project_by_pinned.items():
        repo = row.get("repo")
        if not repo:
            continue
        project_by_repo.setdefault(repo, []).append((pinned, row))
        repos.add(repo)

    if repo_filter:
        repos = {repo_filter} if repo_filter in repos else set()

    # Hoist windows + alive-session lookups out of the per-candidate loop.
    # `all_windows()` holds _STATE_LOCK + deep-copies; calling it N times
    # is wasteful when the data is identical. Same for `tmux has-session` —
    # one `list-sessions` call beats N per-candidate invocations.
    from periscope.store import all_windows
    windows_snapshot = all_windows()
    code, sessions_out = _run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=3.0
    )
    alive_sessions: set[str] = (
        set(sessions_out.strip().split("\n")) if code == 0 and sessions_out.strip()
        else set()
    )

    candidates: list[Candidate] = []
    for repo in sorted(repos):
        default = _detect_default_branch(repo)
        # `_cached_worktrees` returns [(realpath, branch_or_none), ...]
        for wt_path, branch in worktrees._cached_worktrees(repo):
            # Skip the main checkout itself — it's not a worktree we'd
            # ever clean up.
            if os.path.realpath(wt_path) == os.path.realpath(repo):
                continue

            # Match against a project row (if any).
            matched = next(
                (
                    (p, row) for p, row in project_by_pinned.items()
                    if os.path.realpath(p) == wt_path
                ),
                (None, None),
            )
            pinned_dir, project_row = matched
            project_name = project_row.get("name") if project_row else None
            tmux_session = project_row.get("tmux_session") if project_row else None

            branch = branch or "(detached)"
            signals: list[Signal] = []

            # Signal 1: PR merged/closed (primary).
            linked_pr = None
            is_fork = False
            if project_row and tmux_session:
                # Walk the hoisted windows snapshot for the first window
                # tied to this project's tmux session that has linked_pr
                # set. Tmux session names are unique per project (adopt
                # would 409 on collision) so this correctly scopes.
                for pid, ann in windows_snapshot.items():
                    last = ann.get("last_seen") or {}
                    if last.get("session") == tmux_session and ann.get("linked_pr"):
                        linked_pr = ann["linked_pr"]
                        is_fork = bool(ann.get("is_fork"))
                        break

            if linked_pr:
                state = _pr_state(repo, linked_pr)
                if state == "MERGED":
                    signals.append({"kind": "pr_merged", "label": f"PR #{linked_pr} merged"})
                elif state == "CLOSED":
                    signals.append({"kind": "pr_closed", "label": f"PR #{linked_pr} closed"})

            # Signal 2: branch merged into default (fallback when no PR
            # state, or in addition).
            if branch != "(detached)" and branch != default:
                if _is_branch_merged(repo, branch, default):
                    if not any(s["kind"].startswith("pr_") for s in signals):
                        signals.append({"kind": "branch_merged", "label": f"branch merged into {default}"})

            # Signal 3: remote branch deleted. Skipped for fork PRs
            # where the local branch was never on origin.
            if branch != "(detached)" and branch != default and not is_fork:
                if not _remote_branch_exists(repo, branch):
                    signals.append({"kind": "remote_gone", "label": f"origin/{branch} deleted"})

            # Signal 4: idle. Days since last commit on the worktree's
            # HEAD; only flagged when no active tmux session AND
            # > 14 days. Session-alive check uses the hoisted
            # `alive_sessions` set — no per-candidate tmux call.
            idle_days = _last_commit_age_days(wt_path)
            session_alive = bool(tmux_session and tmux_session in alive_sessions)
            if not session_alive and idle_days > IDLE_THRESHOLD_DAYS:
                signals.append({"kind": "idle", "label": f"idle {idle_days}d"})

            if not signals:
                # Worktree is healthy from cleanup's perspective. Skip.
                continue

            dirty = _is_dirty(wt_path)

            candidates.append({
                "pinned_dir": pinned_dir or wt_path,
                "project_name": project_name,
                "tmux_session": tmux_session,
                "repo": repo,
                "branch": branch,
                "is_fork": is_fork,
                "signals": signals,
                "dirty": dirty,
                "untracked": project_row is None,
                "idle_days": idle_days,
            })

    return candidates
```

- [ ] **Step 2: Verify the module imports + a sanity walk**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run python3 -c "
from periscope.cleanup import compute_candidates, _detect_default_branch
print('default branch for periscope:', _detect_default_branch('/Users/tom/dev/periscope'))
# Walk live state. With real projects + worktrees, this exercises every
# signal path against your actual state.json.
cands = compute_candidates()
print(f'candidates: {len(cands)}')
for c in cands[:5]:
    name = c['project_name'] or '(untracked)'
    signals = ' / '.join(s['label'] for s in c['signals'])
    dirty = ' (dirty)' if c['dirty'] else ''
    print(f'  {name} @ {c[\"branch\"]}: {signals}{dirty}')
"
```

Expected: returns a list of candidate rows or empty. Each row has one or more signal labels. Run takes a few seconds the first time (gh + git calls); near-instant on second run (cache hit).

- [ ] **Step 3: Run existing pytest suite**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green. No regressions.

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 commit -am "cleanup: add compute_candidates + 4-signal staleness evaluation with 5-min PR-state cache"
```

---

## Task 2: `periscope/routes/cleanup.py` — endpoints + tests

**Files:**
- Create: `periscope/routes/cleanup.py`
- Modify: `periscope/app.py` (wire the new router)
- Create: `tests/routes/test_cleanup.py`

- [ ] **Step 1: Write the routes module**

Create `periscope/routes/cleanup.py`:

```python
"""Cleanup verb (Verb 5): GET candidates + POST bulk archive."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.cleanup import compute_candidates
from periscope.log import log
from periscope.projects import archive_project, all_projects, MAIN_KEY
from periscope.tmux import _run, _tmux_mutate
from periscope import worktrees


router = APIRouter()


@router.get("/api/cleanup/candidates")
def cleanup_candidates(repo: str | None = None):
    """List cleanup candidates. Optional `repo` query param scopes to one
    repo's worktrees (for a future per-project view; phase 6 ships the
    global view as the primary UI)."""
    repo_filter = os.path.realpath(repo) if repo else None
    cands = compute_candidates(repo_filter)
    return {"candidates": cands}


class ArchiveItem(BaseModel):
    pinned_dir: str
    delete_branch: bool = False


class ArchiveBody(BaseModel):
    candidates: list[ArchiveItem]


@router.post("/api/cleanup/archive")
def cleanup_archive(body: ArchiveBody):
    """Bulk-archive selected candidates. For each:
      1. Archive the project row (if one exists) — sets archived_at.
      2. Kill the tmux session (if one exists).
      3. `git worktree remove --force` the path.
      4. Optionally `git branch -D` the worktree's branch (opt-in per row).

    Failures on individual rows don't stop the batch — collect them and
    return alongside successes.
    """
    archived: list[str] = []
    failed: list[dict] = []

    projects = all_projects()

    for item in body.candidates:
        pinned_dir = item.pinned_dir
        try:
            row = projects.get(pinned_dir)
            if pinned_dir == MAIN_KEY:
                raise ValueError("cannot archive __main__")

            # 1. Archive project row (if one exists).
            tmux_session = row.get("tmux_session") if row else None
            repo = row.get("repo") if row else None
            if row:
                archive_project(pinned_dir)

            # 2. Kill tmux session.
            if tmux_session:
                _tmux_mutate("kill-session", "-t", tmux_session)

            # 3. Determine the repo for worktree removal. Untracked
            # worktrees have no project.repo; derive via git from the
            # worktree path itself.
            if not repo:
                code, common = _run(
                    ["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"],
                    timeout=3.0,
                )
                if code == 0 and common:
                    common_abs = (
                        common if os.path.isabs(common)
                        else os.path.join(pinned_dir, common)
                    )
                    repo = os.path.realpath(os.path.dirname(common_abs))
                else:
                    repo = pinned_dir  # fallback; git will likely error

            # Capture the branch BEFORE removing the worktree (HEAD is
            # gone once the worktree's gitdir is gone).
            branch: str | None = None
            if item.delete_branch:
                code, b = _run(
                    ["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                    timeout=3.0,
                )
                if code == 0 and b and b != "HEAD":
                    branch = b

            # 4. Remove the worktree.
            code, out = _run(
                ["git", "-C", repo, "worktree", "remove", "--force", pinned_dir],
                timeout=10.0,
            )
            if code != 0:
                # Worktree removal can fail for various reasons (git
                # state corrupted, permission denied). Surface but don't
                # halt the batch.
                failed.append({"pinned_dir": pinned_dir, "error": f"worktree remove: {out}"})
                # Skip branch-delete if worktree removal failed —
                # the branch might still be in use.
                continue

            worktrees.invalidate(repo)

            # 5. Optional branch delete. -D is force-delete; the user
            # opted in by checking the box, so don't be defensive.
            if item.delete_branch and branch:
                code, out = _run(
                    ["git", "-C", repo, "branch", "-D", branch], timeout=3.0
                )
                if code != 0:
                    # Non-fatal — surface the warning but the worktree
                    # is already gone.
                    log.warning(
                        "cleanup: branch -D %s on %s failed: %s",
                        branch, repo, out,
                    )

            archived.append(pinned_dir)
        except Exception as e:
            failed.append({"pinned_dir": pinned_dir, "error": str(e)})

    return {"ok": True, "archived": archived, "failed": failed}
```

- [ ] **Step 2: Wire the router in `periscope/app.py`**

Find the existing router-include block in `periscope/app.py`. Add the new import + include alongside the others:

```python
from periscope.routes import cleanup as cleanup_routes
# ...
app.include_router(cleanup_routes.router)
```

- [ ] **Step 3: Write the tests**

Create `tests/routes/test_cleanup.py`:

```python
"""Tests for /api/cleanup/*."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def test_candidates_returns_list(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.compute_candidates",
        return_value=[
            {
                "pinned_dir": "/Users/x/dev/foo/wt-1",
                "project_name": "foo-feature",
                "tmux_session": "foo-feature",
                "repo": "/Users/x/dev/foo",
                "branch": "feature/x",
                "is_fork": False,
                "signals": [{"kind": "pr_merged", "label": "PR #42 merged"}],
                "dirty": False,
                "untracked": False,
                "idle_days": 5,
            }
        ],
    )
    r = client.get("/api/cleanup/candidates")
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1
    assert r.json()["candidates"][0]["project_name"] == "foo-feature"


def test_candidates_repo_filter(client, mocker):
    spy = mocker.patch(
        "periscope.routes.cleanup.compute_candidates", return_value=[]
    )
    r = client.get("/api/cleanup/candidates?repo=/Users/x/dev/foo")
    assert r.status_code == 200
    spy.assert_called_once()
    args = spy.call_args.args
    # repo passed through (post-realpath).
    assert args and "foo" in args[0]


def test_archive_happy_path(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt1": {
                "name": "foo", "tmux_session": "foo", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            }
        },
    )
    archive_spy = mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mutate_spy = mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch(
        "periscope.routes.cleanup._run",
        return_value=(0, ""),  # worktree remove succeeds
    )

    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt1", "delete_branch": False}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["archived"] == ["/wt1"]
    assert body["failed"] == []
    archive_spy.assert_called_once_with("/wt1")
    # tmux kill-session called.
    mutate_spy.assert_any_call("kill-session", "-t", "foo")


def test_archive_with_branch_delete(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt1": {
                "name": "foo", "tmux_session": "foo", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            }
        },
    )
    mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    # _run sequence: rev-parse HEAD → "feature/x", worktree remove → ok,
    # branch -D → ok.
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, "feature/x"),  # rev-parse HEAD (only when delete_branch=True)
            (0, ""),           # worktree remove
            (0, ""),           # branch -D
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt1", "delete_branch": True}],
    })
    assert r.status_code == 200
    assert r.json()["archived"] == ["/wt1"]


def test_archive_untracked_resolves_repo_from_worktree(client, mocker):
    """Untracked worktree (no project row) — repo derived via
    --git-common-dir."""
    mocker.patch("periscope.routes.cleanup.all_projects", return_value={})
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, "/some/repo/.git"),  # git-common-dir
            (0, ""),                  # worktree remove
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt-orphan", "delete_branch": False}],
    })
    assert r.status_code == 200
    assert r.json()["archived"] == ["/wt-orphan"]


def test_archive_continues_on_individual_failure(client, mocker):
    """One bad row doesn't halt the batch."""
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt-good": {
                "name": "good", "tmux_session": "good", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            },
            "/wt-bad": {
                "name": "bad", "tmux_session": "bad", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            },
        },
    )
    mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    # First (good): worktree remove returns (0, ""). Second (bad): returns
    # (1, "worktree path not a worktree").
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, ""),                          # wt-good remove
            (1, "not a worktree"),            # wt-bad remove
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [
            {"pinned_dir": "/wt-good", "delete_branch": False},
            {"pinned_dir": "/wt-bad", "delete_branch": False},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert "/wt-good" in body["archived"]
    assert any(f["pinned_dir"] == "/wt-bad" for f in body["failed"])


def test_archive_rejects_main(client, mocker):
    mocker.patch("periscope.routes.cleanup.all_projects", return_value={})
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "__main__", "delete_branch": False}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] == []
    assert body["failed"][0]["pinned_dir"] == "__main__"
    assert "__main__" in body["failed"][0]["error"]


def test_archive_empty_candidates(client, mocker):
    r = client.post("/api/cleanup/archive", json={"candidates": []})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "archived": [], "failed": []}
```

- [ ] **Step 4: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run pytest tests/routes/test_cleanup.py -x -v 2>&1 | tail -15
```

Expected: all 8 tests pass.

Then run the full suite to confirm no regressions:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 commit -am "routes/cleanup: GET /api/cleanup/candidates + POST /api/cleanup/archive"
```

Don't forget `git add` for the new files if `-am` misses them.

---

## Task 3: `static/index.html` slots

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Filter-bar button**

Add to `<nav class="filters">`, after the existing `#review-pr-btn`:

```html
<button id="cleanup-btn" class="filter-btn is-action" title="archive worktrees with merged PRs, deleted branches, or idle activity">🧹 cleanup</button>
```

- [ ] **Step 2: Modal markup**

Add after the existing `#review-pr-modal` div:

```html
<div id="cleanup-modal" class="hidden cleanup-modal-overlay">
  <div class="cleanup-modal-card">
    <header class="cleanup-modal-head">
      <h2>🧹 cleanup</h2>
      <button id="cleanup-modal-close" title="close">×</button>
    </header>
    <p class="cleanup-modal-sub">Worktrees with merged/closed PRs, deleted remote branches, or idle activity. Dirty worktrees are NOT auto-selected.</p>
    <div id="cleanup-modal-controls">
      <label class="cleanup-delete-branches">
        <input type="checkbox" id="cleanup-delete-branches"> also delete local branches
      </label>
    </div>
    <div id="cleanup-modal-list"></div>
    <div id="cleanup-modal-error" class="cleanup-modal-error" hidden></div>
    <div class="cleanup-modal-actions">
      <button type="button" id="cleanup-cancel">cancel</button>
      <button type="button" id="cleanup-submit" disabled>archive selected (0)</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Verify HTML parses**

```bash
python3 -c "from html.parser import HTMLParser; HTMLParser().feed(open('/Users/tom/dev/worktrees/periscope/tc-project-model-phase-6/static/index.html').read()); print('parse OK')"
```

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 commit -am "index.html: cleanup top-bar button + modal markup"
```

---

## Task 4: `static/cleanup-modal.js` + CSS + app.js wiring

**Files:**
- Create: `static/cleanup-modal.js`
- Modify: `static/app.js`
- Modify: `static/styles.css`

- [ ] **Step 1: Write the modal module**

Create `static/cleanup-modal.js`:

```javascript
// Cleanup modal. Loads candidates from /api/cleanup/candidates, renders
// a checklist with signal badges, submits selected to /api/cleanup/archive.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("cleanup-modal");
const closeBtn = document.getElementById("cleanup-modal-close");
const cancelBtn = document.getElementById("cleanup-cancel");
const submitBtn = document.getElementById("cleanup-submit");
const listEl = document.getElementById("cleanup-modal-list");
const errorEl = document.getElementById("cleanup-modal-error");
const deleteBranchesBox = document.getElementById("cleanup-delete-branches");

let candidates = [];
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderRow(c, i) {
  const name = c.project_name || `(untracked: ${escapeHtml(c.pinned_dir.split("/").pop())})`;
  const branch = escapeHtml(c.branch);
  const badges = c.signals
    .map((s) => `<span class="cleanup-badge cleanup-badge-${s.kind}">${escapeHtml(s.label)}</span>`)
    .join(" ");
  const dirtyLabel = c.dirty ? `<span class="cleanup-dirty">⚠ dirty</span>` : "";
  // Dirty worktrees: rendered NOT checked. Healthy candidates: checked.
  const checked = c.dirty ? "" : "checked";
  const rowClass = c.dirty ? "cleanup-row cleanup-row-dirty" : "cleanup-row";
  return `
    <label class="${rowClass}" data-i="${i}">
      <input type="checkbox" class="cleanup-row-check" ${checked}>
      <div class="cleanup-row-body">
        <div class="cleanup-row-title">${escapeHtml(name)}</div>
        <div class="cleanup-row-meta">${branch} ${dirtyLabel}</div>
        <div class="cleanup-row-signals">${badges}</div>
      </div>
    </label>
  `;
}

function updateSubmitCount() {
  const checked = listEl.querySelectorAll(".cleanup-row-check:checked").length;
  submitBtn.textContent = `archive selected (${checked})`;
  submitBtn.disabled = checked === 0;
}

async function refresh() {
  listEl.innerHTML = `<div class="cleanup-loading">Walking worktrees…</div>`;
  try {
    const res = await fetch("/api/cleanup/candidates");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    candidates = data.candidates;
    if (candidates.length === 0) {
      listEl.innerHTML = `<div class="cleanup-empty">No cleanup candidates. 🎉</div>`;
    } else {
      listEl.innerHTML = candidates.map(renderRow).join("");
    }
    updateSubmitCount();
  } catch (e) {
    showError(`failed to load candidates: ${e.message}`);
    listEl.innerHTML = "";
  }
}

export async function openCleanupModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  deleteBranchesBox.checked = false;
  modal.classList.remove("hidden");
  document.body.classList.add("cleanup-modal-open");
  pushEscape(closeCleanupModal);
  await refresh();
}

export function closeCleanupModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("cleanup-modal-open");
  popEscape(closeCleanupModal);
}

async function handleSubmit() {
  clearError();
  const selected = [];
  const deleteBranches = deleteBranchesBox.checked;
  listEl.querySelectorAll(".cleanup-row").forEach((row) => {
    const check = row.querySelector(".cleanup-row-check");
    if (check && check.checked) {
      const i = parseInt(row.dataset.i, 10);
      const c = candidates[i];
      if (c) {
        selected.push({ pinned_dir: c.pinned_dir, delete_branch: deleteBranches });
      }
    }
  });
  if (selected.length === 0) return;
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/cleanup/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates: selected }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    const result = await res.json();
    if (result.failed && result.failed.length > 0) {
      // Some failed; show error, refresh to show what's left.
      showError(
        `Archived ${result.archived.length}, ${result.failed.length} failed: ` +
        result.failed.map((f) => `${f.pinned_dir.split("/").pop()}: ${f.error}`).join("; ")
      );
      await refresh();
      return;
    }
    closeCleanupModal();
  } catch (e) {
    showError(`request failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initCleanupModal() {
  const openBtn = document.getElementById("cleanup-btn");
  if (openBtn) openBtn.addEventListener("click", openCleanupModal);
  closeBtn.addEventListener("click", closeCleanupModal);
  cancelBtn.addEventListener("click", closeCleanupModal);
  submitBtn.addEventListener("click", handleSubmit);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeCleanupModal();
  });
  listEl.addEventListener("change", (e) => {
    if (e.target.classList.contains("cleanup-row-check")) {
      updateSubmitCount();
    }
  });
}
```

- [ ] **Step 2: Wire `initCleanupModal` in `static/app.js`**

Alongside the existing `initReviewPRModal()` call:

```javascript
import { initCleanupModal } from './cleanup-modal.js';
// ...
initCleanupModal();
```

- [ ] **Step 3: Add CSS**

Add to `static/styles.css`. Reuse the existing modal-overlay patterns via comma-separation:

```css
/* alias existing modal styles */
.new-project-modal-overlay,
.review-pr-modal-overlay,
.cleanup-modal-overlay {
  /* existing block */
}
.new-project-modal-card,
.review-pr-modal-card,
.cleanup-modal-card {
  /* existing block */
}
/* ...etc for head/sub/actions/error... */

/* cleanup-specific */
.cleanup-modal-card {
  width: min(720px, 90vw);  /* wider than the create/review modals */
}
.cleanup-loading,
.cleanup-empty {
  padding: 1em;
  opacity: 0.7;
  font-size: 0.9em;
}
#cleanup-modal-controls {
  margin: 0.5em 0;
}
.cleanup-delete-branches {
  font-size: 0.85em;
  opacity: 0.85;
}
.cleanup-row {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.5em;
  padding: 0.5em;
  border-bottom: 1px solid color-mix(in oklch, var(--fg-0) 12%, transparent);
  cursor: pointer;
}
.cleanup-row:hover { background: color-mix(in oklch, var(--fg-0) 4%, transparent); }
.cleanup-row-dirty {
  background: color-mix(in oklch, var(--warn, #d97706) 8%, transparent);
}
.cleanup-row-title { font-weight: 600; }
.cleanup-row-meta {
  font-size: 0.85em;
  opacity: 0.7;
  font-family: var(--font-mono, ui-monospace, monospace);
}
.cleanup-row-signals { margin-top: 0.25em; }
.cleanup-badge {
  display: inline-block;
  font-size: 0.75em;
  padding: 0.1em 0.4em;
  border-radius: 0.4em;
  margin-right: 0.25em;
  background: color-mix(in oklch, var(--fg-0) 10%, transparent);
}
.cleanup-badge-pr_merged,
.cleanup-badge-pr_closed { color: var(--accent, #4a90e2); }
.cleanup-badge-remote_gone { color: var(--warn, #d97706); }
.cleanup-badge-idle { opacity: 0.7; }
.cleanup-dirty {
  font-size: 0.8em;
  color: var(--warn, #d97706);
  margin-left: 0.5em;
}
```

- [ ] **Step 4: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run --with httpx python3 -c "
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)
r = client.get('/cleanup-modal.js')
print('JS status:', r.status_code, 'bytes:', len(r.content))
assert r.status_code == 200 and 'initCleanupModal' in r.text
r = client.get('/')
print('HTML has #cleanup-btn:', 'id=\"cleanup-btn\"' in r.text)
print('HTML has #cleanup-modal:', 'id=\"cleanup-modal\"' in r.text)
print('PASS')
"
```

Plus `uv run pytest tests/ -x -q` — should be green.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 commit -am "cleanup-modal: candidate list with signal badges + bulk archive submit"
```

Don't forget `git add static/cleanup-modal.js` if `-am` misses it.

---

## Task 5: end-to-end smoke

Final integration check before merge.

- [ ] **Step 1: pytest full suite**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 2: Real-state TestClient smoke**

This walks your actual state.json + git worktrees to confirm the endpoint is healthy on real data:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-6 && uv run --with httpx python3 << 'EOF'
import time
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)

# 1. First call — cold cache. May take several seconds for gh + git ops.
t0 = time.time()
r = client.get("/api/cleanup/candidates")
t1 = time.time()
assert r.status_code == 200, r.text
cands = r.json()["candidates"]
print(f"[1] cold call: {len(cands)} candidates in {t1-t0:.2f}s")
for c in cands[:5]:
    name = c['project_name'] or '(untracked)'
    signals = ' / '.join(s['label'] for s in c['signals'])
    dirty = ' (dirty)' if c['dirty'] else ''
    print(f'  {name} @ {c["branch"]}: {signals}{dirty}')

# 2. Second call — warm cache. Should be fast.
t2 = time.time()
r = client.get("/api/cleanup/candidates")
t3 = time.time()
print(f"[2] warm call: {len(r.json()['candidates'])} candidates in {t3-t2:.2f}s")

# 3. Empty-candidates archive.
r = client.post("/api/cleanup/archive", json={"candidates": []})
assert r.status_code == 200
assert r.json() == {"ok": True, "archived": [], "failed": []}
print("[3] empty archive OK")

print("PASS")
EOF
```

Expected: cold call returns candidates (possibly 0 if your repos are healthy). Warm call is noticeably faster (~10× or more). Empty archive succeeds.

- [ ] **Step 3: (Optional) Real archive smoke**

If you want to exercise the full archive path against a real worktree, pick one that's clearly cleanup-worthy (merged PR, idle > 14 days). Confirm via the candidates list, then archive it via curl:

```bash
curl -s -X POST http://127.0.0.1:8765/api/cleanup/archive \
  -H "Content-Type: application/json" \
  -d '{"candidates":[{"pinned_dir":"/Users/tom/dev/worktrees/some-repo/tc-merged-feature","delete_branch":false}]}' \
  | python3 -m json.tool
```

Expected: 200, the worktree is removed, the project archived. The dashboard refreshes within 3s and the project is gone from the grid (visible only via `/api/projects` if archived projects aren't filtered).

---

## What's deliberately NOT in phase 6

- **Auto-archive** (silent archive when all signals fire for >N days). Phase 6 always requires user confirmation.
- **Configurable idle threshold + branch-delete default + repos_dir** — phase 7 settings.
- **Per-project cleanup view** (just this project's worktrees) — phase 7 polish.
- **`change branch` + `edit repo override`** verbs — independently deferred.
- **History-aware cleanup signals** (e.g. "no claude conversation activity in N days") — out of scope for the metadata phases 1-4 wrote.

The phase-6 endpoints (`GET /api/cleanup/candidates`, `POST /api/cleanup/archive`) are stable. Phase 7's settings layer will read the same data; no schema migrations expected.
