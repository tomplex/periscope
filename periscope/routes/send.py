"""POST /api/send and /api/send-bulk — push keystrokes/paste to tmux panes.

`_send_to_target` does the actual paste-buffer + send-keys dance, including
the 100ms gap that lets Claude Code commit a bracketed paste before Enter
lands. Both endpoints share it; the bulk variant fans out via a
ThreadPoolExecutor.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.panes import note_action, note_focus
from periscope.tmux import tmux

router = APIRouter()


class SendBody(BaseModel):
    keys: list[str] = []
    paste: str | None = None  # bracketed-pasted into the pane before `keys`


class SendBulkBody(BaseModel):
    targets: list[str]            # ["session:index", ...]
    keys: list[str] = []
    paste: str | None = None


def _send_to_target(target: str, paste: str | None, keys: list[str]) -> dict:
    """Core paste-buffer + send-keys logic. Used by `/api/send` and the bulk
    variant; both bump focus + acted_at on the target. Returns a result dict
    on success; raises HTTPException on bad input or a tmux failure (the bulk
    variant catches it per-target so one bad pane doesn't fail the batch)."""
    if not keys and (paste is None or paste == ""):
        raise HTTPException(400, "no keys or paste")
    try:
        if paste is not None and paste != "":
            # Unique buffer name so concurrent calls (including bulk fan-out)
            # never trample each other.
            buf = f"wd-{uuid.uuid4().hex[:8]}"
            tmux("set-buffer", "-b", buf, paste)
            tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
            # Give the receiving TUI (especially Claude Code) time to apply
            # state for the paste before the submit key arrives. Without this,
            # Enter can land before React renders and submits empty input,
            # leaving the pasted text visibly stranded in the prompt area.
            if keys:
                time.sleep(0.10)
        if keys:
            tmux("send-keys", "-t", target, *keys)
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    note_focus(target)
    note_action(target)
    return {"target": target, "ok": True}


@router.post("/api/send")
def send(session: str, index: int, body: SendBody):
    """Send input to a tmux pane.

    `paste`, if set, is sent first via tmux's bracketed-paste mechanism — this
    is the only reliable way to deliver multi-line text, since tmux send-keys
    silently strips embedded newlines.

    `keys` is then sent via send-keys. Each item is either a tmux key name
    (Enter, Escape, C-c, S-Tab, Up, F1, …) or a literal string.
    """
    target = f"{session}:{index}"
    _send_to_target(target, body.paste, body.keys)
    return {"ok": True, "target": target}


@router.post("/api/send-bulk")
def send_bulk(body: SendBulkBody):
    """Fan out the same paste/keys to multiple panes concurrently.

    Each target is processed in its own thread so the per-pane 100ms
    bracketed-paste delay overlaps across panes — broadcasting `/reload-plugins`
    to 30 claudes finishes in ~100ms wall time instead of 3s sequential.

    Buffer-name collisions are avoided by `_send_to_target` minting a fresh
    uuid'd buf per call.
    """
    if not body.targets:
        raise HTTPException(400, "no targets")
    if not body.keys and (body.paste is None or body.paste == ""):
        raise HTTPException(400, "no keys or paste")

    def _send_one(t: str) -> dict:
        # Per-target failures are collected into `results`, not raised — one
        # unreachable pane shouldn't fail the whole broadcast.
        try:
            return _send_to_target(t, body.paste, body.keys)
        except HTTPException as e:
            return {"target": t, "ok": False, "error": e.detail}

    with ThreadPoolExecutor(max_workers=min(32, len(body.targets))) as pool:
        results = list(pool.map(_send_one, body.targets))
    ok_count = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": ok_count, "total": len(results), "results": results}
