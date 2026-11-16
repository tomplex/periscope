"""POST /api/pane/permission and /api/pane/reply — the buttons that make an agent act.

A native banner's Allow / Deny answers a Claude permission dialog; its Send
and "Ask agent to fix" type a message into the pane (THI-426's rail card reuses
both). Unlike /api/send, a raw key pipe, each re-checks the pane when pressed,
because a banner can be minutes stale: Allow must only ever answer the request
its banner was shown for, and a reply must never land in a dialog, where
`1 sounds good` + Enter picks option 1, in a shell or Claude's shell mode,
where it runs, or on a half-typed draft, which Enter would submit with it.

JSON bodies only. A POST without one is a CORS "simple" request any website
could fire at localhost; application/json needs a preflight this server never
answers. FastAPI 0.136.1 already won't parse a body with no Content-Type as
JSON (`strict_content_type`, on by default), but that's a setting, and the
dependency is unpinned (older releases parsed it), so the type is checked
here too: defence in depth, and a plain 415 rather than a 422.
"""

import re
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from periscope.log import log
from periscope.panes import (
    composer_holds,
    composer_ready,
    dialog_matches,
    dialog_open,
    needs_since_for,
    parse_pane,
    parse_permission_dialog,
)
from periscope.pids import window_by_pid
from periscope.routes.send import _send_to_target
from periscope.session_status import live_session_id_for_pane, session_state_for
from periscope.tmux import capture
from periscope.turns import session_id_for_pane
from periscope.window_view import pending_permission_row


def _json_only(request: Request) -> None:
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "send a JSON body (Content-Type: application/json)")


router = APIRouter(dependencies=[Depends(_json_only)])

# `1` is "Yes", taken at once (no Enter). Escape is "No, and tell Claude what
# to do differently": it interrupts ("What should Claude do instead?") and
# leaves the pane waiting for you. Never a number for No: it's option 4 for
# Bash and 3 for Write in 2.1.280, and moves between versions.
_PERMISSION_KEYS = {"allow": ["1"], "deny": ["Escape"]}

# Check-then-send runs under one lock, and a request is answered once. Two
# presses of one banner (the banner and the rail card, a double click) could
# otherwise both pass the checks before Claude redraws: the second `1` lands
# in the composer, or on the dialog a parallel tool call raises next. Marked
# BEFORE the key goes in, so a failed send can't be retried from a banner
# either: the 409 reveals the pane, where the dialog is answered by hand.
_ACT_LOCK = threading.Lock()
_ANSWERED: deque[str] = deque(maxlen=256)

# A pane whose foreground process is a shell isn't running an agent, whatever
# its session file says: a Claude suspended with ctrl+z stays a live claude
# process (session_state_for answers) while the prompt is zsh's, which would
# run a reply as a command.
_SHELLS = frozenset({"bash", "csh", "dash", "fish", "ksh", "nu", "sh", "tcsh", "zsh"})

# An ESC[201~ in the text ends tmux's bracketed paste early and the rest is
# typed as keys (a CR submits before _submit_when_held looks; in shell mode a
# Ctrl-U, `!cmd` and CR runs a command). "Ask agent to fix" builds its text
# from CI check names, which anything with checks write access controls.
# Every C0/C1 control goes but \t and \n.
_CONTROLS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# A reply goes in as two steps with a look in between: the paste, then the
# Enter only once a fresh capture shows the paste in the composer and no
# dialog. One look before the paste left ~150 ms (load-buffer, paste, settle)
# for a permission dialog to render, take focus and be confirmed (`1. Yes`)
# by the Enter, and "Ask agent to fix" goes to working agents, the panes
# that raise them. The paste gets _PASTE_SETTLE_S to show before each look,
# for at most _PASTE_LOOKS looks.
_PASTE_SETTLE_S = 0.10
_PASTE_LOOKS = 4


class PermissionBody(BaseModel):
    pid: str
    request_id: str
    choice: str


class ReplyBody(BaseModel):
    pid: str
    text: str


def _live_pane(handle: str) -> tuple[str, str, str, dict]:
    """(pid, pane_id, target, window) of the live pane, found by pid — never
    by a target tmux may have renumbered since the banner was posted
    (invariant 11). Callers build nothing from the banner but the pid, and
    capture and send by pane_id (`%N`): the pane every check was made on,
    where `session:index` is re-resolved by tmux at send time."""
    pid, pane_id, window = window_by_pid(handle)
    if not window or not pane_id:
        raise HTTPException(404, f"no live pane for pid {handle!r}")
    return pid, pane_id, f"{window['session']}:{window['index']}", window


def _screen(pane_id: str) -> str:
    try:
        return capture(pane_id)
    except Exception as e:
        raise HTTPException(500, f"could not read the pane: {e}") from e


def _submit_when_held(target: str, pane_id: str, text: str) -> bool:
    """Send the reply's Enter once a fresh capture shows the paste in the
    composer and no dialog; False, with nothing sent, when a dialog shows up
    or the paste never does."""
    for _ in range(_PASTE_LOOKS):
        time.sleep(_PASTE_SETTLE_S)
        screen = _screen(pane_id)
        if dialog_open(screen):
            return False
        if composer_holds(screen, text):
            _send_to_target(target, None, ["Enter"], address=pane_id)
            return True
    return False


@router.post("/api/pane/permission")
def pane_permission(body: PermissionBody):
    """Answer the pane's permission dialog: `allow` sends `1`, `deny` Escape.

    409 unless the dialog is still the one the caller saw: the request wasn't
    answered from here already, the pane waits on a permission prompt, the
    request it's asking (recomputed from the hook's row, never taken from the
    caller) is `request_id`, and a fresh capture shows a dialog asking exactly
    that row's command or file (panes.dialog_matches). The screen check is
    what stops a stale row from approving a different dialog that's up."""
    keys = _PERMISSION_KEYS.get(body.choice)
    if keys is None:
        raise HTTPException(400, f"choice must be allow or deny, not {body.choice!r}")
    with _ACT_LOCK:
        pid, pane_id, target, _window = _live_pane(body.pid)
        if body.request_id in _ANSWERED:
            raise HTTPException(409, "that request was already answered")
        # The pane_sessions fallback too, as window_view looks it up, so the
        # request id recomputed here agrees with the one /api/state published
        # (the screen match keeps it safe); a reply wants a live agent instead.
        sess = session_state_for(session_id_for_pane(pane_id)) or {}
        row = pending_permission_row(pane_id, sess.get("state"), sess.get("waiting_for"), needs_since_for(pid))
        if row is None:
            raise HTTPException(409, "the pane isn't waiting on a permission request periscope can verify")
        if row["request_id"] != body.request_id:
            raise HTTPException(409, "the pane is asking about a different request now")
        parsed = parse_permission_dialog(_screen(pane_id))
        if parsed is None or not dialog_matches(parsed, row["tool"], row["match_key"]):
            raise HTTPException(409, "the dialog on screen isn't the request's")
        _ANSWERED.append(body.request_id)
        try:
            _send_to_target(target, None, keys, address=pane_id)
        except HTTPException:
            log.warning(
                "pane-action: %s on %s (%s, %s) failed after it was marked answered",
                body.choice,
                pid,
                pane_id,
                body.request_id,
            )
            raise
    log.info("pane-action: %s %s on %s (%s, %s)", body.choice, row["tool"], pid, pane_id, body.request_id)
    return {"ok": True}


@router.post("/api/pane/reply")
def pane_reply(body: ReplyBody):
    """Type `text` into the pane and submit it (a bracketed paste, then Enter,
    as the composer does; invariant 5).

    409 while any dialog may be open, by either signal: the session file says
    the pane waits on something (with or without a waitingFor), or the screen
    shows a dialog (a permission dialog's footer is outside parse_pane's
    family). 409 too when the pane isn't running an agent: no live session
    and no agent on screen, or a shell in the foreground. A Codex pane has no
    Claude session and no Claude screen, so it gets the 409 (the route can't
    rule out a Codex approval prompt, where a `y…` would approve). And 409
    unless the composer is at a plain, empty `❯` prompt (panes.composer_ready):
    in shell mode (`!`) Enter runs the text as a command, and on a draft it
    submits the draft with it.

    Then the paste goes in alone, and Enter follows only when a fresh capture
    shows no dialog and the paste in the composer (panes.composer_holds);
    otherwise 409 with nothing submitted (the paste may sit in the composer,
    or in a dialog that took focus)."""
    text = _CONTROLS_RE.sub("", body.text).strip()
    if not text:
        raise HTTPException(400, "nothing to send")
    with _ACT_LOCK:
        pid, pane_id, target, window = _live_pane(body.pid)
        # Live only: the recorded pane_sessions row can name another pane's
        # session after a resume or a reused %N.
        sess = session_state_for(live_session_id_for_pane(pane_id)) or {}
        if sess.get("state") == "needs-input":
            waiting = sess.get("waiting_for") or "needs input"
            raise HTTPException(409, f"the pane has a dialog open ({waiting}) — answer it first")
        screen = _screen(pane_id)
        if dialog_open(screen):
            raise HTTPException(409, "the pane has a dialog open — answer it first")
        foreground = (window.get("current_command") or "").lstrip("-")
        if foreground in _SHELLS or not (sess or parse_pane(screen).get("agent")):
            raise HTTPException(409, "the pane isn't running an agent")
        # Positive, not parse_pane's pending_input: that scan looks for the
        # last `❯` row, and in shell mode (`!` in its place) it finds a past
        # prompt in scrollback, or, in a fresh session, nothing at all.
        if not composer_ready(screen):
            raise HTTPException(
                409, "the pane's composer isn't at a plain prompt — shell mode or a draft; open it to see"
            )
        _send_to_target(target, text, [], address=pane_id)
        if not _submit_when_held(target, pane_id, text):
            log.info("pane-action: reply on %s (%s), %d chars, pasted but not submitted", pid, pane_id, len(text))
            raise HTTPException(
                409,
                "the pane changed while the reply was typed — the text may be in the composer or the dialog;"
                " nothing was submitted; open it",
            )
    log.info("pane-action: reply on %s (%s), %d chars", pid, pane_id, len(text))
    return {"ok": True}
