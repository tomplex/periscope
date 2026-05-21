"""Git + GitHub PR state, plus the activity-timeline cache.

Independent of any custom Claude statusline. We ask tmux for the pane's
current path, run git from there, and (if gh is installed) ask for the
PR + CI rollup attached to that branch. Results are cached because both
git status and gh queries cost real wall-clock time and the data changes
slowly compared to our polling cadence.

The activity timeline (for the modal sidebar) layers per-target "opened in
periscope" events on top of repo+branch-scoped commit and CI events; the
shared part is cached by (cwd, branch) since it's the same for every
window sitting on the same branch.

`prewarm_pr_cache` imports `list_windows` from periscope.panes — a one-way
upward dependency that's acknowledged in the Stage B split spec.
"""

import json
import os
import re
import shutil
import threading
import time

from periscope.gitutil import github_slug
from periscope.log import _bg
from periscope.panes import _acted_at, list_windows
from periscope.tmux import _run


_GIT_TTL = 15.0
_PR_TTL = 60.0
_git_cache: dict[str, tuple[float, dict | None]] = {}
_pr_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_pr_fetching: set[tuple[str, str]] = set()
_pr_lock = threading.Lock()
_GH_AVAILABLE = shutil.which("gh") is not None


def git_state_for(path: str) -> dict | None:
    """Return {branch, git} for the git repo at `path`, or None."""
    if not path or not os.path.isdir(path):
        return None
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return None
    _, branch = _run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        _, sha = _run(["git", "-C", path, "rev-parse", "--short", "HEAD"])
        branch = f"@{sha}" if sha else "?"
    # Compact diff stats vs HEAD (covers staged + unstaged together).
    _, diff = _run(["git", "-C", path, "diff", "HEAD", "--shortstat"])
    adds_m = re.search(r"(\d+) insertion", diff)
    dels_m = re.search(r"(\d+) deletion", diff)
    adds = int(adds_m.group(1)) if adds_m else 0
    dels = int(dels_m.group(1)) if dels_m else 0
    # Unpushed commits ahead of upstream.
    code, ahead_s = _run(["git", "-C", path, "rev-list", "--count", "@{u}..HEAD"])
    ahead = int(ahead_s) if code == 0 and ahead_s.isdigit() else 0
    state = "clean" if (adds == 0 and dels == 0) else f"+{adds} -{dels}"
    if ahead > 0:
        state += " *"
    # `repo_slug` (owner/repo) lets the frontend build PR URLs without
    # hardcoding a repo — periscope watches panes across many repos.
    return {"branch": branch, "git": state, "repo_slug": github_slug(path)}


def cached_git_state(path: str) -> dict | None:
    if not path:
        return None
    now = time.time()
    cached = _git_cache.get(path)
    if cached and now - cached[0] < _GIT_TTL:
        return cached[1]
    data = git_state_for(path)
    _git_cache[path] = (now, data)
    return data


def pr_state_for(path: str, branch: str) -> dict | None:
    """Return PR metadata + CI rollup for the PR open against `branch` in
    repo at `path`. Modal sidebar surfaces title/draft/+/−/reviewers; the
    grid card uses {pr, ci} as before."""
    if not _GH_AVAILABLE or not path or not branch:
        return None
    code, out = _run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json",
            "number,title,isDraft,additions,deletions,reviewRequests,statusCheckRollup",
            "--limit", "1",
        ],
        cwd=path,
        timeout=8.0,
    )
    if code != 0 or not out:
        return None
    try:
        prs = json.loads(out)
    except Exception:
        return None
    if not prs:
        return None
    pr = prs[0]
    rollup = pr.get("statusCheckRollup") or []
    states = {(c.get("conclusion") or c.get("status") or "").upper() for c in rollup}
    states.discard("")
    ci = None
    if states & {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        ci = "✗"
    elif states & {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}:
        ci = "⟳"
    elif states and states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        ci = "✓"
    # gh exposes requested reviewers as either users (with `login`) or teams
    # (with `name`) — take the login for users, name for teams, and trim to
    # the leading letters as the avatar text (2 chars max).
    reviewers: list[str] = []
    for r in pr.get("reviewRequests") or []:
        handle = r.get("login") or r.get("name") or ""
        if handle:
            reviewers.append(handle)
    return {
        "pr": pr.get("number"),
        "ci": ci,
        "pr_title": pr.get("title") or "",
        "pr_draft": bool(pr.get("isDraft")),
        "pr_additions": int(pr.get("additions") or 0),
        "pr_deletions": int(pr.get("deletions") or 0),
        "pr_reviewers": reviewers,
    }


def _fetch_pr_into_cache(path: str, branch: str) -> None:
    try:
        data = pr_state_for(path, branch)
    except Exception:
        data = None
    with _pr_lock:
        _pr_cache[(path, branch)] = (time.time(), data)
        _pr_fetching.discard((path, branch))


# --- Activity timeline (for modal sidebar) -------------------------------
#
# Per pane, surface a short timeline of recent events: commits on the repo
# in the last 24h, CI runs on the branch, and a single "opened in periscope"
# anchor sourced from _acted_at. Repo+branch events are cached by
# (cwd, branch) since they're the same for every window on the same branch;
# the per-target open event is layered in fresh on each call.

_ACTIVITY_TTL = 60.0
_activity_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_activity_fetching: set[tuple[str, str]] = set()
_activity_lock = threading.Lock()


def _gh_run_state(run: dict) -> str | None:
    """Map a gh run record to one of 'passed' / 'failed' / 'running', or
    None for runs we don't surface (skipped, neutral)."""
    s = (run.get("status") or "").upper()
    c = (run.get("conclusion") or "").upper()
    if c == "SUCCESS":
        return "passed"
    if c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
        return "failed"
    if c in ("NEUTRAL", "SKIPPED"):
        return None
    if s in ("QUEUED", "IN_PROGRESS", "WAITING"):
        return "running"
    return None


def shared_activity_for(path: str, branch: str) -> list[dict]:
    """Repo/branch-scoped events: commits in last 24h + CI runs on branch."""
    events: list[dict] = []
    if not path or not os.path.isdir(path):
        return events
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return events
    # %ct = committer date as unix seconds; %s = subject. Tab-separated so
    # subjects with spaces don't confuse the split.
    code, out = _run(
        ["git", "-C", path, "log", "-10", "--since=24h", "--pretty=format:%ct%x09%s"],
        timeout=3.0,
    )
    if code == 0 and out:
        for line in out.split("\n"):
            tab = line.find("\t")
            if tab < 0:
                continue
            try:
                at = int(line[:tab])
            except ValueError:
                continue
            subj = line[tab + 1 :].strip()
            if subj:
                events.append({"kind": "commit", "at": at, "text": subj})

    if _GH_AVAILABLE and branch:
        code, out = _run(
            [
                "gh", "run", "list",
                "--branch", branch,
                "--limit", "5",
                "--json", "conclusion,status,createdAt,displayTitle,name",
            ],
            cwd=path,
            timeout=5.0,
        )
        if code == 0 and out:
            try:
                runs = json.loads(out)
            except Exception:
                runs = []
            from datetime import datetime
            for run in runs:
                state = _gh_run_state(run)
                if state is None:
                    continue
                created = run.get("createdAt") or ""
                try:
                    # GitHub timestamps are RFC3339 with a trailing Z.
                    at = int(
                        datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    )
                except Exception:
                    continue
                name = run.get("displayTitle") or run.get("name") or "workflow"
                events.append(
                    {"kind": "ci", "at": at, "text": name, "state": state}
                )
    return events


def _fetch_activity_into_cache(path: str, branch: str) -> None:
    try:
        events = shared_activity_for(path, branch)
    except Exception:
        events = []
    with _activity_lock:
        _activity_cache[(path, branch)] = (time.time(), events)
        _activity_fetching.discard((path, branch))


def cached_pane_activity(target: str, path: str, branch: str | None) -> list[dict]:
    """Return up to 8 timeline events for this pane, newest-first. Shared
    (repo+branch) events come from a stale-while-revalidate cache; the
    per-target 'open' event is layered in fresh from _acted_at."""
    events: list[dict] = []
    if path and branch:
        key = (path, branch)
        now = time.time()
        with _activity_lock:
            cached = _activity_cache.get(key)
            stale = cached is None or (now - cached[0] >= _ACTIVITY_TTL)
            if stale and key not in _activity_fetching:
                _activity_fetching.add(key)
                _bg("activity-fetch", _fetch_activity_into_cache, path, branch)
            shared = cached[1] if cached else []
        events.extend(shared)

    opened_at = _acted_at.get(target, 0)
    if opened_at:
        events.append(
            {"kind": "open", "at": opened_at, "text": "opened in periscope"}
        )

    events.sort(key=lambda e: e.get("at", 0), reverse=True)
    return events[:8]


def cached_pr_state(path: str, branch: str | None) -> dict | None:
    """Stale-while-revalidate. Returns cached data instantly; kicks off a
    refresh in a background thread if the cache is missing or expired. The
    next poll picks up the fresh value."""
    if not branch:
        return None
    key = (path, branch)
    now = time.time()
    with _pr_lock:
        cached = _pr_cache.get(key)
        if cached and now - cached[0] < _PR_TTL:
            return cached[1]
        if key not in _pr_fetching:
            _pr_fetching.add(key)
            _bg("pr-fetch", _fetch_pr_into_cache, path, branch)
        return cached[1] if cached else None


def prewarm_pr_cache() -> None:
    """Walk every current tmux pane, resolve its branch via git, and kick off
    background gh PR queries for each unique (cwd, branch) pair. Runs once at
    startup so PR badges populate on the first /api/state poll instead of
    waiting for the second poll's stale-while-revalidate to fire them."""
    if not _GH_AVAILABLE:
        return
    try:
        windows = list_windows()
    except Exception:
        return
    pairs: set[tuple[str, str]] = set()
    for w in windows:
        cwd = w.get("cwd") or ""
        if not cwd:
            continue
        git = cached_git_state(cwd)
        if git and git.get("branch"):
            pairs.add((cwd, git["branch"]))
    for cwd, branch in pairs:
        # cached_pr_state spawns a daemon thread per (cwd, branch) miss.
        cached_pr_state(cwd, branch)
