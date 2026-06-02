#!/usr/bin/env python3
"""periscope pane->session recorder — Claude `UserPromptSubmit` hook.

Records the pane's CURRENT Claude session id so periscope can map a tmux pane to
its SPECIFIC transcript. cwd alone collides when several panes share a directory
(periscope.turns), and `channel_shim.py` records the session only at spawn — a
`/clear` mints a NEW session id without respawning the shim, so its mapping goes
stale. This hook closes that gap: it fires on every prompt and re-records, so a
`/clear`'d (or pre-hook) pane self-corrects the moment you talk to it.

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
