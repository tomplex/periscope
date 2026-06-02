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

Writes <XDG_CONFIG_HOME|~/.config>/periscope/pane_sessions/<TMUX_PANE> = id —
the same file periscope.turns reads and channel_shim writes. Best-effort: any
failure is swallowed and it always exits 0, so it can never block a prompt.

Installed/removed by `bin/periscope {install-hook,uninstall-hook}`.
"""
import json
import os
import sys


def record() -> None:
    pane = os.environ.get("TMUX_PANE", "")
    if not pane.startswith("%"):
        return
    sid = (json.load(sys.stdin) or {}).get("session_id") or ""
    if not sid:
        return
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, "periscope", "pane_sessions")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, pane)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(sid)
    os.replace(tmp, path)  # atomic publish


def main() -> None:
    try:
        record()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
