"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""
import os
from dataclasses import dataclass

from periscope import projects, worktrees
from periscope.gitutil import resolve_repo_and_branch
from periscope.panes import list_windows
from periscope.tmux import _run, _tmux_mutate
from periscope.worktree_spawn import _layout_two_window


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


def worktree_for_branch(repo: str, branch: str) -> str | None:
    """Path of an existing worktree checked out on `branch`, or None.
    Authoritative source is `git worktree list` (via the 60s cache), not a
    recomputed path."""
    for path, wt_branch in worktrees._cached_worktrees(repo):
        if wt_branch == branch:
            return os.path.realpath(path)
    return None


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


def _session_live(name: str) -> bool:
    return _tmux_mutate("has-session", "-t", name)[0]   # socket-aware; False when missing


def _session_owns_dir(name: str, pinned_dir: str) -> bool:
    """True if any window of session `name` sits at `pinned_dir` (realpath)."""
    return any(w["session"] == name
               and os.path.realpath(w.get("cwd") or "") == pinned_dir
               for w in list_windows())


def _claude_pid_for_session(name: str) -> str:
    """The @periscope_id (pid_raw) of the session's claude window — matched by
    window NAME ('claude'), since list_windows() carries no is_claude flag.
    Falls back to the first window. '' if none."""
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
    """Idempotent create-or-focus. `pinned_dir` is the project's key (taken
    explicitly — Project is a TypedDict with no self-key). Returns
    (tmux_session, claude_pid)."""
    name = project["tmux_session"]
    if _session_live(name):
        if _session_owns_dir(name, pinned_dir):
            return name, _claude_pid_for_session(name)
        name = _dedupe_name(name)                      # live but foreign
        projects.update_project(pinned_dir, tmux_session=name)
    claude_pid, _ = _layout_two_window(name, pinned_dir)
    return name, claude_pid
