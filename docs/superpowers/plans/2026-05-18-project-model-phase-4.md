# Project Model + PR Review (Phase 4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Verb 3 (PR review) from `2026-05-15-workflow-management-design.md`. A user enters a PR number for one of their repos; periscope fetches the PR's commits, creates a worktree at branch `pr-<N>`, spins up a tmux session with claude + shell, and auto-links the PR on the claude window so the existing card-meta row picks up the `#N` badge.

**Architecture:** Three pieces.

1. **Synchronous pid stamping** — `periscope/pids.py` gains a public helper `stamp_new_window(target) -> str` that mints a periscope id, writes it to the tmux window's `@periscope_id` option, and returns the id. Used to write `linked_pr` immediately rather than waiting for the next poll. Retrofitted into `_layout_two_window` (returns the claude window's pid) so future verbs that need per-window metadata at creation time don't have to re-invent it.

2. **`POST /api/projects/pr-review` endpoint** in `periscope/routes/projects.py`. Body: `{repo, pr_number, name?}`. Calls `gh pr view <N> --json headRefName,isCrossRepository,headRepository,baseRefName,state` to fetch metadata, runs `git fetch origin pull/<N>/head:pr-<N>` (uniform refspec for same-repo and fork PRs — local branch is always `pr-<N>`), `git worktree add <wt_path> pr-<N>` (existing branch, no `-b`), then runs phase-2's `_layout_two_window` (now returning the claude pid), and finally writes `state.windows[pid].linked_pr = N` plus `is_fork = <bool>` so the card-meta `#PR` badge appears on the next poll.

3. **`Review PR` top-bar button + modal** modeled on phase 2's `new-project-modal.js`. Repo combobox + PR number input. Submit → endpoint.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git / gh. The pytest suite at `tests/` is authoritative — all existing tests must continue to pass, and phase 4 adds coverage for the new endpoint.

**Spec:** `docs/superpowers/specs/2026-05-15-workflow-management-design.md` §Verb 3.

**Design calls (confirmed in conversation):**
- Local branch is always `pr-<N>`. Same-repo PRs do NOT preserve `headRefName`. User can rename via ⋯ later.
- Refspec `pull/<N>/head:pr-<N>` works for both same-repo AND fork PRs uniformly. The refs/pull namespace exists on every PR.
- PR state (open / closed / merged) is fetched but doesn't gate creation. Reviewing merged PRs is valid.
- Synchronous `@periscope_id` stamping is added for the PR-review claude window AND retrofitted into `_layout_two_window` for symmetry.
- Repo selection is required (no `owner/repo#N` global form).
- No banner for closed/merged PRs in phase 4 (phase 7 polish).

**What's explicitly NOT in phase 4:**
- Global PR lookup without a known repo
- Per-project "review another PR" ⋯ menu item (only top-bar gesture)
- Banner for merged/closed PRs
- Cleanup logic that consumes `is_fork` (phase 6 reads it; phase 4 only writes it)

---

## File Structure

**Modified:**
- `periscope/pids.py` — adds `stamp_new_window(target) -> str` public helper.
- `periscope/routes/projects.py` — `_layout_two_window` retrofitted to return the claude window's pid. Adds `PRReviewBody` model and `POST /api/projects/pr-review` endpoint.
- `static/index.html` — adds `Review PR` button to the filter bar and modal markup.
- `static/styles.css` — modal styles (reuses new-project-modal patterns).
- `static/app.js` — wires `initReviewPRModal` at boot.
- `tests/routes/test_projects.py` — adds pytest coverage for the new endpoint (mocked gh + git).

**Created:**
- `static/review-pr-modal.js` — the `Review PR` modal: open/close/populate/submit, modeled on `new-project-modal.js`.

---

## Task 1: `stamp_new_window` helper + `_layout_two_window` retrofit + `is_fork` annotation

**Files:**
- Modify: `periscope/pids.py` (add public helper; extend `_IMMUNITY_FIELDS`)
- Modify: `periscope/store.py` (add `is_fork: bool` to `WindowAnnotation` TypedDict)
- Modify: `periscope/routes/projects.py` (`_layout_two_window`)
- Modify: `periscope/routes/projects.py` (`projects_create` — minor update to consume new return)

- [ ] **Step 0: Extend `WindowAnnotation` + GC immunity for `is_fork`**

In `periscope/store.py`, the `WindowAnnotation` TypedDict (around line 57) declares the per-window annotation fields. Add `is_fork: bool`:

```python
class WindowAnnotation(TypedDict, total=False):
    """Per-pid annotations persisted in state.json under windows[pid]."""
    linked_pr: int
    linked_linear: str
    completed_at: int
    acked_at: int
    alias: str
    is_fork: bool  # phase 4: set on PR-review projects' claude window
```

In `periscope/pids.py`, the `_IMMUNITY_FIELDS` tuple inside `resolve_pids` (phase 1 introduced it at around line 151) lists the fields that protect a window row from the 30-day GC. Add `"is_fork"`:

```python
        _IMMUNITY_FIELDS = (
            "notes", "tags",
            "linked_pr", "linked_linear",
            "acked_at", "completed_at",
            "alias", "is_fork",
        )
```

Without this, a phase-6 cleanup that clears `linked_pr` (to mark a review as "reviewed") on a fork-PR window would lose `is_fork`, breaking the spec §Verb 5 rule that fork-PR projects skip the "remote branch deleted" cleanup signal.

- [ ] **Step 1: Add `stamp_new_window` to `periscope/pids.py`**

After the existing `_stamp_pid` function (around line 28), add:

```python
def stamp_new_window(target: str) -> str:
    """Mint a fresh periscope id, stamp it onto the tmux window at `target`,
    and return it. Used by handlers that need to write `state.windows[pid]`
    fields synchronously after creating a window — without this, the pid
    wouldn't be assigned until the next /api/state poll runs resolve_pids.

    Re-stamping a window that already has an `@periscope_id` is harmless
    (this function unconditionally mints + stamps), but resolve_pids will
    accept either id on the next poll. Callers should only invoke this on
    freshly-created windows.
    """
    pid = _mint_pid()
    _stamp_pid(target, pid)
    return pid
```

- [ ] **Step 2: Retrofit `_layout_two_window` to return the claude window's pid**

In `periscope/routes/projects.py`, the `_layout_two_window` helper currently returns `None`. After the `select-window` call and before the `note_focus/note_action` block at the end of the function, stamp the claude window and return the pid.

Update the signature:
```python
def _layout_two_window(tmux_session: str, pinned_dir: str) -> str:
    """[existing docstring]

    Returns the claude window's stamped @periscope_id. Phase 4's PR-review
    endpoint uses this to write state.windows[pid].linked_pr synchronously;
    other callers can ignore the return.
    """
```

Inside the function, after locating the claude window's index via `display-message` (which the function already does for `note_focus`/`note_action`):

```python
    from periscope.pids import stamp_new_window
    # ...existing display-message + note_focus/note_action block...
    if not idx_out.isdigit():
        # If we can't resolve the claude window's index after creating it,
        # something is very wrong with tmux state. Fail loudly — silently
        # returning "" would let PR-review skip the linked_pr write and
        # create a project with no #PR badge, which the user couldn't
        # detect without inspecting state.json.
        raise HTTPException(500, "could not resolve claude window index")
    target = f"{tmux_session}:{idx_out}"
    note_focus(target)
    note_action(target)
    pid = stamp_new_window(target)
    return pid
```

(Move the import to the top of `routes/projects.py` if you prefer; inline `from periscope.pids import stamp_new_window` at the function top is also fine — match the file's existing pattern of inline imports for narrow uses.)

- [ ] **Step 3: Update `projects_create` to consume the new return**

In `projects_create` (around line 273-340), the call to `_layout_two_window` returns `None` today and the result is discarded. Update the call:

```python
    try:
        _layout_two_window(tmux_session, pinned_dir)
    except HTTPException:
        # tmux failed mid-layout — leave the worktree on disk so the user
        # can retry adoption or clean up manually. Don't rollback git.
        raise
```

becomes:

```python
    try:
        _layout_two_window(tmux_session, pinned_dir)  # returns pid; ignored here
    except HTTPException:
        # tmux failed mid-layout — leave the worktree on disk so the user
        # can retry adoption or clean up manually. Don't rollback git.
        raise
```

(Just a comment update — the call site doesn't change behavior. The PR-review endpoint in Task 2 captures the return.)

- [ ] **Step 4: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 && uv run python3 -c "
import tempfile, subprocess, os
from periscope.pids import stamp_new_window

# Create a tmux session + window we can stamp.
sess = 'pid-stamp-verify'
subprocess.run(['tmux', 'kill-session', '-t', sess], capture_output=True)
subprocess.run(['tmux', 'new-session', '-d', '-s', sess, '-c', '/tmp'], check=True)
try:
    target = f'{sess}:0'
    pid = stamp_new_window(target)
    print(f'stamped pid: {pid}')
    assert len(pid) == 8 and all(c in '0123456789abcdef' for c in pid)
    # Confirm tmux now reports @periscope_id = pid on the window.
    out = subprocess.run(
        ['tmux', 'show-options', '-w', '-t', target, '@periscope_id'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f'tmux reports: {out}')
    assert pid in out
finally:
    subprocess.run(['tmux', 'kill-session', '-t', sess], capture_output=True)
print('PASS')
"
```

Expected: PASS. The minted pid is 8 hex chars and tmux confirms `@periscope_id` is set.

Also run the existing pytest suite to confirm no regressions from the `_layout_two_window` signature change:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 commit -am "pids: add stamp_new_window helper; _layout_two_window returns claude pid for sync metadata writes"
```

---

## Task 2: `POST /api/projects/pr-review` endpoint

**Files:**
- Modify: `periscope/routes/projects.py` (new endpoint + body model)

- [ ] **Step 1: Add imports**

These are all NEW to `periscope/routes/projects.py` (verified against current main: none are present). Add:

```python
import json  # alongside the other stdlib imports

from periscope.repo_locks import repo_lock
from periscope.store import set_window_fields
from periscope.worktrees import invalidate as worktrees_invalidate
```

The existing `_run`, `_tmux_mutate`, `tmux`, `spawn_worktree`, `create_project`, `all_projects`, `HTTPException`, and `BaseModel` imports stay.

- [ ] **Step 2: Add the body model**

Near the existing `CreateBody`, `AdoptBody`, etc. models:

```python
class PRReviewBody(BaseModel):
    repo: str
    pr_number: int
    name: str | None = None  # defaults to pr-<N> if absent
```

- [ ] **Step 3: Add the endpoint**

Append after the existing `projects_promote` handler (around line 365). The implementation runs gh + git inline rather than going through `spawn_worktree` because the spawn primitive is hardcoded to `-b <new-branch>` semantics — PR review needs `worktree add <path> pr-<N>` (existing local branch).

```python
@router.post("/api/projects/pr-review")
def projects_pr_review(body: PRReviewBody):
    """Spawn a project for reviewing PR #<N> on `repo`. Fetches via
    `pull/<N>/head:pr-<N>` (uniform for same-repo + fork PRs), creates a
    worktree at branch `pr-<N>`, applies the standard claude+shell layout,
    and writes `linked_pr` on the claude window so the card-meta `#PR`
    badge appears on the next poll.

    Errors:
      400 — repo not git, pr_number invalid, gh call failed, fetch failed,
            project name collides
      404 — PR not found
      409 — worktree path already exists OR tmux session name collides OR
            project already exists at pinned_dir
      500 — git/tmux mutation failed for any other reason
    """
    repo = os.path.realpath(body.repo)
    if not os.path.isdir(repo):
        raise HTTPException(400, f"repo does not exist: {body.repo}")
    code, toplevel = _run(["git", "-C", repo, "rev-parse", "--show-toplevel"])
    if code != 0 or not toplevel:
        raise HTTPException(400, f"not a git repo: {body.repo}")
    repo = os.path.realpath(toplevel)

    pr = body.pr_number
    if pr <= 0:
        raise HTTPException(400, f"pr_number must be positive: {pr}")

    # Resolve target name + tmux session up front so we can do a cheap
    # collision pre-check BEFORE the 15-second gh call. Wastes nothing on
    # a known-collision retry.
    local_branch = f"pr-{pr}"
    name_preview = (body.name or local_branch).strip()
    has_session_code, _ = _run(["tmux", "has-session", "-t", name_preview])
    if has_session_code == 0:
        raise HTTPException(
            409, f"tmux session {name_preview!r} already exists; pick a different name",
        )

    # gh pr view → metadata.
    code, out = _run(
        [
            "gh", "pr", "view", str(pr),
            "--json", "headRefName,isCrossRepository,headRepository,baseRefName,state",
        ],
        cwd=repo,
        timeout=15.0,
    )
    if code != 0:
        # gh's stderr is in `out` since _run merges them; map "not found"
        # variants to 404, anything else to 400.
        if "no pull requests found" in out.lower() or "could not resolve" in out.lower():
            raise HTTPException(404, f"PR #{pr} not found in {body.repo}: {out}")
        raise HTTPException(400, f"gh pr view failed: {out}")
    try:
        meta = json.loads(out)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"gh pr view returned invalid JSON: {e}")

    is_fork = bool(meta.get("isCrossRepository"))
    pr_state = (meta.get("state") or "").upper()  # OPEN / CLOSED / MERGED
    # NOTE: base_branch here is the PR's target branch (e.g. `main`), per
    # spec §Verb 3 step 5. This means future worktree-tabs spawned from
    # THIS project (Verb 2) will fork off `main`, not off `pr-<N>`. That
    # IS the spec's intent — sub-feature work off a PR-review should
    # rebase against the PR's target, not the PR itself. Don't "fix" this.
    base_branch = meta.get("baseRefName") or None
    name = name_preview
    tmux_session = name

    # Fetch the PR's head commits into a local branch `pr-<N>`. The
    # `pull/<N>/head:<localname>` refspec works for both same-repo and
    # fork PRs — the refs/pull namespace is what GitHub exposes for
    # PR review. Fetch runs OUTSIDE the per-repo lock (network op,
    # idempotent vs. concurrent fetches).
    fetch_code, fetch_out = _run(
        ["git", "-C", repo, "fetch", "origin", f"pull/{pr}/head:{local_branch}"],
        timeout=60.0,
    )
    if fetch_code != 0:
        # Git's actual error vocabulary for fetch-into-existing-branch:
        #   "non-fast-forward"            — local branch has divergent commits
        #   "refusing to fetch into branch ... checked out at" — branch is a
        #                                    current worktree HEAD elsewhere
        # Both indicate a previous review of this PR is still around; surface
        # 409 with a hint to clean up first. Everything else (network, auth)
        # is a 400 with the raw stderr.
        if "non-fast-forward" in fetch_out or "refusing to fetch" in fetch_out:
            raise HTTPException(
                409,
                f"local branch {local_branch!r} already in use — "
                f"remove the existing worktree/branch first: {fetch_out}",
            )
        raise HTTPException(400, f"git fetch failed: {fetch_out}")

    # Resolve the worktree path. Sibling layout, matches spawn_worktree.
    from periscope.worktree_spawn import WORKTREES_DIR, _slug_for_path
    repo_name = os.path.basename(repo.rstrip("/"))
    wt_path = str(WORKTREES_DIR / repo_name / _slug_for_path(local_branch))
    if os.path.exists(wt_path):
        raise HTTPException(409, f"worktree path already exists: {wt_path}")

    # Create the worktree at `pr-<N>` under the per-repo lock.
    with repo_lock(repo):
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        (WORKTREES_DIR / repo_name).mkdir(parents=True, exist_ok=True)
        code, out = _run(
            ["git", "-C", repo, "worktree", "add", wt_path, local_branch],
            timeout=30.0,
        )
        if code != 0:
            raise HTTPException(500, f"git worktree add failed: {out}")
    worktrees_invalidate(repo)

    pinned_dir = wt_path

    if pinned_dir in all_projects():
        # Race condition: someone adopted this path between our checks.
        # Rare; clean up the just-created worktree AND the orphaned local
        # branch to avoid leaving phase-6 cleanup-view bait. `--force` is
        # safe here because the worktree was just created with no user
        # content.
        _run(["git", "-C", repo, "worktree", "remove", "--force", wt_path])
        _run(["git", "-C", repo, "branch", "-D", local_branch])
        raise HTTPException(
            409, f"project already exists at {pinned_dir!r}"
        )

    # Apply the 2-window layout and capture the claude window's pid for the
    # synchronous linked_pr write.
    try:
        claude_pid = _layout_two_window(tmux_session, pinned_dir)
    except HTTPException:
        raise

    try:
        row = create_project(
            pinned_dir,
            name=name,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=base_branch,
        )
    except ValueError as e:
        _run(["tmux", "kill-session", "-t", tmux_session])
        raise HTTPException(409, str(e))

    # Write the PR link on the claude window. Future polls' resolve_pids
    # will see @periscope_id=<claude_pid> on the tmux window, recognize it
    # as a valid stamp, and refresh last_seen — the linked_pr field stays
    # because phase-1 added it to the GC immunity list.
    if claude_pid:
        set_window_fields(claude_pid, linked_pr=pr, is_fork=is_fork)

    result = {
        "ok": True,
        "pinned_dir": pinned_dir,
        "pr_number": pr,
        "is_fork": is_fork,
        "pr_state": pr_state,
        **row,
    }
    return result
```

- [ ] **Step 4: Add pytest coverage**

Note: `tests/routes/test_projects.py` does NOT exist yet — phases 1–3 didn't add coverage for the project endpoints (a gap to fix later). Phase 4 creates the file fresh with the standard fixture wiring and tests for the new endpoint only.

Create `tests/routes/test_projects.py`:

```python
"""Tests for /api/projects/*."""

import json
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)
```

Then append the phase-4 tests below.

```python
# === phase 4: PR review =====================================================

def _gh_view_json(pr_number, head_ref="feature-foo", is_cross=False, state="OPEN", base="main"):
    """Build a fake `gh pr view --json ...` output."""
    return json.dumps({
        "headRefName": head_ref,
        "isCrossRepository": is_cross,
        "headRepository": {"name": "fakerepo"},
        "baseRefName": base,
        "state": state,
    })


def _pr_review_run_sequence(repo_path, gh_output):
    """Ordered return values for `_run` covering the PR review flow:
      1. git rev-parse --show-toplevel        → (0, repo)
      2. tmux has-session                     → (1, "") meaning "session not found"
      3. gh pr view --json ...                → (0, gh_output)
      4. git fetch origin pull/N/head:pr-N    → (0, "")
      5. git worktree add <path> pr-N         → (0, "")
    The order MUST match the endpoint's actual call sequence.
    """
    return [
        (0, str(repo_path)),
        (1, ""),
        (0, gh_output),
        (0, ""),
        (0, ""),
    ]


def test_pr_review_success_same_repo(client, mocker, tmp_path):
    """Happy path: same-repo PR (isCrossRepository=False)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(42, is_cross=False)),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value="abcd1234")
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-42", "tmux_session": "pr-42", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    # Don't trip on the os.path.exists check for the worktree path —
    # tmp_path/repo doesn't already have ~/dev/worktrees/repo/pr-42.
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    set_fields = mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr_number"] == 42
    assert body["is_fork"] is False
    assert body["pr_state"] == "OPEN"
    set_fields.assert_called_once_with("abcd1234", linked_pr=42, is_fork=False)


def test_pr_review_fork_pr(client, mocker, tmp_path):
    """Fork PR: is_fork=True flag is written."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(99, is_cross=True)),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value="abcd1234")
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-99", "tmux_session": "pr-99", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    set_fields = mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 99,
    })
    assert r.status_code == 200
    assert r.json()["is_fork"] is True
    set_fields.assert_called_once_with("abcd1234", linked_pr=99, is_fork=True)


def test_pr_review_merged_pr_still_creates(client, mocker, tmp_path):
    """A merged PR should still create the project."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(7, state="MERGED")),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value="abcd1234")
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-7", "tmux_session": "pr-7", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 7,
    })
    assert r.status_code == 200
    assert r.json()["pr_state"] == "MERGED"


def test_pr_review_not_found(client, mocker, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=[
            (0, str(repo)),           # rev-parse
            (1, ""),                  # has-session: session-not-found is fine
            (1, "no pull requests found for branch"),  # gh pr view
        ],
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 9999,
    })
    assert r.status_code == 404


def test_pr_review_rejects_non_git(client, mocker, tmp_path):
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        return_value=(128, "fatal: not a git repository"),
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(not_a_repo), "pr_number": 1,
    })
    assert r.status_code == 400


def test_pr_review_rejects_invalid_pr_number(client, mocker, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        return_value=(0, str(repo)),
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 0,
    })
    assert r.status_code == 400


def test_pr_review_rejects_session_collision(client, mocker, tmp_path):
    """Pre-flight `tmux has-session` 409 fires before gh."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=[
            (0, str(repo)),  # rev-parse
            (0, ""),          # has-session returns 0 → session exists
        ],
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42,
    })
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
```

- [ ] **Step 5: Run pytest**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 && uv run pytest tests/routes/test_projects.py -x -v 2>&1 | tail -20
```

Expected: the new tests pass alongside existing ones.

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 commit -am "routes/projects: POST /api/projects/pr-review — gh + fetch + worktree + auto-link"
```

---

## Task 3: HTML slots

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add Review PR button to the filter bar**

In `static/index.html`, the existing `+ project` button is in the `<nav class="filters">` block. Add `Review PR` adjacent:

```html
<button id="review-pr-btn" class="filter-btn is-action" title="review a PR by number — creates a worktree at pr-<N> + claude session">📋 review PR</button>
```

Place it between `#new-project-btn` and `#send-bulk`.

- [ ] **Step 2: Add the modal markup**

After the existing `#new-project-modal` div, add:

```html
<div id="review-pr-modal" class="hidden review-pr-modal-overlay">
  <div class="review-pr-modal-card">
    <header class="review-pr-modal-head">
      <h2>review PR</h2>
      <button id="review-pr-modal-close" title="close">×</button>
    </header>
    <p class="review-pr-modal-sub">Fetches <code>pull/&lt;N&gt;/head:pr-&lt;N&gt;</code>, creates a worktree, opens a tmux session with claude.</p>
    <form id="review-pr-form">
      <label>
        Repo
        <input id="review-pr-repo" list="review-pr-repos" placeholder="/Users/tom/dev/foo" required>
        <datalist id="review-pr-repos"></datalist>
      </label>
      <label>
        PR number
        <input id="review-pr-number" type="number" min="1" placeholder="1234" required>
      </label>
      <label>
        Name <span class="review-pr-modal-hint">(defaults to pr-&lt;N&gt;)</span>
        <input id="review-pr-name" placeholder="optional">
      </label>
      <div id="review-pr-error" class="review-pr-modal-error" hidden></div>
      <div class="review-pr-modal-actions">
        <button type="button" id="review-pr-cancel">cancel</button>
        <button type="submit" id="review-pr-submit">review</button>
      </div>
    </form>
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 commit -am "index.html: review PR top-bar button + modal markup"
```

---

## Task 4: `review-pr-modal.js` + CSS + app.js wiring

**Files:**
- Create: `static/review-pr-modal.js`
- Modify: `static/app.js`
- Modify: `static/styles.css`

- [ ] **Step 1: Write the modal module**

Create `static/review-pr-modal.js` (mirrors `new-project-modal.js`):

```javascript
// Review PR modal. Repo + PR number → POST /api/projects/pr-review.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("review-pr-modal");
const closeBtn = document.getElementById("review-pr-modal-close");
const cancelBtn = document.getElementById("review-pr-cancel");
const form = document.getElementById("review-pr-form");
const repoInput = document.getElementById("review-pr-repo");
const prInput = document.getElementById("review-pr-number");
const nameInput = document.getElementById("review-pr-name");
const reposListEl = document.getElementById("review-pr-repos");
const errorEl = document.getElementById("review-pr-error");
const submitBtn = document.getElementById("review-pr-submit");

let cached = { repos: [] };
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

async function refresh() {
  try {
    const res = await fetch("/api/projects/discoverable");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    cached = await res.json();
    renderRepoOptions();
  } catch (e) {
    showError(`failed to load repos: ${e.message}`);
  }
}

export async function openReviewPRModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  modal.classList.remove("hidden");
  document.body.classList.add("review-pr-modal-open");
  pushEscape(closeReviewPRModal);
  repoInput.value = "";
  prInput.value = "";
  nameInput.value = "";
  await refresh();
  repoInput.focus();
}

export function closeReviewPRModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("review-pr-modal-open");
  popEscape(closeReviewPRModal);
}

async function handleSubmit(e) {
  e.preventDefault();
  clearError();
  const repo = repoInput.value.trim();
  const pr = parseInt(prInput.value, 10);
  const name = nameInput.value.trim();
  if (!repo) {
    showError("repo is required");
    return;
  }
  if (!pr || pr <= 0) {
    showError("PR number must be a positive integer");
    return;
  }
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/projects/pr-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, pr_number: pr, name: name || undefined }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    closeReviewPRModal();
  } catch (e) {
    showError(`request failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initReviewPRModal() {
  const openBtn = document.getElementById("review-pr-btn");
  if (openBtn) openBtn.addEventListener("click", openReviewPRModal);
  closeBtn.addEventListener("click", closeReviewPRModal);
  cancelBtn.addEventListener("click", closeReviewPRModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeReviewPRModal();
  });
  form.addEventListener("submit", handleSubmit);
}
```

- [ ] **Step 2: Wire `initReviewPRModal` in `static/app.js`**

Alongside the existing `initNewProjectModal()` call:

- Add import:
  ```javascript
  import { initReviewPRModal } from './review-pr-modal.js';
  ```
- Add init call (immediately after `initNewProjectModal();`):
  ```javascript
  initReviewPRModal();
  ```

- [ ] **Step 3: Add CSS**

Append to `static/styles.css`. Since the visual pattern is identical to `new-project-modal`, the simplest is to add the `.review-pr-modal-*` selectors as comma-separated aliases on the existing `.new-project-modal-*` rules. Concrete: locate the existing `.new-project-modal-overlay` block and add `.review-pr-modal-overlay` to the selector. Same for `.new-project-modal-card`, `.new-project-modal-head`, `.new-project-modal-sub`, `.new-project-modal-error`, `.new-project-modal-actions`, and the form-input rules.

If aliasing is awkward in the existing file (e.g. you need to wrap form-input rules in a parent selector), just copy the rules wholesale with the `review-pr-modal` prefix. Code duplication is acceptable for a self-contained visual block.

- [ ] **Step 4: Verify the JS imports + button renders**

TestClient sanity check (no live tmux needed):

```bash
cd /Users/tom/dev/periscope && uv run --with httpx python3 -c "
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)
r = client.get('/static/review-pr-modal.js')
print('JS status:', r.status_code, 'bytes:', len(r.content))
assert r.status_code == 200 and 'initReviewPRModal' in r.text
r = client.get('/')
print('HTML has #review-pr-btn:', 'id=\"review-pr-btn\"' in r.text)
print('HTML has #review-pr-modal:', 'id=\"review-pr-modal\"' in r.text)
print('PASS')
"
```

Expected: PASS.

If you can open a browser, manual smoke: click Review PR → modal opens, datalists populated. Submit with a real PR number works against an authenticated `gh`.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 commit -am "review-pr-modal: open/close + populate from /api/projects/discoverable + submit to /api/projects/pr-review"
```

---

## Task 5: end-to-end smoke

- [ ] **Step 1: Full pytest suite**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 && uv run pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all green (existing + new phase-4 tests).

- [ ] **Step 2: TestClient round-trip with mocked git + gh**

The PR review endpoint's happy path is already covered by the pytest tests in Task 2. This step is a lighter sanity check that the synchronously-stamped pid receives the `linked_pr` write end-to-end.

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-4 && XDG_CONFIG_HOME=/tmp/p4-smoke uv run --with httpx python3 << 'EOF'
"""Phase-4 smoke: synchronous pid stamping + linked_pr write."""
import os, subprocess
from fastapi.testclient import TestClient
from periscope.app import app
from periscope.pids import stamp_new_window
from periscope.store import set_window_fields, get_window
client = TestClient(app)

# 1. Pid stamping survives round-trip.
sess = "p4-pid-test"
subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-c", "/tmp"], check=True)
try:
    pid = stamp_new_window(f"{sess}:0")
    print(f"[1] stamped pid: {pid}")
    # Confirm tmux reports it.
    out = subprocess.run(
        ["tmux", "show-options", "-w", "-t", f"{sess}:0", "@periscope_id"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert pid in out

    # 2. set_window_fields writes survive.
    set_window_fields(pid, linked_pr=42, is_fork=False)
    annot = get_window(pid)
    print(f"[2] window annotation: {annot}")
    assert annot.get("linked_pr") == 42

    # 3. /api/state poll picks up the linked PR (the stamped pid + state
    # together drive the card's #PR badge via views.py).
    r = client.get("/api/state")
    assert r.status_code == 200
    matching = [w for w in r.json()["windows"] if w["session"] == sess]
    assert matching, f"expected our session to appear in /api/state: {sess}"
    w = matching[0]
    print(f"[3] window view pr_linked={w.get('pr_linked')}, pr={w.get('pr')}")
    # linked_pr writes are surfaced via the pr field (with pr_linked: true)
    # in views.py.
    assert w.get("pr_linked") is True
    assert str(w.get("pr")) == "42"
    print("PASS")
finally:
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
EOF
rm -rf /tmp/p4-smoke
```

Expected: PASS.

- [ ] **Step 3: (Optional) Real-PR test with live `gh`**

If you want to verify the full PR review verb against a real PR, run interactively:

```bash
# Pick a real open PR on a repo you have gh-authenticated access to.
PR=12345  # set to a real PR number
REPO=/Users/tom/dev/fdy  # or whichever
curl -s -X POST http://127.0.0.1:8765/api/projects/pr-review \
  -H "Content-Type: application/json" \
  -d "{\"repo\": \"$REPO\", \"pr_number\": $PR}" | python3 -m json.tool
```

Expected: 200 response with `pinned_dir`, `pr_number`, `is_fork`, `pr_state`. The new project appears in the dashboard within 3s with `#$PR` badge on its claude window.

Cleanup:
```bash
tmux kill-session -t pr-$PR 2>/dev/null
git -C $REPO worktree remove --force ~/dev/worktrees/$(basename $REPO)/pr-$PR 2>/dev/null
git -C $REPO branch -D pr-$PR 2>/dev/null
```

---

## What's deliberately NOT in phase 4

- **Global PR lookup** (`gh pr view <url>` without a known repo path) — defer.
- **Per-project "review another PR" ⋯ menu item** — defer to phase 7 polish.
- **Banner for closed/merged PRs** — defer to phase 7.
- **Cleanup logic that consumes `is_fork`** — phase 6 consumes; phase 4 only writes.
- **Branch-name preservation** for same-repo PRs — always `pr-<N>`; user can rename via ⋯.

The phase-4 endpoint (`POST /api/projects/pr-review`) is stable. Phase 6's cleanup view will read the persisted `linked_pr` + `is_fork` fields without changes here.
