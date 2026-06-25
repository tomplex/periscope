"""Per-repo threading.Lock registry.

`git worktree add` is not atomic with branch creation; concurrent
spawns against the same repo race. The coarse store._STATE_LOCK is
too coarse — it would serialize unrelated state writes during a slow
git operation. Instead, every git-mutating verb acquires a lock
keyed on the repo's realpath.

This module is used by phase 2+ verbs (new project, new worktree-tab,
PR review, cleanup). Phase 1 doesn't itself call any git-mutating
verb, but introduces the infrastructure so later phases don't
re-invent it.
"""

import contextlib
import os
import threading

_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _key(repo_path: str) -> str:
    """Canonicalize a repo path to its registry key."""
    return os.path.realpath(repo_path)


@contextlib.contextmanager
def repo_lock(repo_path: str):
    """Hold the per-repo lock for the duration of the `with` block.

    Nested acquisition of the SAME repo by the SAME thread will
    deadlock — these are non-reentrant. Callers should hold the lock
    only across the git mutation itself, not surrounding state work.

    Multiple concurrent operations on DIFFERENT repos run in parallel.
    """
    key = _key(repo_path)
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
    with lock:
        yield
