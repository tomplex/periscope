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

from periscope import config
from periscope.gitutil import github_slug
from periscope.log import _bg
from periscope.panes import list_windows
from periscope.tmux import _run

_GIT_TTL = 15.0
_PR_TTL = 60.0
_git_cache: dict[str, tuple[float, dict | None]] = {}
_pr_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_pr_fetching: set[tuple[str, str]] = set()
_pr_lock = threading.Lock()

# Explicitly-linked PRs (via the link_pr MCP tool) are keyed by number, not
# branch: a linked PR may be merged or on a different branch than the pane's
# current one, so pr_state_for's `--state open --head <branch>` query can't see
# it. Separate number-keyed SWR cache resolves its lifecycle state + CI.
_LINKED_PR_TTL = 60.0
_linked_pr_cache: dict[tuple[str, int], tuple[float, dict | None]] = {}
_linked_pr_fetching: set[tuple[str, int]] = set()
_GH_AVAILABLE = shutil.which("gh") is not None

# GitHub check conclusions that mean "failed". Shared by the PR-rollup glyph
# (pr_state_for) and the activity-timeline run state (_gh_run_state) so a new
# failure conclusion is added in one place. The "running" and "success/neutral"
# buckets intentionally differ between those two callers and are NOT shared.
_CI_FAILED_CONCLUSIONS = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"})


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
    # Untracked files. `git diff HEAD` ignores them entirely, so a worktree
    # whose only change is brand-new files reported "clean" — and isDirty()
    # then suppressed the chip, so nothing surfaced at all. --directory
    # collapses a wholly-untracked dir to one entry instead of walking it.
    _, untracked = _run(["git", "-C", path, "ls-files", "-o", "--exclude-standard",
                         "--directory", "--no-empty-directory"])
    new = sum(1 for line in untracked.splitlines() if line.strip())
    # Unpushed commits ahead of upstream.
    code, ahead_s = _run(["git", "-C", path, "rev-list", "--count", "@{u}..HEAD"])
    ahead = int(ahead_s) if code == 0 and ahead_s.isdigit() else 0
    bits = []
    if adds or dels:
        bits.append(f"+{adds} -{dels}")
    if new:
        bits.append(f"?{new}")
    state = " ".join(bits) if bits else "clean"
    if ahead > 0:
        state += " *"
    # `repo_slug` (owner/repo) lets the frontend build PR URLs without
    # hardcoding a repo — periscope watches panes across many repos.
    # repo_key is the full repo path (handles both sibling and inline
    # worktree layouts via gitutil.resolve_repo). repo_label is the
    # basename for human-readable display in the rail.
    from periscope.gitutil import resolve_repo
    repo_key = resolve_repo(path)
    repo_label = os.path.basename(repo_key.rstrip("/")) if repo_key else ""
    return {
        "branch": branch,
        "git": state,
        "repo_slug": github_slug(path),
        "repo_key": repo_key,
        "repo_label": repo_label,
    }


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


def _ci_glyph(rollup: list | None) -> str | None:
    """Roll a PR's statusCheckRollup up to a single glyph: ✗ if anything failed,
    ⟳ if anything's still running, ✓ if everything landed clean, else None."""
    states = {(c.get("conclusion") or c.get("status") or "").upper()
              for c in (rollup or [])}
    states.discard("")
    if states & _CI_FAILED_CONCLUSIONS:
        return "✗"
    if states & {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}:
        return "⟳"
    if states and states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "✓"
    return None


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
    ci = _ci_glyph(pr.get("statusCheckRollup"))
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
# Per repo+branch, surface recent commits and CI runs. The merge with
# persisted events and the per-target "opened in periscope" anchor lives in
# activity.py, which holds the read-path cache; this module only computes
# the git/gh half via shared_activity_for.


def _gh_run_state(run: dict) -> str | None:
    """Map a gh run record to one of 'passed' / 'failed' / 'running', or
    None for runs we don't surface (skipped, neutral)."""
    s = (run.get("status") or "").upper()
    c = (run.get("conclusion") or "").upper()
    if c == "SUCCESS":
        return "passed"
    if c in _CI_FAILED_CONCLUSIONS:
        return "failed"
    if c in ("NEUTRAL", "SKIPPED"):
        return None
    if s in ("QUEUED", "IN_PROGRESS", "WAITING"):
        return "running"
    return None


def shared_activity_for(path: str, branch: str) -> list[dict]:
    """Repo/branch-scoped events: commits within the ACTIVITY_DAYS window
    + CI runs on the branch. Commit and CI events carry a `url`."""
    events: list[dict] = []
    if not path or not os.path.isdir(path):
        return events
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return events
    slug = github_slug(path)
    # %ct = committer unix time, %H = full sha, %s = subject. Tab-separated
    # so subjects with spaces survive the split.
    code, out = _run(
        ["git", "-C", path, "log", "-20",
         f"--since={config.ACTIVITY_DAYS}d",
         "--pretty=format:%ct%x09%H%x09%s"],
        timeout=3.0,
    )
    if code == 0 and out:
        for line in out.split("\n"):
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                at = int(parts[0])
            except ValueError:
                continue
            sha, subj = parts[1], parts[2].strip()
            if not subj:
                continue
            ev = {"kind": "commit", "at": at, "text": subj}
            if slug:
                ev["url"] = f"https://github.com/{slug}/commit/{sha}"
            events.append(ev)

    if _GH_AVAILABLE and branch:
        code, out = _run(
            ["gh", "run", "list", "--branch", branch, "--limit", "10",
             "--json", "conclusion,status,createdAt,displayTitle,name,url"],
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
                    at = int(
                        datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    )
                except Exception:
                    continue
                name = run.get("displayTitle") or run.get("name") or "workflow"
                ev = {"kind": "ci", "at": at, "text": name, "state": state}
                if run.get("url"):
                    ev["url"] = run["url"]
                events.append(ev)
    return events


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


def linked_pr_state_for(path: str, number: int) -> dict | None:
    """Resolve an explicitly-linked PR's lifecycle state + CI by number.
    Returns {pr_state: 'open'|'merged'|'closed', ci: glyph|None} or None if gh
    can't answer. Distinct from pr_state_for, which only sees OPEN PRs on the
    current branch."""
    if not _GH_AVAILABLE or not path or not number:
        return None
    code, out = _run(
        ["gh", "pr", "view", str(number), "--json", "state,statusCheckRollup"],
        cwd=path,
        timeout=8.0,
    )
    if code != 0 or not out:
        return None
    try:
        pr = json.loads(out)
    except Exception:
        return None
    return {
        "pr_state": (pr.get("state") or "").lower() or None,  # open/merged/closed
        "ci": _ci_glyph(pr.get("statusCheckRollup")),
    }


def _fetch_linked_pr_into_cache(path: str, number: int) -> None:
    try:
        data = linked_pr_state_for(path, number)
    except Exception:
        data = None
    with _pr_lock:
        _linked_pr_cache[(path, number)] = (time.time(), data)
        _linked_pr_fetching.discard((path, number))


def cached_linked_pr_state(path: str, number: int | None) -> dict | None:
    """SWR for a linked PR's state, keyed by number (mirrors cached_pr_state)."""
    if not path or not number:
        return None
    key = (path, int(number))
    now = time.time()
    with _pr_lock:
        cached = _linked_pr_cache.get(key)
        if cached and now - cached[0] < _LINKED_PR_TTL:
            return cached[1]
        if key not in _linked_pr_fetching:
            _linked_pr_fetching.add(key)
            _bg("linked-pr-fetch", _fetch_linked_pr_into_cache, *key)
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
