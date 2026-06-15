"""projects[pinned_dir]: project lifecycle metadata.

A project = pinned directory + repo + tmux session. Identity is
pinned_dir (absolute path, realpath'd). The `__main__` sentinel is
the unpinned catch-all (see spec §"Main project").

Accessors hold periscope.store._STATE_LOCK internally and persist
mutations via _write_state. Read accessors return copies.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, Optional

from fastapi import HTTPException

from periscope import store as _store
from periscope.log import log
from periscope.repo_locks import repo_lock
from periscope.tmux import _run
from periscope.worktree_spawn import worktree_path
from periscope.worktrees import invalidate as worktrees_invalidate


MAIN_KEY = "__main__"


class Project(TypedDict, total=False):
    """A row in state['projects']."""
    name: str
    tmux_session: str
    repo: Optional[str]
    pinned_repo: Optional[str]
    created_at: int
    archived_at: Optional[int]
    base_branch: Optional[str]


def _canonical_key(pinned_dir: str) -> str:
    """Write-time canonicalization: absolute, realpath'd, no trailing slash.
    Called on insert paths only (create_project + the migration).

    For READS, prefer `_lookup_key` — it tries the literal first and
    only realpaths on miss. Realpath is multiple syscalls per call and
    we're hot-path in `build_window_view` per poll.
    """
    if pinned_dir == MAIN_KEY:
        return MAIN_KEY
    if not pinned_dir.startswith("/"):
        raise ValueError(f"pinned_dir must be absolute: {pinned_dir!r}")
    return os.path.realpath(pinned_dir).rstrip("/") or "/"


def _lookup_key(pinned_dir: str, projects: dict) -> str:
    """Read-time canonicalization: try the literal key first (the
    common case — keys in `projects` are already canonical after
    migration / create). Fall back to realpath only on miss.
    """
    if pinned_dir == MAIN_KEY:
        return MAIN_KEY
    if pinned_dir in projects:
        return pinned_dir
    rstripped = pinned_dir.rstrip("/") or "/"
    if rstripped in projects:
        return rstripped
    # Cold path: realpath. Symlinked input paths fall through here.
    if pinned_dir.startswith("/"):
        return os.path.realpath(pinned_dir).rstrip("/") or "/"
    return pinned_dir


def get_project(pinned_dir: str) -> Project:
    """Return a copy of projects[pinned_dir], or an empty dict if unknown."""
    with _store._STATE_LOCK:
        projects = _store._STATE.get("projects", {})
        key = _lookup_key(pinned_dir, projects)
        return dict(projects.get(key, {}))  # type: ignore[return-value]


def all_projects() -> dict[str, Project]:
    """Snapshot of all projects (copies)."""
    with _store._STATE_LOCK:
        return {
            k: dict(v)
            for k, v in _store._STATE.get("projects", {}).items()
        }


def create_project(pinned_dir: str, **fields) -> Project:
    """Insert a new project row. Raises ValueError on duplicate or
    non-absolute pinned_dir.

    `fields` should include at least `name` and `tmux_session`. Missing
    optional fields default to None / 0.
    """
    key = _canonical_key(pinned_dir)  # write-time realpath
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        if key in projects:
            raise ValueError(f"project already exists at {key!r}")
        row: Project = {
            "name": fields.get("name", ""),
            "tmux_session": fields.get("tmux_session", ""),
            "repo": fields.get("repo"),
            "pinned_repo": fields.get("pinned_repo"),
            "created_at": fields.get("created_at", int(time.time())),
            "archived_at": fields.get("archived_at"),
            "base_branch": fields.get("base_branch"),
        }
        projects[key] = dict(row)
        _store._write_state(_store._STATE)
        return dict(row)


def update_project(pinned_dir: str, **fields) -> bool:
    """Merge `fields` into projects[pinned_dir] and persist. Returns True
    if the project existed. Cannot modify identity (pinned_dir itself).
    None values overwrite — use them to clear archived_at, base_branch, etc.
    """
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        key = _lookup_key(pinned_dir, projects)
        if key not in projects:
            return False
        # __main__ is restricted: only tmux_session is mutable on it.
        if key == MAIN_KEY:
            for k in list(fields.keys()):
                if k != "tmux_session":
                    log.warning("ignoring update to __main__.%s", k)
                    fields.pop(k, None)
        projects[key].update(fields)
        _store._write_state(_store._STATE)
        return True


def archive_project(pinned_dir: str) -> bool:
    """Set archived_at to now. __main__ is never archivable."""
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        key = _lookup_key(pinned_dir, projects)
        if key == MAIN_KEY:
            raise ValueError("cannot archive __main__")
        if key not in projects:
            return False
        projects[key]["archived_at"] = int(time.time())
        _store._write_state(_store._STATE)
        return True


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


# ---------------------------------------------------------------------------
# PR-fetch helpers (moved from routes/projects.py)
# ---------------------------------------------------------------------------

def _resolve_pr_metadata(repo: str, pr: int) -> dict:
    """`gh pr view` for PR #pr in `repo`, returning the parsed metadata.

    Raises HTTPException: 404 if the PR doesn't exist, 400 if the gh call
    fails for any other reason, 500 if gh returns unparseable JSON.
    """
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
            raise HTTPException(404, f"PR #{pr} not found in {repo}: {out}")
        raise HTTPException(400, f"gh pr view failed: {out}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"gh pr view returned invalid JSON: {e}")


def _fetch_pr_branch(repo: str, pr: int, local_branch: str) -> None:
    """Fetch PR #pr's head commits into local branch `local_branch` via the
    `pull/<N>/head` refspec (uniform for same-repo and fork PRs). Runs
    outside the per-repo lock — a network op, idempotent vs. concurrent
    fetches.

    Raises HTTPException: 409 if the local branch is already in use (a
    prior review of this PR is still around), 400 on any other failure.
    """
    fetch_code, fetch_out = _run(
        ["git", "-C", repo, "fetch", "origin", f"pull/{pr}/head:{local_branch}"],
        timeout=60.0,
    )
    if fetch_code != 0:
        # Git's fetch-into-existing-branch error vocabulary:
        #   "non-fast-forward"  — local branch has divergent commits
        #   "refusing to fetch" — branch is a current worktree HEAD elsewhere
        # Both mean a previous review of this PR is still around → 409 with
        # a cleanup hint. Everything else (network, auth) is a 400.
        if "non-fast-forward" in fetch_out or "refusing to fetch" in fetch_out:
            raise HTTPException(
                409,
                f"local branch {local_branch!r} already in use — "
                f"remove the existing worktree/branch first: {fetch_out}",
            )
        raise HTTPException(400, f"git fetch failed: {fetch_out}")


def _discard_pr_worktree(repo: str, wt_path: str, local_branch: str) -> None:
    """Roll back a just-created PR-review worktree: force-remove it and
    delete its orphan local branch. Used when a later step fails after the
    worktree exists — leaving it would be undetectable cleanup-view bait.
    `--force` is safe: the worktree was just created with no user content.
    """
    _run(["git", "-C", repo, "worktree", "remove", "--force", wt_path])
    _run(["git", "-C", repo, "branch", "-D", local_branch])


@dataclass(frozen=True)
class PRWorktree:
    path: str
    base_branch: str
    is_fork: bool
    local_branch: str
    pr_state: str   # OPEN / CLOSED / MERGED (uppercased)
    name: str       # resolved project name (head_ref or local_branch fallback)


def fetch_pr_into_worktree(repo: str, pr: int) -> PRWorktree:
    """Fetch PR #pr into a fresh worktree under `repo`, preserving the
    route's rollback semantics: on any failure after the worktree exists,
    `_discard_pr_worktree` force-removes it and deletes the orphan branch.
    Raises ValueError on bad input; HTTPException on gh/git failures
    (the caller maps). Returns the worktree metadata.
    """
    if pr <= 0:
        raise ValueError(f"pr must be positive: {pr}")

    local_branch = f"pr-{pr}"

    meta = _resolve_pr_metadata(repo, pr)

    is_fork = bool(meta.get("isCrossRepository"))
    pr_state = (meta.get("state") or "").upper()  # OPEN / CLOSED / MERGED
    base_branch = meta.get("baseRefName") or None

    head_ref = (meta.get("headRefName") or "").strip()
    name = (head_ref or local_branch).strip()

    # Fetch the PR head into local branch `pr-<N>`.
    _fetch_pr_branch(repo, pr, local_branch)

    # Worktree path honors the repo's inline/sibling layout. Slugged from
    # the PR's head branch so the dir on disk is recognizable, not `pr-<N>`.
    wt_path = worktree_path(repo, name)

    if os.path.exists(wt_path):
        raise HTTPException(409, f"worktree path already exists: {wt_path}")

    # Create the worktree at `pr-<N>` under the per-repo lock. On failure,
    # delete the orphan `pr-<N>` branch the fetch created — otherwise a
    # retry hits the "non-fast-forward" path and 409s with a confusing error.
    with repo_lock(repo):
        Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
        code, out = _run(
            ["git", "-C", repo, "worktree", "add", wt_path, local_branch],
            timeout=30.0,
        )
        if code != 0:
            _run(["git", "-C", repo, "branch", "-D", local_branch])
            raise HTTPException(500, f"git worktree add failed: {out}")
    worktrees_invalidate(repo)

    return PRWorktree(
        path=wt_path,
        base_branch=base_branch,
        is_fork=is_fork,
        local_branch=local_branch,
        pr_state=pr_state,
        name=name,
    )
