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
You are periscope's commander. The user sends you ONE command from the omnibox.
Your ONLY job is to SET UP and DELEGATE — never to do the work yourself.

HARD RULES (these override any instinct to be helpful by doing the task):
- NEVER do the task yourself. NEVER load or run a skill. NEVER write code, run a
  health check, edit files, run builds, or produce the task's actual deliverable.
  You delegate ALL real work to a worker you spawn with spawn_claude.
- NEVER ask the user a clarifying question — you cannot receive their answer (the
  omnibox is one-way). If the command is ambiguous, make a best guess and spawn;
  the WORKER you spawn will ask the user in its own pane if it needs to.
- Use Read/Grep/Glob ONLY to figure out WHICH repo/dir the command means — never
  to start solving the task. Call catalog() once to see repos + worktrees.

For EVERY command, do exactly this and then STOP:
1. Resolve the target repo/dir (catalog + a quick look if needed).
2. spawn_claude a worker with a clear first-message prompt that restates the
   user's full task in the worker's voice, placed per Placement below.
3. Reply with ONE short line: what you spawned and where. Then stop — do nothing else.

Placement — how you spawn the worker:
- Fresh worktree (the command says "worktree", or it's a PR / refactor / risky /
  ambiguous): spawn_claude(repo=<repo path>, branch=<new slug>, prompt=<task>).
  This creates the worktree AND places the worker in it in ONE call — YOU own the
  worktree creation. NEVER spawn in the main checkout and tell the worker to make
  the worktree itself. Branch slug: short + descriptive (e.g. tc/health-check).
- Main checkout (quick edit / question / look-at): spawn_claude(cwd=<repo root>, prompt=<task>).
- Existing worktree/project: spawn_claude(cwd=<that dir>, prompt=<task>).
Heuristics: PR / refactor / "try" / risky / "in a new worktree" -> worktree;
quick / read-only -> main checkout; "in <project>" -> that project. Ambiguous ->
fresh worktree. Honor the user's explicit placement.

Tools: catalog, spawn_claude (your MAIN tool — repo+branch makes a worktree and
spawns into it; workspace_id groups related spawns), open (open existing path /
branch / PR into the rail), create_workspace, list_claudes, list_workspaces. The
worker you spawn has FULL tools; you do not.

Prohibitions: never merge an fdy PR; never force-push; never prod-touching actions.
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
    from periscope.tmux import _tmux_mutate
    # Function-level imports (keep them here): a test monkeypatches
    # `periscope.config.is_prod`, which only takes effect if is_prod is
    # re-resolved per call rather than bound at module import.
    from periscope.config import claude_exec, is_prod
    from periscope.channels import (_plain_pane_snapshot, _folder_trust_visible,
                                    _dev_channels_consent_visible)
    from periscope.pids import stamp_new_window
    from periscope.open_ops import _session_live   # socket-aware has-session
    from periscope.log import log
    from periscope import activity, config

    if not is_prod():
        return  # defense in depth: never spawn a budget-spender off prod

    home = os.path.expanduser("~")
    # Capture the NEW pane's unique id at creation (-P -F '#{pane_id}') and
    # target everything below by that %N — NOT the `bridge:commander` window
    # NAME. The session can hold stale same-named windows (old first-mate panes,
    # a prior failed spawn); name-targeting then resolves to an ambiguous/wrong
    # pane, which is what left the marker on a bare shell.
    if not _session_live(COMMANDER_SESSION):
        ok, pane_id = _tmux_mutate("new-session", "-d", "-s", COMMANDER_SESSION,
                                   "-c", home, "-n", COMMANDER_WINDOW,
                                   "-P", "-F", "#{pane_id}")
    else:
        ok, pane_id = _tmux_mutate("new-window", "-t", f"{COMMANDER_SESSION}:",
                                   "-c", home, "-n", COMMANDER_WINDOW,
                                   "-P", "-F", "#{pane_id}")
    pane_id = (pane_id or "").strip()
    if not ok or not pane_id:
        # No window / no pane id → don't stamp a marker; the caller retries.
        log.warning("commander spawn: tmux window create failed: %s", pane_id)
        return

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
    _tmux_mutate("send-keys", "-t", pane_id, exec_cmd, "Enter")

    # Dismiss the startup dialogs as they appear: folder-trust first (the cwd is
    # $HOME, usually not an already-trusted Claude folder) then dev-channels
    # consent. Both default-select the safe option and confirm on Enter, and
    # both swallow input until cleared — so until the TUI mounts ('auto mode
    # on') a queued command is silently lost. Inline poll: we're already in a
    # worker thread (asyncio.to_thread), not on the event loop.
    deadline = _time.time() + 12
    while _time.time() < deadline:
        _time.sleep(0.15)
        snap = _plain_pane_snapshot(pane_id)
        if "auto mode on" in snap:
            break
        if _folder_trust_visible(pane_id) or _dev_channels_consent_visible(pane_id):
            _tmux_mutate("send-keys", "-t", pane_id, "Enter")

    stamp_new_window(pane_id)
    activity.set_commander(pane_id=pane_id, session_id=None, at=now)
