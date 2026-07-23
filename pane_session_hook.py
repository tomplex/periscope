#!/usr/bin/env python3
"""periscope pane->session recorder — Claude `SessionStart` + `UserPromptSubmit` hook.

Records the pane's CURRENT Claude session id so periscope can map a tmux pane to
its SPECIFIC transcript. cwd alone collides when several panes share a directory
(periscope.turns). Registered on two events:
  - SessionStart — fires at startup AND on /clear, so a fresh or just-cleared
    pane records its own session id immediately, before its first prompt (no
    cwd-fallback to whatever was most recently active).
  - UserPromptSubmit — fires on every prompt, so panes that predate the hook
    self-correct the moment you talk to them.

Why this is the reliable producer: it reads `session_id` from the hook PAYLOAD
(authoritative + current, unlike the shim's spawn-frozen env) and `TMUX_PANE`
from the environment of a DIRECT child of the pane's Claude (the real pane id,
not the inherited/contaminated value a deep subprocess scan would see).

Writes one row into the `pane_sessions` table in
<XDG_CONFIG_HOME|~/.config>/periscope/periscope.db. The hook opens its own
short-lived SQLite connection (it runs out-of-process; no shared connection
with the periscope server). The table's journal_mode persists in the file
header, so the hook's connection inherits WAL automatically. Best-effort: any
failure is swallowed and it always exits 0, so it can never block a prompt.

Installed/removed by `bin/periscope {install-hook,uninstall-hook}`.
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pane_sessions ("
    "  pane_id TEXT PRIMARY KEY,"
    "  session_id TEXT NOT NULL,"
    "  updated_at INTEGER NOT NULL"
    ")"
)

_SCHEMA_BASES = (
    "CREATE TABLE IF NOT EXISTS session_bases ("
    "  session_id TEXT PRIMARY KEY,"
    "  repo TEXT NOT NULL,"
    "  base_sha TEXT NOT NULL,"
    "  created_at INTEGER NOT NULL"
    ")"
)


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                       text=True, timeout=10.0)
    return r.stdout.strip() if r.returncode == 0 else ""


def _stamp_session_base(c: sqlite3.Connection, sid: str, cwd: str) -> None:
    """Snapshot the worktree once per session, as the baseline for periscope's
    "changes this session" diff scope.

    Timing is why this lives in the hook rather than server-side: SessionStart
    fires before Claude's first edit, so the baseline is exact. A server-side
    stamp would only notice the new session on its next tick, and everything
    done in that window would be misattributed to before the session.

    Cost on the common path is one indexed SELECT — git only runs the first
    time a session id is seen, so the per-prompt UserPromptSubmit firing does
    no subprocess work. `git stash create` captures uncommitted work WITHOUT
    touching the worktree or the stash ref; it prints nothing on a clean tree,
    so fall back to HEAD.
    """
    if c.execute("SELECT 1 FROM session_bases WHERE session_id=?",
                 (sid,)).fetchone():
        return
    repo = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo:
        return   # not a git worktree; session scope simply won't be offered
    base = _git(repo, "stash", "create") or _git(repo, "rev-parse", "HEAD")
    if not base:
        return   # unborn branch (no commits yet)
    c.execute(
        "INSERT OR IGNORE INTO session_bases "
        "(session_id, repo, base_sha, created_at) VALUES (?,?,?,?)",
        (sid, repo, base, int(time.time())),
    )


def record() -> None:
    pane = os.environ.get("TMUX_PANE", "")
    if not pane.startswith("%"):
        return
    payload = json.load(sys.stdin) or {}
    sid = payload.get("session_id") or ""
    if not sid:
        return
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    db = os.path.join(base, "periscope", "periscope.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    # `timeout` covers the rare case where periscope's worker is mid-write —
    # SQLite serializes writers so we just wait briefly. WAL mode (set by the
    # server on first open) makes this a non-blocker in practice.
    with sqlite3.connect(db, timeout=2.0) as c:
        c.execute(_SCHEMA)
        c.execute(
            "INSERT INTO pane_sessions (pane_id, session_id, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(pane_id) DO UPDATE SET "
            "  session_id=excluded.session_id, "
            "  updated_at=excluded.updated_at",
            (pane, sid, int(time.time())),
        )
        c.execute(_SCHEMA_BASES)
        # Own try: a git failure must never cost the pane_sessions write above,
        # which is what the transcript view depends on.
        with contextlib.suppress(Exception):
            _stamp_session_base(c, sid, payload.get("cwd") or os.getcwd())


def main() -> None:
    with contextlib.suppress(Exception):
        record()
    sys.exit(0)


if __name__ == "__main__":
    main()
