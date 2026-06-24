"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from periscope import projects, store, worktrees
from periscope.gitutil import detect_default_branch, resolve_repo, resolve_repo_and_branch
from periscope.panes import list_windows
from periscope.tmux import _run, _tmux_mutate
from periscope.worktree_spawn import _layout_two_window, spawn_worktree


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
    claude_pid: str          # @periscope_id (pid_raw) of the claude window
    claude_pane_id: str      # tmux pane_id (%N) of the claude window
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


def place_in_rail(tmux_session: str, project: projects.Project,
                  pane_pids: list[str]) -> dict:
    """Append the session to the rail prefs (idempotent) and return the
    full ui blob for the client to write into prefsSignal."""
    ui = store.get_ui()
    repo = project["repo"]
    order = list(ui.get("repo_order", []))
    if repo not in order:
        order.append(repo)
    wts = {k: list(v) for k, v in ui.get("worktrees_by_repo", {}).items()}
    wt_list = wts.setdefault(repo, [])
    if tmux_session not in wt_list:
        wt_list.append(tmux_session)
    panes = {k: list(v) for k, v in ui.get("panes_by_worktree", {}).items()}
    if tmux_session not in panes:
        panes[tmux_session] = list(pane_pids)
    patch = {"repo_order": order, "worktrees_by_repo": wts,
             "panes_by_worktree": panes}
    store.update_ui(patch)
    return store.get_ui()


def resolve_worktree_session(path: str) -> tuple[str, projects.Project] | None:
    """Resolve a cwd to the `(session_name, project)` a pane should spawn into
    to appear as its OWN rail item — registering the project if absent and
    deduping a live-but-foreign session name (same name-collision handling as
    `ensure_session`). Creates no windows: the caller adds the pane itself
    (new tmux session when the name is free, new tab when it already owns the
    worktree). Returns None when `path` isn't inside a git repo — there's no
    worktree to anchor a rail item to, so the caller falls back to its session.
    """
    try:
        toplevel = _git_toplevel(path)
    except ValueError:
        return None
    repo = resolve_repo(toplevel)
    project = ensure_project(toplevel, repo)
    name = project["tmux_session"]
    if _session_live(name) and not _session_owns_dir(name, toplevel):
        name = _dedupe_name(name)                          # live but foreign
        projects.update_project(toplevel, tmux_session=name)
    return name, project


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


def _discover_repos() -> set[str]:
    """Known-project repos + git repos one level under ~/dev (skipping
    hidden dirs + the worktrees container). Mirrors projects_discoverable."""
    repos: set[str] = set()
    for p in projects.all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"]))
    dev = Path.home() / "dev"
    if dev.is_dir():
        for child in dev.iterdir():
            if child.is_dir() and not child.name.startswith(".") \
               and child.name != "worktrees" and (child / ".git").exists():
                repos.add(str(child.resolve()))
    return repos


def _open_path(path: str) -> OpenResult:
    """Resolve a directory to a live, rail-placed tmux session.

    The shared implementation for PathTarget and PR/Branch targets so the
    path case is monkeypatchable independently of the descriptor dispatch.
    """
    toplevel = _git_toplevel(path)                       # ValueError if non-git
    repo = resolve_repo(toplevel)                        # --git-common-dir → parent
    project = ensure_project(toplevel, repo)
    session, claude_pid = ensure_session(project, toplevel)
    # Rebuild the full pane list from the now-live session. list_windows()
    # is a live shell-out, so freshly-stamped windows are visible
    # synchronously; pid_raw is the @periscope_id, "" for unmanaged.
    pane_pids = [w["pid_raw"] for w in list_windows()
                 if w["session"] == session and w["pid_raw"]]
    # The membership tag keys on the tmux pane_id (%N), but claude_pid is the
    # @periscope_id — scan for the claude window to recover its pane_id.
    claude_pane_id = next(
        (w["pane_id"] for w in list_windows()
         if w.get("pid_raw") == claude_pid and w.get("pane_id")),
        "",
    )
    ui = place_in_rail(session, project, pane_pids or [claude_pid])
    # Tag every pane in this session with its project context (the key the
    # session-match would resolve), so grouping/close work off metadata, not
    # the session. Uses resolve to get the canonical project key.
    from periscope import activity
    proj_key = projects.resolve_project_for_window({"session": session})
    if proj_key and proj_key != projects.MAIN_KEY:
        for w in list_windows():
            if w.get("session") == session and w.get("pane_id"):
                activity.set_pane_project(w["pane_id"], proj_key)
    return OpenResult(tmux_session=session, repo=repo, claude_pid=claude_pid,
                      claude_pane_id=claude_pane_id, ui=ui)


def open_target(descriptor: Descriptor) -> OpenResult:
    """Resolve a descriptor to a live, rail-placed tmux session.

    PathTarget  — git toplevel → ensure_project → ensure_session → place_in_rail.
    BranchTarget — locate or spawn the worktree, then open the path.
    PRTarget    — fetch PR into worktree, open the path, then stamp
                  linked_pr / is_fork (the path case has no PR knowledge).
                  Rolls back the worktree if the open fails after the fetch.
    """
    if isinstance(descriptor, PathTarget):
        return _open_path(descriptor.path)

    if isinstance(descriptor, BranchTarget):
        wt = worktree_for_branch(descriptor.repo, descriptor.branch)
        if wt is None:
            wt = spawn_worktree(descriptor.repo, descriptor.branch)["path"]
        return _open_path(wt)

    if isinstance(descriptor, PRTarget):
        prwt = projects.fetch_pr_into_worktree(descriptor.repo, descriptor.pr)
        try:
            result = _open_path(prwt.path)
        except Exception:
            # Any failure after the worktree exists must roll it back so the
            # caller can retry without hitting a stale orphan. Re-raise unchanged.
            projects._discard_pr_worktree(descriptor.repo, prwt.path, prwt.local_branch)
            raise
        store.set_window_fields(result.claude_pid, linked_pr=descriptor.pr,
                                is_fork=prwt.is_fork)
        return result

    raise ValueError(f"unknown descriptor: {descriptor!r}")


def build_catalog() -> dict:
    """GET /api/open/catalog payload: {repos:[...], worktrees:[...]}."""
    repos_out, worktrees_out = [], []
    for repo in sorted(_discover_repos()):
        code, out = _run(["git", "-C", repo, "branch", "--format=%(refname:short)"])
        branches = (out.split("\n")[:100] if (code == 0 and out) else [])
        repos_out.append({"repo": repo, "label": os.path.basename(repo),
                          "default_branch": detect_default_branch(repo),
                          "branches": branches})
        for path, branch in worktrees._cached_worktrees(repo):
            worktrees_out.append({
                "path": os.path.realpath(path), "repo": repo,
                "branch": branch,
                "is_main": os.path.realpath(path) == os.path.realpath(repo),
            })
    return {"repos": repos_out, "worktrees": worktrees_out}
