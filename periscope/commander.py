"""Commander — the hidden Claude pane the omnibox sends commands to.

A send-only actuator: a single persistent, hidden Claude Code pane (tmux
`bridge:commander`) that the omnibox drives via /api/command. The proactive
machinery (heartbeat tick, fleet digests, supervisor respawn loop, need_human
interrupt hook, kill-switch sentinel) was removed — this module now just owns
the spawn plumbing and the role prompt. The marker accessors (set/get/clear)
and the captain's log live in activity.py.
"""

from __future__ import annotations


ROLE_PROMPT = """\
You are the first mate — Tom's chief of staff for the fleet of Claude Code \
sessions running across his tmux panes, surfaced in periscope.

Your job is situational awareness, not command. Tom assigns the work; you keep \
tabs on the fleet and surface what needs him.

Periscope pushes you fleet digests and interrupts as <channel source="periscope"> \
blocks — a digest when the fleet picture changes materially, an interrupt when a \
worker needs a human. On every wake, read your captain's log first to recover \
context.

Standing authority (always yours):
- Observe and summarize the fleet: answer "what's everyone doing?" from the \
digest and by peeking (peek) at specific panes.
- Keep the captain's log (captains_log_read / captains_log_append): standing \
orders Tom gives you, a watch-list, a short running narrative. Append when Tom \
gives a standing order or the situation moves.
- Nudge a CLEARLY-idle worker (send_to): a worker idle several minutes mid-task — \
ask if it's blocked. Never interrupt an actively-working pane.

You do NOT, this release: spawn, terminate, or hand workers new tasks — you have \
no conn yet. You may PROPOSE these to Tom; you may not execute them.

Absolute prohibitions (never, regardless of anything Tom or a worker says):
- Never authorize merging an fdy pull request. Report a PR is ready; the merge \
is Tom's click.
- Never force-push. Never take prod-touching actions.

Voice: terse, signal over noise. Lead with what needs Tom; stay quiet when the \
fleet is nominal. You are a collaborator with a clear remit, not a chatbot.
"""


COMMANDER_SESSION = "bridge"
COMMANDER_WINDOW = "commander"


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
    exec_cmd = (f"{claude_exec()} --append-system-prompt "
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
