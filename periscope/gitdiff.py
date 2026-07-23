"""Git-backed diffs for the pane's Changes tab.

Two scopes, both computed against the pane's worktree as it is *right now*
(committed + uncommitted, so the tab shows reality, not just what's staged):

- `branch`  — everything this branch has done: `git diff <merge-base(default,HEAD)>`
- `session` — everything since the current Claude session started:
              `git diff <session baseline>` (see periscope.activity.session_bases)

Why git and not the transcript: `git diff` catches edits made by Bash, by the
user in another editor, and by any tool Claude used — the JSONL only ever knew
about Edit/Write tool calls. It also gives real before/after, which a transcript
`old_string`/`new_string` pair cannot.

Line matching is git's job — unified-diff output IS the matched result, so there
is no LCS/Myers implementation here. This module shells out, parses hunks, and
returns a structure the client renders directly.
"""
import os
import re

from periscope.gitutil import detect_default_branch
from periscope.tmux import _run

# Rendering caps. Explicit truncation with a count beats a silently clipped
# scroll box — the client shows "showing N of M" and the user knows.
MAX_FILES = 300
MAX_LINES_PER_FILE = 2000

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def repo_root(cwd: str) -> str | None:
    """git toplevel for `cwd`, or None when it isn't a git worktree."""
    if not cwd or not os.path.isdir(cwd):
        return None
    code, out = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    return out if code == 0 and out else None


def snapshot_base(repo: str) -> str | None:
    """A commit capturing the worktree as it is now, for use as a session
    baseline. `git stash create` writes a dangling commit WITHOUT touching the
    worktree or the stash ref (verified: `git status` is unchanged after).
    Returns HEAD when the tree is clean — stash create prints nothing then.

    Known gap: stash create does not capture *untracked* files, so a file that
    existed-but-untracked before the session reads as session-created. Tracking
    those would mean `git add -A` (mutates the index) — not worth it.
    """
    code, out = _run(["git", "-C", repo, "stash", "create"], timeout=10.0)
    if code == 0 and out:
        return out
    code, head = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    return head if code == 0 and head else None


def branch_base(repo: str) -> str | None:
    """Fork point of this branch from the repo's default branch."""
    default = detect_default_branch(repo)
    for ref in (f"origin/{default}", default):
        code, out = _run(["git", "-C", repo, "merge-base", ref, "HEAD"])
        if code == 0 and out:
            return out
    return None


def _numstat(repo: str, base: str) -> dict[str, tuple[int, int]]:
    """path -> (additions, deletions). Binary files report '-' and land as 0."""
    code, out = _run(["git", "-C", repo, "diff", "--numstat", base], timeout=20.0)
    if code != 0:
        return {}
    stats: dict[str, tuple[int, int]] = {}
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        adds, dels, path = parts
        stats[path] = (
            int(adds) if adds.isdigit() else 0,
            int(dels) if dels.isdigit() else 0,
        )
    return stats


def parse_unified(text: str) -> list[dict]:
    """Parse `git diff` unified output into per-file hunk structures.

    Each line carries its own kind so the client never has to re-derive it from
    a leading character (which is ambiguous for a context line that itself
    starts with '+').
    """
    files: list[dict] = []
    cur: dict | None = None
    hunk: dict | None = None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            # "diff --git a/x b/x" — take the b-side, which is correct for
            # renames and is the path the user is looking at now.
            cur = {"path": raw.split(" b/", 1)[-1] if " b/" in raw else "",
                   "status": "modified", "hunks": [], "truncated": False}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif raw.startswith("new file mode"):
            cur["status"] = "added"
        elif raw.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif raw.startswith("rename to "):
            cur["status"] = "renamed"
        elif raw.startswith("Binary files"):
            cur["status"] = "binary"
        elif raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if not m:
                continue
            hunk = {
                "header": (m.group(5) or "").strip(),   # enclosing symbol, if git found one
                "old_start": int(m.group(1)),
                "new_start": int(m.group(3)),
                "lines": [],
            }
            cur["hunks"].append(hunk)
        elif hunk is not None and raw[:1] in ("+", "-", " "):
            if sum(len(h["lines"]) for h in cur["hunks"]) >= MAX_LINES_PER_FILE:
                cur["truncated"] = True
                continue
            kind = {"+": "add", "-": "del", " ": "ctx"}[raw[0]]
            hunk["lines"].append({"kind": kind, "text": raw[1:]})
    return files


def diff_for(repo: str, base: str) -> dict:
    """Structured diff of the worktree against `base`."""
    code, text = _run(
        # -M detects renames; -U3 is git's default context and reads well at
        # the transcript column's width.
        ["git", "-C", repo, "diff", "-M", "-U3", "--no-color", base],
        timeout=30.0,
    )
    if code != 0:
        return {"base": base, "files": [], "truncated": 0, "error": "git diff failed"}
    files = parse_unified(text)
    stats = _numstat(repo, base)
    for f in files:
        adds, dels = stats.get(f["path"], (0, 0))
        f["additions"], f["deletions"] = adds, dels
    dropped = max(0, len(files) - MAX_FILES)
    return {"base": base, "files": files[:MAX_FILES], "truncated": dropped}
