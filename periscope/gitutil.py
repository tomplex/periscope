"""Low-level git helpers: repo-root resolution and default-branch
detection.

Distinct from `git_pr.py`, which owns the cached git/PR *state* the
dashboard renders. These are stateless primitives — given a directory,
shell out to `git` and return a path or branch name. Kept in their own
module so import-sensitive callers (notably `store.py`, whose v2
migration runs at import time) can use them without pulling in
`git_pr.py`'s heavier import graph. Imports only `tmux._run`.
"""

import os
import re

from periscope.tmux import _run


def resolve_repo(pinned_dir: str) -> str:
    """Resolve a checkout/worktree directory to its repo root.

    `git rev-parse --git-common-dir` returns <root>/.git for a normal
    checkout — so the repo is that dir's toplevel — or the *shared* .git of
    the main checkout for a linked worktree, whose parent is the repo.
    Falls back to `pinned_dir` itself when git can't answer.
    """
    code, common = _run(["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"])
    if code == 0 and common:
        common_abs = (
            common if os.path.isabs(common) else os.path.join(pinned_dir, common)
        )
        return os.path.realpath(os.path.dirname(common_abs))
    return pinned_dir


def resolve_repo_and_branch(pinned_dir: str) -> tuple[str, str]:
    """Resolve a directory to `(repo_root, base_branch)`.

    `base_branch` is the directory's current branch, or "" when detached
    (`--abbrev-ref` reports "HEAD"). Used by the v2 migration and the
    project-creation routes to populate a project's `repo` + `base_branch`.
    """
    repo = resolve_repo(pinned_dir)
    _, branch = _run(["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        branch = ""
    return repo, branch


def detect_default_branch(repo: str) -> str:
    """Resolve a repo's default branch — 'main' / 'master' / similar.

    Tries `git symbolic-ref refs/remotes/origin/HEAD`; falls back to
    probing local branches for main/master; defaults to 'main' (a caller's
    fetch then fails loudly if that guess is wrong).
    """
    code, ref = _run(
        ["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"],
        timeout=3.0,
    )
    if code == 0 and ref:
        # e.g. refs/remotes/origin/main → "main"
        return ref.rsplit("/", 1)[-1]
    # Fallback: probe local branches.
    code, out = _run(
        ["git", "-C", repo, "branch", "--format=%(refname:short)"],
        timeout=3.0,
    )
    branches = out.split("\n") if code == 0 else []
    for candidate in ("main", "master"):
        if candidate in branches:
            return candidate
    return "main"


def github_slug(directory: str) -> str | None:
    """The `owner/repo` GitHub slug for the repo containing `directory`,
    derived from `git remote get-url origin`. None when there is no origin
    remote or it doesn't point at github.com.

    Handles the common origin URL forms:
      git@github.com:owner/repo.git
      https://github.com/owner/repo.git
      https://github.com/owner/repo
      ssh://git@github.com/owner/repo.git
    """
    code, url = _run(["git", "-C", directory, "remote", "get-url", "origin"])
    if code != 0 or not url:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None
