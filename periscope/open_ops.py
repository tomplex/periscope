"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from periscope import config, projects, store, tracks, worktrees
from periscope.gitutil import (
    detect_default_branch,
    resolve_repo,
    resolve_repo_and_branch,
)
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
    agent_pid: str
    agent_pane_id: str
    agent: Literal["claude", "codex"]
    ui: dict

    @property
    def claude_pid(self) -> str:
        """Internal compatibility for callers migrating to agent_pid."""
        return self.agent_pid

    @property
    def claude_pane_id(self) -> str:
        return self.agent_pane_id


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


def _agent_pid_for_dir(session: str, pinned_dir: str, agent: str) -> str:
    """The @periscope_id (pid_raw) of the claude window in `session` whose cwd
    is `pinned_dir`. Under one shared session many claude windows coexist, so
    a session-wide "first claude" match is wrong here — filter by realpath'd
    cwd. Falls back to the first window at `pinned_dir`. '' if none."""
    wins = [w for w in list_windows()
            if w["session"] == session
            and os.path.realpath(w.get("cwd") or "") == pinned_dir]
    if not wins:
        return ""
    match = next(
        (w for w in wins if w.get("agent") == agent or w.get("name") == agent),
        None,
    )
    return (match or {}).get("pid_raw") or ""


def _dedupe_name(base: str) -> str:
    n, candidate = 2, f"{base}-2"
    while _session_live(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def place_in_rail(tmux_session: str, project: projects.Project,
                  pane_pids: list[str]) -> dict:
    """Append the opened panes to the rail prefs (idempotent) and return the
    full ui blob for the client to write into prefsSignal.

    Keys are the TRACK-era ones. The pre-tracks trio (`repo_order` /
    `worktrees_by_repo` / `panes_by_worktree`) is no longer read by the rail:
    `prefs.js` drops `worktrees_by_repo` outright, never reads
    `panes_by_worktree`, and honours `repo_order` only as the initial fallback
    when `track_order` is unset — so writing them persisted nothing once the
    rail had saved an order even once.

    Placement is ordering only, not visibility: `mergeLiveAndPrefs` already
    appends live-new tracks and tabs on the next poll. This makes the order
    the user sees survive, rather than being silently re-derived.
    """
    track_id = tracks.repo_default_track(project["repo"])
    ui = store.get_ui()
    order = list(ui.get("track_order", []))
    if track_id not in order:
        order.append(track_id)
    tabs = {k: list(v) for k, v in ui.get("tabs_by_track", {}).items()}
    tab_list = tabs.setdefault(track_id, [])
    for pid in pane_pids:
        if pid not in tab_list:
            tab_list.append(pid)
    store.update_ui({"track_order": order, "tabs_by_track": tabs})
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


def ensure_session(
    project: projects.Project,
    pinned_dir: str,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
) -> tuple[str, str]:
    """Idempotent create-or-focus into the single shared MANAGED_SESSION.
    `pinned_dir` is the project's key (taken explicitly — Project is a
    TypedDict with no self-key). Returns (tmux_session, claude_pid).

    "Already open?" is answered by cwd ownership WITHIN the shared session
    (every project's panes live there now) — not by a session named after the
    project, which no longer exists. So there's no foreign-name dedupe here."""
    session = config.MANAGED_SESSION
    if _session_live(session) and _session_owns_dir(session, pinned_dir):
        existing = _agent_pid_for_dir(session, pinned_dir, agent)
        if existing:
            return session, existing
    if agent == "claude":
        agent_pid, _ = _layout_two_window(session, pinned_dir, account=account)
    else:
        agent_pid, _ = _layout_two_window(
            session, pinned_dir, agent=agent, account=account
        )
    return session, agent_pid


def _discover_repos() -> set[str]:
    """Known-project repos + git repos one level under ~/dev (skipping
    hidden dirs + the worktrees container). Mirrors projects_discoverable."""
    repos: set[str] = set()
    for p in projects.all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"] or ""))
    dev = Path.home() / "dev"
    if dev.is_dir():
        for child in dev.iterdir():
            if child.is_dir() and not child.name.startswith(".") \
               and child.name != "worktrees" and (child / ".git").exists():
                repos.add(str(child.resolve()))
    return repos


def _open_path(
    path: str,
    agent: Literal["claude", "codex"] = "claude",
) -> OpenResult:
    """Resolve a directory to a live, rail-placed tmux session.

    The shared implementation for PathTarget and PR/Branch targets so the
    path case is monkeypatchable independently of the descriptor dispatch.
    """
    toplevel = _git_toplevel(path)                       # ValueError if non-git
    repo = resolve_repo(toplevel)                        # --git-common-dir → parent
    project = ensure_project(toplevel, repo)
    session, agent_pid = ensure_session(project, toplevel, agent)
    # Rebuild the full pane list from the now-live session. list_windows()
    # is a live shell-out, so freshly-stamped windows are visible
    # synchronously; pid_raw is the @periscope_id, "" for unmanaged.
    pane_pids = [w["pid_raw"] for w in list_windows()
                 if w["session"] == session and w["pid_raw"]]
    # The membership tag keys on the tmux pane_id (%N), but claude_pid is the
    # @periscope_id — scan for the claude window to recover its pane_id.
    agent_pane_id = next(
        (w["pane_id"] for w in list_windows()
         if w.get("pid_raw") == agent_pid and w.get("pane_id")),
        "",
    )
    ui = place_in_rail(session, project, pane_pids or [agent_pid])
    # Tag THIS open's panes into the repo's default track so grouping works off
    # track metadata (the rail groups purely by track_id). Scope is panes at
    # `toplevel` with no existing tag: the shared MANAGED_SESSION holds every
    # project's panes, so a session-wide re-tag moves the whole rail into this
    # track (the sts2-seed-finder clobber); an existing tag is either already
    # right or a user's goal-track move — never overwrite it here.
    from periscope import activity, tracks
    tid = tracks.repo_default_track(repo)
    for w in list_windows():
        if (w.get("session") == session and w.get("pane_id")
                and os.path.realpath(w.get("cwd") or "") == toplevel
                and activity.get_pane_track(w["pane_id"]) is None):
            tracks.move_pane(w["pane_id"], tid)
    return OpenResult(tmux_session=session, repo=repo, agent_pid=agent_pid,
                      agent_pane_id=agent_pane_id, agent=agent, ui=ui)


def open_target(
    descriptor: Descriptor,
    agent: Literal["claude", "codex"] = "claude",
) -> OpenResult:
    """Resolve a descriptor to a live, rail-placed tmux session.

    PathTarget  — git toplevel → ensure_project → ensure_session → place_in_rail.
    BranchTarget — locate or spawn the worktree, then open the path.
    PRTarget    — fetch PR into worktree, open the path, then stamp
                  linked_pr / is_fork (the path case has no PR knowledge).
                  Rolls back the worktree if the open fails after the fetch.
    """
    if isinstance(descriptor, PathTarget):
        return _open_path(descriptor.path) if agent == "claude" else _open_path(
            descriptor.path, agent
        )

    if isinstance(descriptor, BranchTarget):
        wt = worktree_for_branch(descriptor.repo, descriptor.branch)
        if wt is None:
            wt = spawn_worktree(descriptor.repo, descriptor.branch)["path"]
        return _open_path(wt) if agent == "claude" else _open_path(wt, agent)

    if isinstance(descriptor, PRTarget):
        prwt = projects.fetch_pr_into_worktree(descriptor.repo, descriptor.pr)
        try:
            result = _open_path(prwt.path) if agent == "claude" else _open_path(
                prwt.path, agent
            )
        except Exception:
            # Any failure after the worktree exists must roll it back so the
            # caller can retry without hitting a stale orphan. Re-raise unchanged.
            projects._discard_pr_worktree(descriptor.repo, prwt.path, prwt.local_branch)
            raise
        store.set_window_fields(result.agent_pid, linked_pr=descriptor.pr,
                                is_fork=prwt.is_fork)
        return result

    raise ValueError(f"unknown descriptor: {descriptor!r}")


def build_catalog() -> dict:
    """GET /api/open/catalog payload: {repos:[...], worktrees:[...]}."""
    repos_out, worktrees_out = [], []
    for repo in sorted(_discover_repos()):
        # Sorted by commit recency, NOT alphabetically: the launcher shows only
        # the first handful of branches and hides the rest behind a search, so
        # the order decides what's reachable without typing. `git branch` sorts
        # by refname, which made the 100-cap keep the alphabetically-first 100 —
        # on a repo with 130 branches that silently dropped the ones actually
        # being worked on.
        code, out = _run([
            "git", "-C", repo, "for-each-ref", "--sort=-committerdate",
            "--format=%(refname:short)", "refs/heads/",
        ])
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
