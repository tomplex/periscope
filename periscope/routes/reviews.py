"""/api/reviews* — the review queue and the native walkthrough.

Periscope never mutates git or GitHub here (spec D1): prepare and merge are
channel messages to the pane that owns the branch; the only filesystem
writes are an append to comments.md, reviewed.json's per-stop marks, and the
explicit clear of a review dir.
All handlers are async because delivery goes through channels._deliver;
blocking git/file work runs in a thread so the event loop (every pane
mirror, every MCP connection) never stalls.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import channels, git_pr, gitdiff, open_ops, projects, reviews, state_hub, store, tracks
from periscope.log import log
from periscope.panes import list_windows

router = APIRouter()


def _windows() -> list[dict]:
    blob = state_hub._last_blob
    return json.loads(blob).get("windows", []) if blob else []


def _roots() -> dict[str, str]:
    """repo label → main-checkout path, for every repo periscope knows. The
    queue and the routes must find worktrees the same way, or the queue
    enables prepare on a row the click then 409s."""
    return {os.path.basename(r.rstrip("/")): r for r in open_ops.discover_repos()}


def _aliases(wins: list[dict], repo: str, branch: str) -> set[str]:
    return reviews.aliases_for(reviews._repo_root(wins, repo, _roots()), repo, branch)


def _check(repo: str, branch: str) -> None:
    """repo and branch become path segments under ~/.claude/reviews — refuse
    anything that could climb out of it."""
    # "." as a branch would make clear_review remove the whole repo dir.
    if (
        not repo
        or "/" in repo
        or repo in (".", "..")
        or not branch
        or branch.startswith("/")
        or any(seg in (".", "..") for seg in branch.split("/"))
    ):
        raise HTTPException(400, "bad repo/branch")


class ReviewRef(BaseModel):
    repo: str
    branch: str


class CommentBody(ReviewRef):
    stop: str
    body: str


class ReviewedBody(ReviewRef):
    stop: str
    sig: str
    reviewed: bool


# stopSig in static/src/review/walkthrough.js: 32-bit FNV-1a as 8 lowercase hex.
_SIG_RE = re.compile(r"[0-9a-f]{8}")


def _check_stop(stop: str) -> None:
    # A newline would split comments.md's heading line, and parse_comments
    # would drop the entry; reviewed.json keys follow the same rule.
    if not stop.strip() or "\n" in stop or "\r" in stop:
        raise HTTPException(400, "bad stop")
    # A lone surrogate round-trips through JSON but can't be written as UTF-8:
    # set_reviewed's write_text (ensure_ascii=False) would raise mid-write.
    try:
        stop.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(400, "bad stop") from None


@router.get("/api/reviews")
async def reviews_list():
    team_keys = store.linear_team_keys()

    def load():
        return reviews.list_reviews(_windows(), sorted(open_ops.discover_repos()), team_keys)  # a set

    rows = await asyncio.to_thread(load)
    return {"rows": rows}


@router.get("/api/reviews/walkthrough")
async def reviews_walkthrough(repo: str, branch: str):
    _check(repo, branch)

    def load():
        text = reviews._read(reviews.walkthrough_path(repo, branch))
        if not text:
            return None
        wins = _windows()
        worktree = reviews.worktree_for(wins, repo, branch, roots=_roots(), aliases=_aliases(wins, repo, branch))
        header = reviews.parse_header(text)
        diff = None
        if worktree:
            base = gitdiff.branch_base(worktree)
            diff = gitdiff.diff_for(worktree, base) if base else None
        if diff:
            # Generated files (the bundle, lockfiles) are megabytes of hunks no
            # stop cites; the entry stays so a ref to one reads "generated".
            for f in diff["files"]:
                if gitdiff.is_generated(f["path"]):
                    f["hunks"] = []
                    f["generated"] = True
        return {
            "text": text,
            "comments": reviews.parse_comments(reviews._read(reviews.comments_path(repo, branch))),
            "reviewed": reviews.read_reviewed(repo, branch),
            "head_now": reviews.head_sha(worktree) if worktree else None,
            "stale_by": reviews.stale_by(worktree, header["head"]) if header and worktree else None,
            "diff": diff,
        }

    body = await asyncio.to_thread(load)
    if body is None:
        raise HTTPException(404, f"no walkthrough for {repo} {branch}")
    return body


async def _deliver_or_409(owner: dict | None, message: str) -> None:
    if not owner:
        raise HTTPException(409, "not delivered: no owning pane on this branch")
    res = await channels._deliver(owner["pane_id"], message, "periscope")
    if not res.get("ok"):
        raise HTTPException(409, f"not delivered: {res.get('error')}")


@router.post("/api/reviews/comment")
async def reviews_comment(body: CommentBody):
    _check(body.repo, body.branch)
    _check_stop(body.stop)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
    # Append BEFORE delivery: a comment with no pane to hear it is still kept,
    # and the next pane to prepare the branch is told to read the file.
    await asyncio.to_thread(reviews.append_comment, body.repo, body.branch, body.stop, body.body, ts)
    wins = _windows()
    owner = reviews.owner_for(wins, body.repo, body.branch, _aliases(wins, body.repo, body.branch))
    msg = (
        f'Review comment on "{body.stop}" (branch {body.branch}; saved in '
        f"{reviews.comments_path(body.repo, body.branch)}):\n\n{body.body.strip()}"
    )
    await _deliver_or_409(owner, msg)
    return {"ok": True}


def _set_reviewed(repo: str, branch: str, stop: str, sig: str | None) -> None:
    # A dir can hold comments.md but no walkthrough: there's nothing to mark.
    # Checked in the same thread as the write so the stat never blocks the
    # event loop; raising here maps to the route's 404 same as a mid-request clear.
    if not reviews.walkthrough_path(repo, branch).is_file():
        raise FileNotFoundError
    reviews.set_reviewed(repo, branch, stop, sig)


@router.post("/api/reviews/reviewed")
async def reviews_reviewed(body: ReviewedBody):
    """Mark or unmark one stop reviewed (THI-443 D6). The sig ties the mark to
    the stop's text, so a re-prepare that rewrites the stop lapses it."""
    _check(body.repo, body.branch)
    _check_stop(body.stop)
    if not _SIG_RE.fullmatch(body.sig):
        raise HTTPException(400, "bad sig")
    sig = body.sig if body.reviewed else None
    try:
        await asyncio.to_thread(_set_reviewed, body.repo, body.branch, body.stop, sig)
    except FileNotFoundError:
        # No walkthrough, or one cleared mid-request; a mark never recreates it (THI-443 D6).
        raise HTTPException(404, f"no walkthrough for {body.repo} {body.branch}") from None
    return {"ok": True}


def _prepare_prompt(repo: str, branch: str) -> str:
    return (
        f"Run the `walkthrough` skill for branch `{branch}` and write it to "
        f"`{reviews.walkthrough_path(repo, branch)}`; read "
        f"`{reviews.comments_path(repo, branch)}` first if it exists."
    )


# state_hub._last_blob refreshes on the next tick, but a spawn awaits ~10 s: a
# second prepare in that window would see no owner and spawn a second Claude
# writing the same walkthrough.md.
_preparing: set[tuple[str, str]] = set()
# A spawned pane shows no owner until its channel attaches and the next poll
# lands (~10-20 s after the request returns); a prepare in that window is
# refused rather than spawning a twin. An attached owner clears the entry.
_recent_spawn: dict[tuple[str, str], float] = {}
RECENT_SPAWN_S = 60.0


@router.post("/api/reviews/prepare")
async def reviews_prepare(body: ReviewRef):
    _check(body.repo, body.branch)
    key = (body.repo, body.branch)
    if key in _preparing:
        raise HTTPException(409, "prepare already running")
    _preparing.add(key)
    try:
        return await _prepare(body)
    finally:
        _preparing.discard(key)


async def _prepare(body: ReviewRef) -> dict:
    wins = _windows()
    aliases = _aliases(wins, body.repo, body.branch)
    owner = reviews.owner_for(wins, body.repo, body.branch, aliases)
    prompt = _prepare_prompt(body.repo, body.branch)
    key = (body.repo, body.branch)
    if owner:
        _recent_spawn.pop(key, None)
        await _deliver_or_409(owner, prompt)
        return {"ok": True, "mode": "delivered"}
    age = time.monotonic() - _recent_spawn.get(key, -RECENT_SPAWN_S)
    if age < RECENT_SPAWN_S:
        raise HTTPException(409, f"a pane was spawned {int(age)} s ago and hasn't attached yet")
    roots = await asyncio.to_thread(_roots)
    worktree = await asyncio.to_thread(
        lambda: reviews.worktree_for(wins, body.repo, body.branch, fresh=True, roots=roots, aliases=aliases)
    )
    prwt = None
    number = None
    if not worktree:
        # A PR nobody has checked out: fetch it with ⌘K's PR helper (local
        # branch `pr-<N>`, which the queue already treats as this row's alias),
        # then spawn into it. Creating a worktree is the one git mutation
        # prepare makes; it still never commits, pushes or merges (spec D1).
        root = roots.get(body.repo)
        number = await asyncio.to_thread(_open_pr_number, root, body.repo, body.branch) if root else None
        if root is None or number is None:
            raise HTTPException(
                409, "no pane on this branch, no worktree on disk and no open PR — open the branch first"
            )
        prwt = await asyncio.to_thread(projects.fetch_pr_into_worktree, root, number)
        worktree = prwt.path
    checked_out = prwt is not None
    anchored = await asyncio.to_thread(open_ops.resolve_worktree_session, worktree)
    if not anchored:
        raise HTTPException(409, f"{worktree} is not inside a git repo")
    session, project = anchored
    try:
        spawned = await channels.spawn_claude_with_prompt(
            worktree, tracks.repo_default_track(project["repo"]), prompt, session=session
        )
    except channels.SpawnFailed as e:
        raise HTTPException(502, f"spawn failed: {e}") from e
    _recent_spawn[key] = time.monotonic()
    if not spawned["pid"]:
        # The Claude is already running with the prompt pasted; a 500 would invite a retry that spawns a second one.
        log.warning("reviews: spawned but pane id unknown for %s %s", body.repo, body.branch)
        return {"ok": True, "mode": "spawned", "checked_out": checked_out, **spawned, "pane_id": None}
    if prwt is not None:
        # What ⌘K's PR open stamps: the card shows the PR, merged/closed cleanup
        # fires, and a fork's worktree isn't flagged remote-gone on sight.
        store.set_window_fields(spawned["pid"], linked_pr=number, is_fork=prwt.is_fork)

    # Same server-side rail placement as the spawn_claude tool's anchored path.
    def place() -> None:
        pane_pids = [w["pid_raw"] for w in list_windows() if w["session"] == session and w.get("pid_raw")]
        open_ops.place_in_rail(session, project, pane_pids or [spawned["pid"]])

    try:
        await asyncio.to_thread(place)
    except Exception:
        # Placement only orders the rail; the Claude exists, and a 500 would invite a retry that spawns a second one.
        log.warning("reviews: rail placement failed for %s %s", body.repo, body.branch, exc_info=True)
    return {"ok": True, "mode": "spawned", "checked_out": checked_out, **spawned}


def _open_pr_number(root: str, repo: str, branch: str) -> int | None:
    """The open PR whose head is `branch`, from the same sources the queue row came from."""
    hit = next((p["number"] for p in reviews.cached_open_prs(root) if p["branch"] == branch), None)
    if hit is not None:
        return hit
    # A submitted review drops the PR from `review-requested:@me`; the queue then
    # builds the row from the walkthrough header and shows it open when GitHub
    # says so. Same answer here, or the row enables a prepare the route 409s.
    text = reviews._read(reviews.walkthrough_path(repo, branch))
    header = reviews.parse_header(text) if text else None
    if header and header["pr"] and (git_pr.cached_linked_pr_state(root, header["pr"]) or {}).get("pr_state") == "open":
        return header["pr"]
    return None


@router.post("/api/reviews/merge")
async def reviews_merge(body: ReviewRef):
    _check(body.repo, body.branch)
    wins = _windows()
    owner = reviews.owner_for(wins, body.repo, body.branch, _aliases(wins, body.repo, body.branch))
    msg = (
        f"This branch (`{body.branch}`) has been reviewed in periscope and the comments in "
        f"`{reviews.comments_path(body.repo, body.branch)}`. Read that file first and proceed only if "
        "every entry is `resolved: yes`: merge by this repo's convention, then report. Otherwise "
        "reply with what is still open and do not merge."
    )
    await _deliver_or_409(owner, msg)
    return {"ok": True}


@router.post("/api/reviews/clear")
async def reviews_clear(body: ReviewRef):
    _check(body.repo, body.branch)
    await asyncio.to_thread(reviews.clear_review, body.repo, body.branch)
    return {"ok": True}
