"""The review walkthrough convention: files under ~/.claude/reviews that the
`walkthrough` skill writes and periscope renders. Periscope only ever appends
to comments.md, writes reviewed.json (per-stop reviewed marks, THI-443) and
removes a whole review dir on an explicit clear — the pane is the sole writer
of walkthrough.md (spec D2).

Two parsers read this format: this line-based one (header, stop count,
comments — cheap enough to run per queue refresh) and the frontend's mdast one
(the view). They agree on ATX headings and plain-text metadata blockquotes,
which is all the skill emits.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import TypedDict

from periscope import git_pr, gitutil, worktrees
from periscope.log import _bg
from periscope.tmux import _run

REVIEWS_DIR = Path(os.path.expanduser("~/.claude/reviews"))

# A metadata pair: lowercase dashed key, a space, any value. Anything with
# markup (bold, links) fails this, which is the "plain text only" rule
# without a markdown parser.
_PAIR_RE = re.compile(r"^([a-z-]+): (.+)$")
_COMMENT_HEAD_RE = re.compile(r"^## (\d{4}-\d\d-\d\dT\d\d:\d\d) · (.+)$")
_RESOLVED_RE = re.compile(r"^> resolved: (yes|no)$", re.IGNORECASE)


class Header(TypedDict):
    branch: str
    head: str
    pr: int | None
    repo: str | None


class Comment(TypedDict):
    ts: str
    stop: str
    resolved: bool
    body: str


# --- paths -----------------------------------------------------------------


def slug_for(branch: str) -> str:
    return branch.replace("/", "--")


def review_dir(repo: str, branch: str) -> Path:
    return REVIEWS_DIR / repo / slug_for(branch)


def walkthrough_path(repo: str, branch: str) -> Path:
    return review_dir(repo, branch) / "walkthrough.md"


def comments_path(repo: str, branch: str) -> Path:
    return review_dir(repo, branch) / "comments.md"


def reviewed_path(repo: str, branch: str) -> Path:
    return review_dir(repo, branch) / "reviewed.json"


# --- parse -----------------------------------------------------------------


def meta_pairs(lines: list[str]) -> dict[str, str] | None:
    """The D3 pair rule over the stripped lines of one blockquote: every
    ` · `- or newline-separated piece must be `key: value`. None means it is
    an ordinary blockquote."""
    out: dict[str, str] = {}
    for line in lines:
        for piece in line.split(" · "):
            m = _PAIR_RE.match(piece.strip())
            if not m:
                return None
            out[m.group(1)] = m.group(2).strip()
    return out or None


def _quote_after_title(text: str) -> list[str]:
    """Stripped lines of the blockquote directly after the first `# ` line."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    quote: list[str] = []
    while i < len(lines) and lines[i].startswith(">"):
        quote.append(lines[i][1:].strip())
        i += 1
    return quote


def parse_header(text: str) -> Header | None:
    pairs = meta_pairs(_quote_after_title(text))
    if not pairs or "branch" not in pairs or "head" not in pairs:
        return None
    pr = pairs.get("pr")
    return {
        "branch": pairs["branch"],
        "head": pairs["head"],
        "pr": int(pr) if pr and re.fullmatch(r"\d+", pr, re.ASCII) else None,
        "repo": pairs.get("repo"),
    }


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_STOP_RE = re.compile(r"^ {0,3}##(?:\s|$)")


def count_stops(text: str) -> int:
    """ATX `##` headings outside fenced code, by CommonMark's rules for those two
    constructs — the view counts mdast depth-2 headings, and the queue's "N stops"
    must agree with it. Known gaps (the skill never emits them): a setext `---`
    heading, a `##` nested under a list item, and a `##` inside an HTML block or
    comment all count differently here."""
    n = 0
    fence: tuple[str, int] | None = None  # (fence char, opener length)
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if fence:
            # A closer is the same char, at least as long, with nothing after it.
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            continue
        # A backtick opener's info string may not contain a backtick: ```foo``` is inline code, not a fence.
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if _STOP_RE.match(line):
            n += 1
    return n


def parse_comments(text: str) -> list[Comment]:
    """An entry starts at `## <ts> · <stop>` followed by `> resolved: yes|no`.
    A body line that merely starts with `## ` stays body."""
    lines = text.splitlines()
    out: list[Comment] = []
    i = 0
    while i < len(lines):
        m = _COMMENT_HEAD_RE.match(lines[i].rstrip())
        r = _RESOLVED_RE.match(lines[i + 1].strip()) if m and i + 1 < len(lines) else None
        if not (m and r):
            i += 1
            continue
        body: list[str] = []
        j = i + 2
        while j < len(lines):
            m2 = _COMMENT_HEAD_RE.match(lines[j].rstrip())
            if m2 and j + 1 < len(lines) and _RESOLVED_RE.match(lines[j + 1].strip()):
                break
            body.append(lines[j])
            j += 1
        out.append(
            {
                "ts": m.group(1),
                "stop": m.group(2),
                "resolved": r.group(1).lower() == "yes",
                "body": "\n".join(body).strip(),
            }
        )
        i = j
    return out


# --- files -----------------------------------------------------------------


def append_comment(repo: str, branch: str, stop: str, body: str, ts: str) -> None:
    # ts must be minute-precision %Y-%m-%dT%H:%M: _COMMENT_HEAD_RE only parses that, so seconds would write an unparseable entry.
    path = comments_path(repo, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"## {ts} · {stop}\n> resolved: no\n\n{body.strip()}\n\n")


# reviewed.json is a read-modify-write, and the dev and prod servers both write
# ~/.claude/reviews (it isn't instance_file()-separated): the flock serializes
# writers across processes and threads (flock is per open file description,
# not per process); the module lock is belt-and-braces (spec D6).
_reviewed_lock = threading.Lock()


def read_reviewed(repo: str, branch: str) -> dict[str, str]:
    """stop title → the stop's sig when it was marked (THI-443 D6). A missing,
    unreadable or hand-mangled file reads as nothing reviewed."""
    try:
        data = json.loads(_read(reviewed_path(repo, branch)) or "{}")
    except (ValueError, RecursionError):  # deeply nested junk blows the decoder's stack, not just malformed JSON
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def set_reviewed(repo: str, branch: str, stop: str, sig: str | None) -> None:
    """Mark `stop` reviewed at `sig`, or unmark it with None. Written through a
    temp file + os.replace so a crash mid-write can't leave half a JSON object;
    an empty result removes the file rather than leaving `{}` behind. Raises
    FileNotFoundError when the review dir is gone: a mark never creates it."""
    path = reviewed_path(repo, branch)
    with _reviewed_lock:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(dfd, fcntl.LOCK_EX)
            marks = read_reviewed(repo, branch)
            if sig is None:
                marks.pop(stop, None)
            else:
                marks[stop] = sig
            if not marks:
                path.unlink(missing_ok=True)
                return
            # One temp name is safe under the flock; a crash leaves at most this
            # file, which the next write replaces.
            tmp = path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(marks, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        finally:
            os.close(dfd)  # releases the flock


def clear_review(repo: str, branch: str) -> None:
    d = review_dir(repo, branch).resolve()
    if not d.is_relative_to(REVIEWS_DIR.resolve()) or d == REVIEWS_DIR.resolve():
        raise ValueError(f"refusing to remove {d}: outside the reviews dir")
    try:
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return  # only a missing dir is a no-op; permission errors must surface
    try:
        # A reviewed.json write in flight finishes first; one that waits gets
        # the lock on the dead dir and its temp write fails -> the route's 404.
        fcntl.flock(dfd, fcntl.LOCK_EX)
        shutil.rmtree(d)
    finally:
        os.close(dfd)


# --- queue -----------------------------------------------------------------


# The row's Linear ticket rides under the pane payload's names (THI-442), built
# by the same git_pr.linear_ticket_fields, so linearTicket() renders either.
class ReviewRow(git_pr.TicketFields):
    repo: str
    branch: str
    slug: str
    role: str
    pr: dict | None
    walkthrough: dict | None
    unresolved: int
    comments: int
    owner: dict | None
    worktree: str | None


def owner_for(windows: list[dict], repo: str, branch: str, aliases: set[str] | None = None) -> dict | None:
    """The live window that owns a review: a channel-attached Claude on that
    (repo, branch); the most recently active when several. Keyed on repo +
    branch, never track — a repo-default track holds every branch. `aliases`
    are other local branch names for the same review (a PR checked out as
    `pr-<N>`)."""
    names = {branch, *(aliases or ())}
    cands = [
        w
        for w in windows
        if w.get("repo_label") == repo
        and w.get("branch") in names
        and w.get("agent") == "claude"
        and w.get("channel_attached")
    ]
    if not cands:
        return None
    return max(cands, key=lambda w: w.get("activity") or 0)


def _repo_root(windows: list[dict], repo: str, roots: dict[str, str] | None = None) -> str | None:
    """Main-checkout path for a repo label: from the known-repos map when
    given, else from any live window on that repo."""
    if roots and repo in roots:
        return roots[repo]
    for w in windows:
        if w.get("repo_label") == repo and w.get("repo_key"):
            return w["repo_key"]
    return None


def worktree_for(
    windows: list[dict],
    repo: str,
    branch: str,
    *,
    fresh: bool = False,
    roots: dict[str, str] | None = None,
    aliases: set[str] | None = None,
) -> str | None:
    """Where `branch` is checked out. The queue tolerates the 60 s worktree
    cache; prepare (a create-or-reuse decision) reads fresh, as
    worktrees.for_branch's docstring requires."""
    root = _repo_root(windows, repo, roots)
    if not root:
        return None
    names = [branch, *sorted(aliases or ())]
    if fresh:
        return next((wt for n in names if (wt := worktrees.for_branch(root, n))), None)
    for path, b in worktrees._cached_worktrees(root):
        if b in names:
            return path
    return None


def aliases_for(root: str | None, repo: str, branch: str) -> set[str]:
    """`pr-<N>` for a branch whose PR number is known without calling gh: from
    the last-good cached open-PR rows, else the walkthrough header. A PR is
    checked out locally as `pr-<N>`, not its headRefName (see list_reviews)."""
    if root:
        with _prs_lock:
            hit = _prs_cache.get(root)
        for p in hit[1] if hit else ():
            if p["branch"] == branch:
                return {f"pr-{p['number']}"}
    header = parse_header(_read(walkthrough_path(repo, branch)))
    return {f"pr-{header['pr']}"} if header and header["pr"] else set()


def head_sha(worktree: str) -> str | None:
    # Bounded: a hung git must not stall the queue route.
    code, out = _run(["git", "-C", worktree, "rev-parse", "HEAD"], timeout=3.0)
    return out or None if code == 0 else None


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_STALE_CACHE: dict[tuple[str, str, str], int] = {}


def stale_by(worktree: str | None, head: str) -> int | None:
    """Commits on the worktree since the walkthrough's recorded head; None
    without a worktree or when `head` is unknown there (rebased away)."""
    if not worktree or not _SHA_RE.match(head):
        return None
    now = head_sha(worktree)
    if not now:
        return None
    key = (worktree, head, now)
    if key in _STALE_CACHE:
        return _STALE_CACHE[key]
    code, out = _run(["git", "-C", worktree, "rev-list", "--count", "--end-of-options", f"{head}..HEAD"], timeout=3.0)
    if code != 0 or not out.isdigit():
        # Not cached: a head that isn't fetched yet would stay None forever.
        return None
    _STALE_CACHE[key] = int(out)
    return _STALE_CACHE[key]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _orphan_pairs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not REVIEWS_DIR.is_dir():
        return out
    for repo_dir in sorted(REVIEWS_DIR.iterdir()):
        if not repo_dir.is_dir():
            continue
        for d in sorted(repo_dir.iterdir()):
            if not d.is_dir():
                continue
            header = parse_header(_read(d / "walkthrough.md"))
            branch = header["branch"] if header else d.name.replace("--", "/")
            out.append((repo_dir.name, branch))
    return out


# --- GitHub: the queue's primary source --------------------------------------

_GH_AVAILABLE = shutil.which("gh") is not None
_PR_FIELDS = "number,title,headRefName,isDraft,statusCheckRollup,author"


def _gh_list(repo_root: str, *extra: str) -> list[dict] | None:
    """None on any failure (offline, auth, bad JSON) so the caller can keep
    the previous cache entry instead of blanking the queue for 60 s."""
    code, out = _run(
        ["gh", "pr", "list", "--state", "open", "--json", _PR_FIELDS, "--limit", "50", *extra],
        cwd=repo_root,
        timeout=10.0,
    )
    if code != 0:
        return None
    try:
        return json.loads(out) if out else []
    except ValueError:
        return None


def open_prs(repo_root: str) -> list[dict] | None:
    """Open PRs that are mine plus open PRs where my review is requested, in
    that order, deduped by number. `[]` without gh or for a repo with no
    GitHub remote; None when gh failed."""
    if not _GH_AVAILABLE or not gitutil.github_slug(repo_root):
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for role, extra in (("mine", ("--author", "@me")), ("review-requested", ("--search", "review-requested:@me"))):
        listed = _gh_list(repo_root, *extra)
        if listed is None:
            return None
        for p in listed:
            n = p["number"]
            if n in seen:
                continue
            seen.add(n)
            out.append(
                {
                    "number": n,
                    "title": p.get("title") or "",
                    "branch": p.get("headRefName") or "",
                    "draft": bool(p.get("isDraft")),
                    "ci": git_pr._ci_glyph(p.get("statusCheckRollup")),
                    "role": role,
                    "author": (p.get("author") or {}).get("login"),
                }
            )
    return out


_PRS_TTL_S = 60.0
_prs_lock = threading.Lock()
_prs_cache: dict[str, tuple[float, list[dict]]] = {}
_prs_fetching: set[str] = set()


def _fetch_prs_into_cache(repo_root: str) -> None:
    try:
        rows = open_prs(repo_root)
    except Exception:
        rows = None
    with _prs_lock:
        if rows is None:
            # Keep the last good rows; just push the retry out by a TTL.
            prev = _prs_cache.get(repo_root)
            _prs_cache[repo_root] = (time.time(), prev[1] if prev else [])
        else:
            _prs_cache[repo_root] = (time.time(), rows)
        _prs_fetching.discard(repo_root)


def cached_open_prs(repo_root: str) -> list[dict]:
    """Stale-while-revalidate over open_prs, the same shape as
    git_pr.cached_pr_state: serve what we have, refresh in the background."""
    with _prs_lock:
        hit = _prs_cache.get(repo_root)
        fresh = hit is not None and time.time() - hit[0] < _PRS_TTL_S
        if not fresh and repo_root not in _prs_fetching:
            _prs_fetching.add(repo_root)
            _bg(f"reviews-prs:{os.path.basename(repo_root)}", _fetch_prs_into_cache, repo_root)
    return hit[1] if hit else []


# --- queue -----------------------------------------------------------------


def _pr_row(p: dict, state: str, root: str | None) -> dict:
    return {
        "number": p["number"],
        "title": p.get("title", ""),
        "draft": bool(p.get("draft")),
        "state": state,
        "ci": p.get("ci"),
        "author": p.get("author"),
        "url": _pr_url(root, p["number"]),
    }


_GH_SLUG: dict[str, str] = {}  # no TTL: the origin remote effectively never changes


def _pr_url(root: str | None, number: int) -> str | None:
    if not root:
        return None
    # Only a found slug is cached: a None can be a 3 s git timeout on a slow
    # first poll, and would otherwise strip every link from the repo until restart.
    slug = _GH_SLUG.get(root) or gitutil.github_slug(root)
    if not slug:
        return None
    _GH_SLUG[root] = slug
    return f"https://github.com/{slug}/pull/{number}"


_DEFAULT_BRANCH: dict[str, str] = {}  # no TTL: a repo's default branch effectively never changes


def _default_branch(root: str) -> str:
    if root not in _DEFAULT_BRANCH:
        _DEFAULT_BRANCH[root] = gitutil.detect_default_branch(root)
    return _DEFAULT_BRANCH[root]


def _ticket_title(pr: dict | None, owner: dict | None) -> str | None:
    """The PR title a row's ticket is read from. A row built from a walkthrough
    header, or a local branch, has none even while its PR is open (a teammate's
    drops out of review-requested:@me once you review it); the owner pane on the
    branch knows it — if its PR is the row's, not one it was linked to."""
    if pr and pr.get("title"):
        return pr["title"]
    if owner and owner.get("pr_title") and (pr is None or owner.get("pr") == pr["number"]):
        return owner["pr_title"]
    return None


def list_reviews(windows: list[dict], repos: list[str], team_keys: frozenset[str] = frozenset()) -> list[ReviewRow]:
    """Queue rows: GitHub PRs (mine, review-requested) per known repo, then
    live (repo, branch) pairs with no PR, then review dirs matching neither.
    `team_keys` is settings.linear_team_keys, passed in so this module reads no
    settings."""
    # Two discovered repos sharing a basename collapse to one: inherent to
    # label-keyed review dirs (spec D2).
    roots = {os.path.basename(r.rstrip("/")): r for r in repos}
    entries: list[tuple[str, str, str, dict | None]] = []  # (repo, branch, role, pr)
    seen: set[tuple[str, str]] = set()
    # `projects.fetch_pr_into_worktree` checks a PR out as local `pr-<N>`, not
    # its headRefName: the row keeps headRefName, but that branch is its alias.
    pr_alias: dict[tuple[str, str], set[str]] = {}
    for label, root in roots.items():
        for p in cached_open_prs(root):
            key = (label, p["branch"])
            if key in seen:
                continue
            seen.add(key)
            pr_alias[key] = {f"pr-{p['number']}"}
            entries.append((label, p["branch"], p["role"], _pr_row(p, "open", root)))
    for w in windows:
        label, branch = w.get("repo_label"), w.get("branch")
        if not label or not branch or (label, branch) in seen:
            continue
        if any(branch in a for k, a in pr_alias.items() if k[0] == label):
            continue  # the checkout of a PR already in the queue
        # Detached HEAD reports "@<sha>" or "?", never None.
        if branch.startswith("@") or branch == "?":
            continue
        root = roots.get(label) or w.get("repo_key")
        # A shell sitting on the default branch is not "sprint work not yet PR'd".
        if root and branch == _default_branch(root):
            continue
        seen.add((label, branch))
        entries.append((label, branch, "local", None))
    for key in _orphan_pairs():
        if key not in seen:
            seen.add(key)
            entries.append((key[0], key[1], "local", None))

    rows: list[ReviewRow] = []
    for repo, branch, role, pr in entries:
        text = _read(walkthrough_path(repo, branch))
        header = parse_header(text) if text else None
        aliases = pr_alias.get((repo, branch))
        owner = owner_for(windows, repo, branch, aliases)
        worktree = worktree_for(windows, repo, branch, roots=roots, aliases=aliases)
        walkthrough = None
        if header:
            walkthrough = {
                "stops": count_stops(text),
                "head": header["head"],
                "stale_by": stale_by(worktree, header["head"]),
            }
        if pr is None and header and header["pr"]:
            # The header names a PR the open lists don't: merged, closed — or still open
            # because a submitted review drops it from `review-requested:@me`.
            cwd = worktree or roots.get(repo) or ""
            st = git_pr.cached_linked_pr_state(cwd, header["pr"]) or {}
            state = st.get("pr_state") or "unknown"
            pr = _pr_row({"number": header["pr"], "author": None}, state, roots.get(repo))
            pr["ci"] = st.get("ci")
            if state == "open":
                role = "review-requested"
        comments = parse_comments(_read(comments_path(repo, branch)))
        rows.append(
            {
                "repo": repo,
                "branch": branch,
                "slug": slug_for(branch),
                "role": role,
                "pr": pr,
                "walkthrough": walkthrough,
                "unresolved": sum(1 for c in comments if not c["resolved"]),
                # The view refetches when this moves: a reply that answers without resolving changes nothing else.
                "comments": len(comments),
                "owner": {k: owner[k] for k in ("pid", "pane_id", "session", "index", "name")} if owner else None,
                "worktree": worktree,
                # The row's branch, not the owner's: a PR checked out as `pr-<N>`
                # puts its pane on a branch that names nothing.
                **git_pr.linear_ticket_fields(owner, branch, _ticket_title(pr, owner), team_keys),
            }
        )
    return rows
