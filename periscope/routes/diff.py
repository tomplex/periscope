"""GET /api/fs/diff — git-backed diff for a pane, in two scopes.

`scope=branch`  — everything this branch has done vs its fork point.
`scope=session` — everything since the current Claude session started.

See periscope.gitdiff for why this is git-backed rather than transcript-derived.
"""
from fastapi import APIRouter, HTTPException, Query

from periscope import activity, gitdiff
from periscope.tmux import tmux

router = APIRouter()


@router.get("/api/fs/diff")
def fs_diff(
    session: str,
    index: int,
    scope: str = Query("branch", pattern="^(branch|session)$"),
):
    target = f"{session}:{index}"
    # One fork for both fields — same pattern as turns.get_turns_for_pane.
    try:
        meta = tmux("display-message", "-t", target, "-p",
                    "#{pane_id}\t#{pane_current_path}").strip()
    except Exception:
        raise HTTPException(404, f"unknown pane: {target}") from None
    pane_id, _, cwd = meta.partition("\t")
    if not cwd:
        raise HTTPException(404, f"pane has no cwd: {target}")

    repo = gitdiff.repo_root(cwd)
    if not repo:
        raise HTTPException(400, f"not a git worktree: {cwd}")

    if scope == "branch":
        base = gitdiff.branch_base(repo)
        if not base:
            raise HTTPException(409, "could not resolve this branch's fork point")
    else:
        sid = activity.get_pane_session(pane_id)
        if not sid:
            raise HTTPException(
                409, "no Claude session recorded for this pane yet")
        rec = activity.get_session_base(sid)
        if not rec:
            raise HTTPException(
                409, "this session started before diff tracking — branch scope works")
        base_repo, base = rec
        # A pane that cd'd into a different repo mid-session has a baseline that
        # means nothing here; say so rather than rendering a bogus diff.
        if base_repo != repo:
            raise HTTPException(
                409, f"session baseline was taken in {base_repo}, not {repo}")

    out = gitdiff.diff_for(repo, base)
    out["scope"] = scope
    out["repo"] = repo
    return out
