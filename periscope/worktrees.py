"""Worktree affiliation per pane.

A project pins to one directory (worktree or repo root). A pane's
cwd is normally inside the pinned dir, but may be inside another
worktree of the same repo (sibling) or completely off-repo. We
classify and return a chip-rendering hint.

Caches `git worktree list --porcelain` per repo for 60s. Invalidation
on writes is the caller's responsibility (phase 2+: after worktree
add/remove, call invalidate(repo)).
"""

import os
import threading
import time
from typing import TypedDict

from periscope.tmux import _run

_TTL_S = 60.0
_lock = threading.Lock()
# repo_realpath → (fetched_at, [(worktree_realpath, branch_or_none), ...])
_cache: dict[str, tuple[float, list[tuple[str, str | None]]]] = {}


class Affiliation(TypedDict):
    kind: str  # "at-pin" | "sibling" | "off-repo" | "no-repo"
    label: str | None  # branch or worktree basename for chip text


def _list_worktrees(repo: str) -> list[tuple[str, str | None]]:
    """Return [(worktree_path_realpath, branch_or_None), ...] for the repo.
    `repo` may be a worktree path; git resolves to the main checkout
    internally. We pass `-C repo` so this works either way.
    """
    code, out = _run(
        ["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=3.0
    )
    if code != 0 or not out:
        return []
    rows: list[tuple[str, str | None]] = []
    current_path: str | None = None
    current_branch: str | None = None
    for line in out.split("\n"):
        if line.startswith("worktree "):
            if current_path is not None:
                rows.append((current_path, current_branch))
            current_path = line[len("worktree "):]
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current_branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            current_branch = None
    if current_path is not None:
        rows.append((current_path, current_branch))
    # Realpath each so symlinked entry points compare equal.
    return [(os.path.realpath(p), b) for p, b in rows]


def _cached_worktrees(repo: str) -> list[tuple[str, str | None]]:
    key = os.path.realpath(repo)
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _TTL_S:
            return cached[1]
    fresh = _list_worktrees(repo)
    with _lock:
        _cache[key] = (now, fresh)
    return fresh


def invalidate(repo: str) -> None:
    """Drop the cache entry for this repo. Call after `git worktree add`
    or `git worktree remove`."""
    key = os.path.realpath(repo)
    with _lock:
        _cache.pop(key, None)


def affiliation(cwd: str, pinned_dir: str | None, repo: str | None) -> Affiliation:
    """Classify a pane's cwd relative to its project's pinned_dir + repo.

    Returns:
      {"kind": "no-repo", "label": None}  — project has no repo (main, or
                                            non-git pinned dir)
      {"kind": "at-pin", "label": None}   — cwd is at pinned_dir or a subdir
      {"kind": "sibling", "label": branch}— cwd is in another worktree of
                                            the project's repo
      {"kind": "off-repo", "label": basename(cwd)}
                                          — cwd is not in any worktree of
                                            the project's repo
    """
    if not cwd:
        return {"kind": "no-repo", "label": None}
    cwd_real = os.path.realpath(cwd)
    if not pinned_dir or not repo:
        return {"kind": "no-repo", "label": None}
    pin_real = os.path.realpath(pinned_dir)
    # cwd inside pinned_dir (or pinned_dir == cwd_real exactly).
    if cwd_real == pin_real or cwd_real.startswith(pin_real.rstrip("/") + "/"):
        return {"kind": "at-pin", "label": None}
    # Cross-worktree of same repo?
    for wt_path, branch in _cached_worktrees(repo):
        if cwd_real == wt_path or cwd_real.startswith(wt_path.rstrip("/") + "/"):
            return {"kind": "sibling", "label": branch or os.path.basename(wt_path)}
    return {"kind": "off-repo", "label": os.path.basename(cwd_real) or "?"}
