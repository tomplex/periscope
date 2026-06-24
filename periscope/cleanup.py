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
from typing import TypedDict

from periscope import worktrees
from periscope.gitutil import detect_default_branch
from periscope.log import log
from periscope.projects import MAIN_KEY, all_projects
from periscope.store import get_settings
from periscope.tmux import _run

# === Caches ================================================================

_PR_STATE_TTL = 300.0  # 5 minutes
_BRANCH_TTL = 60.0

_lock = threading.Lock()
_pr_state_cache: dict[tuple[str, int], tuple[float, str | None]] = {}
_branch_merged_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_remote_branch_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_default_branch_cache: dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.time()


# === Signal helpers ========================================================

def _detect_default_branch(repo: str) -> str:
    """Cached default-branch lookup — 5-min TTL, repo defaults don't churn.
    The uncached resolution lives in `gitutil.detect_default_branch`."""
    with _lock:
        cached = _default_branch_cache.get(repo)
        if cached and _now() - cached[0] < _PR_STATE_TTL:
            return cached[1]
    branch = detect_default_branch(repo)
    with _lock:
        _default_branch_cache[repo] = (_now(), branch)
    return branch


def _pr_state(repo: str, pr_number: int) -> str | None:
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
    state: str | None = None
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
    project_name: str | None  # null if untracked
    tmux_session: str | None  # null if untracked
    repo: str  # the repo's main-checkout path
    branch: str  # the worktree's current branch (or "(detached)")
    is_fork: bool  # from state.windows[pid].is_fork on the project's claude window
    signals: list[Signal]
    dirty: bool
    untracked: bool  # true if no project row exists for this pinned_dir
    idle_days: int  # days since last commit on the worktree's branch


# === Main entry point ======================================================

IDLE_THRESHOLD_DAYS = 14


def _evaluate_worktree(
    wt_path: str,
    branch: str | None,
    repo: str,
    default: str,
    project_by_pinned: dict[str, dict],
    windows_snapshot: dict[str, dict],
    alive_sessions: set[str],
    idle_threshold: int,
) -> Candidate | None:
    """Evaluate one worktree of `repo`. Returns a Candidate when any
    staleness signal fires, or None — for the repo's own main checkout, or
    a worktree that is healthy from cleanup's perspective.
    """
    # Skip the main checkout itself — it's not a worktree we'd ever
    # clean up.
    if os.path.realpath(wt_path) == os.path.realpath(repo):
        return None

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
        for ann in windows_snapshot.values():
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
    if (
        branch != "(detached)"
        and branch != default
        and _is_branch_merged(repo, branch, default)
        and not any(s["kind"].startswith("pr_") for s in signals)
    ):
        signals.append({"kind": "branch_merged", "label": f"branch merged into {default}"})

    # Signal 3: remote branch deleted. Skipped for fork PRs
    # where the local branch was never on origin.
    if (
        branch != "(detached)"
        and branch != default
        and not is_fork
        and not _remote_branch_exists(repo, branch)
    ):
        signals.append({"kind": "remote_gone", "label": f"origin/{branch} deleted"})

    # Signal 4: idle. Days since last commit on the worktree's
    # HEAD; only flagged when no active tmux session AND
    # > threshold. Session-alive check uses the hoisted
    # `alive_sessions` set — no per-candidate tmux call.
    idle_days = _last_commit_age_days(wt_path)
    session_alive = bool(tmux_session and tmux_session in alive_sessions)
    if not session_alive and idle_days > idle_threshold:
        signals.append({"kind": "idle", "label": f"idle {idle_days}d"})

    if not signals:
        # Worktree is healthy from cleanup's perspective.
        return None

    dirty = _is_dirty(wt_path)

    return {
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
    }


def compute_candidates(repo_filter: str | None = None) -> list[Candidate]:
    """Walk every repo periscope knows about and return the cleanup
    candidate list. A worktree appears as a candidate if ANY signal
    fires.

    `repo_filter` (optional realpath): scope to a single repo. Used by
    a future per-project cleanup view (not in phase 6).
    """
    projects = all_projects()
    # Build (repo → project_rows) so we can correlate worktrees with
    # their owning project rows. We also need to surface UNTRACKED
    # worktrees: ones on disk via `git worktree list` with no matching project row.
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
    idle_threshold = int(get_settings().get("cleanup_idle_days") or IDLE_THRESHOLD_DAYS)

    candidates: list[Candidate] = []
    for repo in sorted(repos):
        default = _detect_default_branch(repo)
        # `_cached_worktrees` returns [(realpath, branch_or_none), ...]
        for wt_path, branch in worktrees._cached_worktrees(repo):
            cand = _evaluate_worktree(
                wt_path, branch, repo, default,
                project_by_pinned, windows_snapshot,
                alive_sessions, idle_threshold,
            )
            if cand is not None:
                candidates.append(cand)

    return candidates
