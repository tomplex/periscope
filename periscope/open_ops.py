"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""
import os
from dataclasses import dataclass

from periscope import projects
from periscope.gitutil import resolve_repo_and_branch
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
