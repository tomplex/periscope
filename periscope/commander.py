"""Commander — the hidden Claude pane the omnibox sends commands to.

A send-only actuator: a single persistent, hidden Claude Code pane (tmux
`bridge:commander`) that the omnibox drives via /api/command. The proactive
machinery (heartbeat tick, fleet digests, supervisor respawn loop, need_human
interrupt hook, kill-switch sentinel) was removed — this module now just owns
the spawn plumbing and the role prompt. The marker accessors (set/get/clear)
and the captain's log live in activity.py.
"""

from __future__ import annotations

import asyncio
import time

from periscope.panes import list_windows


ROLE_PROMPT = """\
You are periscope's commander. The user sends you commands from the omnibox; act
on them immediately with your tools, then narrate what you did concisely.

You ORCHESTRATE, you do not edit. To do work in a repo, spawn a worker
(spawn_claude) with a clear first-message prompt and an explicit cwd; the worker
has full tools. You have read-only code access (Read/Grep/Glob) to understand and
route — resolve fuzzy references ("the attribute config refactor" -> which
repo/dir) before acting. Call catalog() ONCE per command to see repos + worktrees
and reuse the result; do not poll it.

Placement — choose where each worker lands by the cwd you pass:
- Main checkout: spawn_claude(cwd=<repo root>).
- Fresh worktree: open(repo, branch=<new>) to create it, then spawn into it.
- Existing project/worktree: spawn_claude(cwd=<that dir>).
Heuristics: PR / refactor / "try" / risky -> worktree; quick edit / question /
look-at -> main checkout; "in <project>" -> that project. When genuinely
ambiguous, default to a fresh worktree. Honor the user's explicit placement.

Tools: catalog, create_workspace, open (open(repo, branch) creates a worktree),
spawn_claude, list_claudes, list_workspaces, peek, send_to, the captain's log.

Absolute prohibitions: never merge an fdy pull request; never force-push; never
take prod-touching actions.
"""


COMMANDER_SESSION = "bridge"
COMMANDER_WINDOW = "commander"

_SPAWN_LOCK = asyncio.Lock()   # single-flight: lifespan boot vs first /api/command


async def ensure_commander():
    """Ensure exactly one live commander pane; (re)spawn if the marker's pane is
    gone. Single-flight via _SPAWN_LOCK so a boot-spawn and a racing first
    /api/command can't double-spawn. Returns the CommanderMarker (or None off
    prod / on spawn failure). The blocking spawn runs in a thread so it never
    stalls the event loop serving other panes' MCP connections."""
    from periscope import activity
    async with _SPAWN_LOCK:
        marker = activity.get_commander()
        live = {w.get("pane_id") for w in list_windows()}
        if marker is not None and marker.pane_id in live:
            return marker
        await asyncio.to_thread(_spawn_commander, now=int(time.time()))
        return activity.get_commander()


def _spawn_commander(*, now: int) -> None:
    """Ensure the `bridge` session, open a single `commander` window running
    claude_exec() + --append-system-prompt ROLE_PROMPT, stamp it, set the
    marker. Borrows worktree_spawn._layout_two_window's sequence (single window,
    no HTTPException — this is a lifespan task, not a request)."""
    import os
    import shlex
    import time as _time
    from periscope.tmux import tmux, _tmux_mutate
    # Function-level imports (keep them here): a test monkeypatches
    # `periscope.config.is_prod`, which only takes effect if is_prod is
    # re-resolved per call rather than bound at module import.
    from periscope.config import claude_exec, is_prod
    from periscope.channels import dismiss_dev_channels_consent_bg
    from periscope.pids import stamp_new_window
    from periscope.open_ops import _session_live   # socket-aware has-session
    from periscope.log import log
    from periscope import activity, config

    if not is_prod():
        return  # defense in depth: never spawn a budget-spender off prod

    home = os.path.expanduser("~")
    if not _session_live(COMMANDER_SESSION):
        ok, msg = _tmux_mutate("new-session", "-d", "-s", COMMANDER_SESSION,
                               "-c", home, "-n", COMMANDER_WINDOW)
    else:
        ok, msg = _tmux_mutate("new-window", "-t", f"{COMMANDER_SESSION}:",
                               "-c", home, "-n", COMMANDER_WINDOW)
    if not ok:
        # Don't stamp a marker for a window that doesn't exist — the caller
        # retries cleanly. Stamping now would leak a bogus marker.
        log.warning("commander spawn: tmux window create failed: %s", msg)
        return
    target = f"{COMMANDER_SESSION}:{COMMANDER_WINDOW}"
    # Deliver the (multi-line) role prompt via a file, not inline: send-keys
    # strips embedded newlines (CLAUDE.md note 5), which mangles a multi-line
    # --append-system-prompt arg AND the ~1.5k-char command line never lands
    # intact. Writing it to a file keeps the launch command short and
    # single-line; the shell substitutes the file's content as the arg.
    prompt_path = config.ACTIVITY_DB.parent / "commander-prompt.txt"
    prompt_path.write_text(ROLE_PROMPT)
    exec_cmd = (f"{claude_exec()} --model sonnet "
                f"--allowedTools Read,Grep,Glob --disallowedTools Bash,Edit,Write "
                f"--append-system-prompt "
                f'"$(cat {shlex.quote(str(prompt_path))})"')
    _time.sleep(0.1)  # let rc finish before the command lands (CLAUDE.md note 5)
    _tmux_mutate("send-keys", "-t", target, exec_cmd, "Enter")
    if "--dangerously-load-development-channels" in exec_cmd:
        dismiss_dev_channels_consent_bg(target)
    stamp_new_window(target)
    pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()
    if not pane_id:
        # A bogus empty marker is never in the live set, so the caller would
        # respawn — a window/budget leak. Leave the marker unset; retry cleanly.
        log.warning("commander spawn: could not read pane_id; leaving marker unset")
        return
    activity.set_commander(pane_id=pane_id, session_id=None, at=now)
