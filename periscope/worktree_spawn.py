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


def _resolve_layout(repo: str) -> str:
    """Return the worktree-layout string for `repo`. Order:
      1. settings.worktree_layout_overrides[repo] (sticky once set)
      2. Auto-detect from existing worktrees: if all non-main worktrees
         match the sibling pattern → 'sibling'; if all match inline →
         'inline'; mixed or zero → fall back to default.
      3. settings.worktree_layout_default (= 'sibling' if unset).

    Auto-detect runs ONCE per repo per process — once a layout is
    written to overrides, we never re-detect (Tom's design call: the
    first spawn determines the convention).

    Always-writes to `settings.worktree_layout_overrides[realpath(repo)]`
    after deciding, so subsequent spawns are O(1) settings lookups.
    """
    from periscope.store import get_settings, update_settings

    repo_real = os.path.realpath(repo)
    s = get_settings()
    overrides = s.get("worktree_layout_overrides") or {}
    if repo_real in overrides:
        return overrides[repo_real]

    default = s.get("worktree_layout_default") or "sibling"

    # Auto-detect.
    detected: set[str] = set()
    for wt_path, _branch in worktrees._cached_worktrees(repo_real):
        wt_real = os.path.realpath(wt_path)
        if wt_real == repo_real:
            continue  # skip main checkout
        if wt_real.startswith(str(WORKTREES_DIR) + "/"):
            detected.add("sibling")
        elif wt_real.startswith(os.path.join(repo_real, ".worktrees") + "/"):
            detected.add("inline")
    if len(detected) == 1:
        layout = next(iter(detected))
    else:
        layout = default

    # Record + persist.
    new_overrides = {**overrides, repo_real: layout}
    update_settings({"worktree_layout_overrides": new_overrides})
    return layout


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
    fetch: bool = True,
) -> dict:
    """Create a worktree of `repo` at branch `branch`, forked from
    `origin/<base_branch>` (or the detected default branch).

    When `fetch=True` (default), fetches origin/<base> first. When
    `fetch=False`, skips the network call and forks from the local
    <base_branch> ref directly, intended for new-worktree-tab callers
    where `base_branch` is the project's own (typically unpushed)
    feature branch.

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

    # Resolve layout + worktree path.
    layout = _resolve_layout(repo)
    if layout == "inline":
        # `<repo>/.worktrees/<branch-slugged>` — splash convention.
        wt_path = Path(repo) / ".worktrees" / _slug_for_path(branch)
    else:
        # Default: sibling layout.
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

    # Fetch runs OUTSIDE the per-repo lock (network op, idempotent vs.
    # concurrent fetches). Skipped when `fetch=False` — callers spawning
    # off a local-only ref (e.g. an unpushed project branch) don't want
    # to fetch and don't need the remote to be up to date.
    # Phase-1's repo_locks.py:33-35 documents this: "Callers should hold
    # the lock only across the git mutation itself, not surrounding work."
    if fetch:
        fetch_code, fetch_out = _run(
            ["git", "-C", repo, "fetch", "origin", base], timeout=30.0
        )
        if fetch_code != 0:
            warning = f"fetch failed: origin/{base} may be stale ({fetch_out!r})"
            log.warning("worktree_spawn: %s", warning)

    with repo_lock(repo):
        # Ensure parent dir exists for both layouts. mkdir(parents=True)
        # handles arbitrary depth.
        wt_path.parent.mkdir(parents=True, exist_ok=True)

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
        if code != 0:
            raise ValueError(f"git worktree add failed: {out}")

    worktrees.invalidate(repo)

    result = {"path": wt_path_str, "base_branch": base, "branch": branch}
    if warning:
        result["warning"] = warning
    return result
