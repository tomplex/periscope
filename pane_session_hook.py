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
import json
import os
import sqlite3
import sys
import time

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pane_sessions ("
    "  pane_id TEXT PRIMARY KEY,"
    "  session_id TEXT NOT NULL,"
    "  updated_at INTEGER NOT NULL"
    ")"
)


def record() -> None:
    pane = os.environ.get("TMUX_PANE", "")
    if not pane.startswith("%"):
        return
    sid = (json.load(sys.stdin) or {}).get("session_id") or ""
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


def main() -> None:
    try:
        record()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
