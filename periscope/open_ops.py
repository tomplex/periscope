"""Unified-open core: turn a target descriptor into a live, rail-placed
session. Plain functions over store.py singletons + tmux/git primitives —
no HTTP here (routes/open.py is the thin shim). `open` is a builtin; the
dispatch function is `open_target`, never `open`.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from periscope import config, projects, store, tracks, usage, worktrees
from periscope.gitutil import (
    detect_default_branch,
    github_slug,
    resolve_repo,
    resolve_repo_and_branch,
)
from periscope.panes import list_windows
from periscope.repo_locks import open_lock
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
    Authoritative source is `git worktree list`, read fresh — see
    `worktrees.for_branch` for why the 60s cache is the wrong answer to a
    create-or-reuse question."""
    return worktrees.for_branch(repo, branch)


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
    create/adopt routes' 409) — but check-then-insert, so callers hold
    `open_lock(toplevel)` (`_open_path`, `resolve_worktree_session`)."""
    existing = projects.all_projects().get(os.path.realpath(toplevel))
    if existing:
        return existing
    _, branch = resolve_repo_and_branch(toplevel)
    name = os.path.basename(toplevel)
    return projects.create_project(
        toplevel,
        name=name,
        tmux_session=name,
        repo=repo,
        base_branch=branch or None,
    )


def _session_live(name: str) -> bool:
    return _tmux_mutate("has-session", "-t", name)[0]  # socket-aware; False when missing


# How long the dir an open recorded on a window (`@periscope_opened`) stands in
# for its live cwd. A fresh pane's pane_current_path is its foreground
# process's, and while its shell runs its rc that can be a subshell elsewhere
# (~/.oh-my-zsh, or ""), so a repeat open in that window missed the pair the
# first had just made and spawned a second. Bounded because the agent window
# outlives the agent: quit claude, cd elsewhere, start another, and a record
# trusted forever would focus that pane on every later open of the first dir.
_OPENED_TRUST_S = 60


def _sits_at(w: dict, pinned_dir: str) -> bool:
    """True if window row `w` is at `pinned_dir` (realpath): by its live cwd,
    or, while it is fresh, by the dir the open that created it recorded."""
    # "" is unknown, not a dir: realpath("") is the server's own cwd, and prod
    # runs from the periscope checkout, so a pane of any project reading "" mid-rc
    # sat "at" it, and an open of that checkout focused or claimed it.
    cwd = w.get("cwd")
    if cwd and os.path.realpath(cwd) == pinned_dir:
        return True
    at, _, opened = (w.get("opened") or "").partition(" ")
    return (
        bool(opened)
        and at.isdigit()
        and time.time() - int(at) < _OPENED_TRUST_S
        and os.path.realpath(opened) == pinned_dir
    )


def _session_owns_dir(name: str, pinned_dir: str) -> bool:
    """True if any window of session `name` sits at `pinned_dir` (`_sits_at`)."""
    return any(w["session"] == name and _sits_at(w, pinned_dir) for w in list_windows())


def _agent_pid_for_dir(session: str, pinned_dir: str, agent: str) -> str:
    """The @periscope_id (pid_raw) of the `agent` window in `session` that sits
    at `pinned_dir` (`_sits_at`). Under one shared session many claude windows
    coexist, so a session-wide "first claude" match is wrong here — filter by
    dir. '' if no window there is the agent's, or it isn't stamped yet."""
    wins = [w for w in list_windows() if w["session"] == session and _sits_at(w, pinned_dir)]
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


def place_in_rail(tmux_session: str, project: projects.Project, pane_pids: list[str]) -> dict:
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
    store.update_ui({"track_order": order, "tabs_by_track": tabs}, writer="place_in_rail")
    return store.get_ui()


def place_session_in_rail(session: str, project: projects.Project, pid: str) -> dict:
    """`place_in_rail` with every stamped pane of `session`, or `pid` alone when none is.
    list_windows() is a live listing, so a window stamped a moment ago is in it."""
    pane_pids = [w["pid_raw"] for w in list_windows() if w["session"] == session and w["pid_raw"]]
    return place_in_rail(session, project, pane_pids or [pid])


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
    # The same lock as _open_path: MCP spawn_claude (workspace="new", every
    # commander spawn) registers here too, and a spawn and a threadpool open
    # (⌘K's POST /api/open, /api/workspaces/spawn) of one dir both passed
    # ensure_project's check, so the loser's insert raised "project already
    # exists" (a 400 when the loser was the open). Two spawns can't race here:
    # MCP handlers run one at a time on the event loop. Safe to
    # take: no caller holds a lock (spawn_claude's worktree create has dropped
    # repo_lock by now), and the body only shells out and takes _STATE_LOCK.
    with open_lock(toplevel):
        project = ensure_project(toplevel, repo)
        name = project["tmux_session"]
        if _session_live(name) and not _session_owns_dir(name, toplevel):
            name = _dedupe_name(name)  # live but foreign
            projects.update_project(toplevel, tmux_session=name)
    return name, project


def ensure_session(
    project: projects.Project,
    pinned_dir: str,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    """Idempotent create-or-focus into the single shared MANAGED_SESSION.
    `pinned_dir` is the project's key (taken explicitly — Project is a
    TypedDict with no self-key). Returns (tmux_session, agent_pid,
    created_pids).

    "Already open?" is answered by cwd ownership WITHIN the shared session
    (every project's panes live there now) — not by a session named after the
    project, which no longer exists. So there's no foreign-name dedupe here.

    `created_pids` are the panes this call made (empty on the focus path): the
    caller tags them by pid, because a fresh pane's live cwd can't be trusted
    yet (see `_open_path`).

    Check-then-create, so not safe to race: hold `open_lock(pinned_dir)`
    across it, as `_open_path` does."""
    session = config.MANAGED_SESSION
    if _session_live(session) and _session_owns_dir(session, pinned_dir):
        existing = _agent_pid_for_dir(session, pinned_dir, agent)
        if existing:
            return session, existing, ()
    if agent == "claude":
        # No account named → launch_policy decides. This is periscope's
        # primary launch path (⌘K omnibox, POST /api/open, PR review); pinning
        # it to the default account would send every open to the subscription
        # that fills up first. Codex is excluded below: it has no Claude
        # subscription to choose between. choose_launch degrades to "default"
        # when usage is unknown, so an open never waits on it.
        launch = usage.choose_launch(account)
        agent_pid, shell_pid = _layout_two_window(
            session,
            pinned_dir,
            account=launch.account,
            model=launch.model,
            effort=launch.effort,
        )
    else:
        agent_pid, shell_pid = _layout_two_window(session, pinned_dir, agent=agent, account=account)
    return session, agent_pid, tuple(p for p in (agent_pid, shell_pid) if p)


def discover_repos() -> set[str]:
    """Known-project repos + git repos one level under each settings.repo_roots
    entry (skipping hidden dirs + the worktrees container)."""
    repos: set[str] = set()
    for p in projects.all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"] or ""))
    for root in store.repo_roots():
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for child in root_path.iterdir():
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "worktrees"
                and (child / ".git").exists()
            ):
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
    # The omnibox hands over whatever was typed, so `~` arrives unexpanded —
    # git -C takes it literally and fails. Expand here rather than in the route:
    # this is the single choke point every descriptor variant funnels through.
    path = os.path.expanduser(path)
    # Check existence separately from the git check. Without this a typo falls
    # through to _git_toplevel and reports "not inside a git repo", which sends
    # you looking for the wrong problem.
    if not os.path.isdir(path):
        raise ValueError(f"no such directory: {path}")
    toplevel = _git_toplevel(path)  # ValueError if non-git
    repo = resolve_repo(toplevel)  # --git-common-dir → parent
    # Check-then-create under a per-dir lock. A double submit is two POSTs on
    # two threadpool threads, and the MCP `open` tool races ⌘K the same way;
    # the agent window gets its @periscope_id only as _layout_two_window's last
    # step, so a second check landing mid-layout saw no agent here and built a
    # second pair. ensure_project is inside too: two first opens of a dir both
    # inserted, and the loser's "project already exists" surfaced as a 400
    # (resolve_worktree_session takes the same lock for spawn_claude's insert).
    with open_lock(toplevel):
        project = ensure_project(toplevel, repo)
        session, agent_pid, created = ensure_session(project, toplevel, agent)
    # Recover the claude window's tmux pane_id (%N) for the result payload —
    # the terminal WS targets panes by %N even though identity keys on the pid.
    agent_pane_id = next(
        (w["pane_id"] for w in list_windows() if w.get("pid_raw") == agent_pid and w.get("pane_id")),
        "",
    )
    ui = place_session_in_rail(session, project, agent_pid)
    # Tag THIS open's panes into the repo's default track so grouping works off
    # track metadata (the rail groups purely by track_id). Scope is panes at
    # `toplevel` with no existing tag: the shared MANAGED_SESSION holds every
    # project's panes, so a session-wide re-tag moves the whole rail into this
    # track (the sts2-seed-finder clobber); an existing tag is either already
    # right or a user's goal-track move — never overwrite it here.
    #
    # The panes this open just created are tagged by pid, NOT by cwd: a fresh
    # pane's pane_current_path is its FOREGROUND process's cwd, and a shell
    # still in its rc can have a subshell elsewhere (oh-my-zsh's `cd $ZSH`
    # update check reads as ~/.oh-my-zsh ~60ms in, sometimes as ""). This loop
    # runs inside that window, so a cwd test intermittently left the new
    # shell — or, under load, the agent pane — untagged.
    from periscope import activity, tracks

    tid = tracks.repo_default_track(repo)
    for w in list_windows():
        # Membership keys on the stamped @periscope_id (pid_raw on raw rows);
        # unstamped windows have no durable identity to tag yet — skip them.
        p = (w.get("pid_raw") or "").strip()
        if (
            p
            and w.get("session") == session
            and (p in created or _sits_at(w, toplevel))
            and activity.get_pane_track(p) is None
        ):
            tracks.move_pane(p, tid)
    return OpenResult(
        tmux_session=session, repo=repo, agent_pid=agent_pid, agent_pane_id=agent_pane_id, agent=agent, ui=ui
    )


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
        return _open_path(descriptor.path) if agent == "claude" else _open_path(descriptor.path, agent)

    if isinstance(descriptor, BranchTarget):
        wt = worktree_for_branch(descriptor.repo, descriptor.branch)
        if wt is None:
            wt = spawn_worktree(descriptor.repo, descriptor.branch)["path"]
        return _open_path(wt) if agent == "claude" else _open_path(wt, agent)

    if isinstance(descriptor, PRTarget):
        prwt = projects.fetch_pr_into_worktree(descriptor.repo, descriptor.pr)
        try:
            result = _open_path(prwt.path) if agent == "claude" else _open_path(prwt.path, agent)
        except Exception:
            # Any failure after the worktree exists must roll it back so the
            # caller can retry without hitting a stale orphan. Re-raise unchanged.
            projects._discard_pr_worktree(descriptor.repo, prwt.path, prwt.local_branch)
            raise
        store.set_window_fields(result.agent_pid, linked_pr=descriptor.pr, is_fork=prwt.is_fork)
        return result

    raise ValueError(f"unknown descriptor: {descriptor!r}")


def build_catalog() -> dict:
    """GET /api/open/catalog payload: {repos:[{repo, label, default_branch, branches, slug}], worktrees:[...], roots:[...]}.
    `roots` is the expanded scan list, so an empty catalog can say where it
    looked."""
    repos_out, worktrees_out = [], []
    for repo in sorted(discover_repos()):
        # Sorted by commit recency, NOT alphabetically: the launcher shows only
        # the first handful of branches and hides the rest behind a search, so
        # the order decides what's reachable without typing. `git branch` sorts
        # by refname, which made the 100-cap keep the alphabetically-first 100 —
        # on a repo with 130 branches that silently dropped the ones actually
        # being worked on.
        code, out = _run(
            [
                "git",
                "-C",
                repo,
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/heads/",
            ]
        )
        branches = out.split("\n")[:100] if (code == 0 and out) else []
        repos_out.append(
            {
                "repo": repo,
                "label": os.path.basename(repo),
                "default_branch": detect_default_branch(repo),
                "branches": branches,
                # `owner/name` of origin on GitHub, or None: another app finds a pull request's repo by it.
                "slug": github_slug(repo),
            }
        )
        for path, branch in worktrees._cached_worktrees(repo):
            worktrees_out.append(
                {
                    "path": os.path.realpath(path),
                    "repo": repo,
                    "branch": branch,
                    "is_main": os.path.realpath(path) == os.path.realpath(repo),
                }
            )
    return {"repos": repos_out, "worktrees": worktrees_out, "roots": store.repo_roots()}
