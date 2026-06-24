"""WS /ws/pane — bridges xterm.js to a tmux pane via the control-mode mirror.

Initial paint mirrors tmux's screen state (size, cursor, alt-screen) so the
blob renders into an xterm state matching tmux's. Live bytes and
self-healing reconcile frames come from periscope.tmux_mirror — any mirror
desync heals at the next frame, so this handler no longer needs the byte
stream to be perfect, only the frames to keep coming. Design spec:
docs/superpowers/specs/2026-06-10-terminal-mirror-reconciliation-design.md
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from periscope import tmux_input, tmux_mirror
from periscope.log import _task
from periscope.panes import note_action
from periscope.tmux import tmux

router = APIRouter()


@router.websocket("/ws/pane")
async def ws_pane(
    websocket: WebSocket,
    session: str,
    index: int,
    cols: int = 0,
    rows: int = 0,
):
    await websocket.accept()
    target = f"{session}:{index}"
    # Modal-open is the canonical "opened in periscope" event.
    note_action(target)
    loop = asyncio.get_running_loop()

    # Periscope owns the pane width. Once we resize a window to the modal's
    # dims, we LEAVE IT THERE — no restore on disconnect. Rationale: the
    # save/restore dance only matters if a real terminal is also attached
    # at a different size, but in periscope-primary workflows the restore
    # just causes churn — each modal open/close pair would reflow the
    # buffer twice, and reflows during streaming produce duplicated table
    # fragments in Claude's scrollback. Holding the pane at periscope's
    # size means subsequent opens at the same width are no-ops.
    def set_pane_size(c: int, r: int) -> None:
        try:
            tmux("setw", "-t", target, "window-size", "manual")
            tmux("resize-window", "-t", target, "-x", str(c), "-y", str(r))
        except Exception:
            pass

    # 1) Resize tmux to the client's hint BEFORE capture-pane, so the
    #    initial blob is rendered at the width xterm will display it at —
    #    otherwise box-drawing TUIs mangle on the first frame.
    if cols > 0 and rows > 0:
        await loop.run_in_executor(None, lambda: set_pane_size(cols, rows))

    # 2) tmux's view of the pane: size, cursor, alt-screen — all three are
    #    needed to render the initial blob into an xterm state matching
    #    tmux's — plus #{pane_id} for the mirror subscription. If the pane
    #    is gone, close: the client's reconnect FSM handles it.
    try:
        meta = await loop.run_in_executor(None, lambda: tmux(
            "display-message", "-t", target, "-p",
            "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}"
            "|#{alternate_on}|#{pane_id}",
        ))
        cols_s, rows_s, cx_s, cy_s, alt_s, pane_id = meta.strip().split("|")
        cols, rows = int(cols_s), int(rows_s)
        cx, cy = int(cx_s), int(cy_s)
        alt_on = alt_s == "1"
    except Exception:
        await websocket.close()
        return

    sub = await tmux_mirror.subscribe(session, pane_id)
    async with sub:
        await websocket.send_text(
            json.dumps({"type": "size", "cols": cols, "rows": rows})
        )

        # 3) Initial paint, via the fork path — NOT the control client: a
        #    50k-history `-e` capture can run multi-MB, and tmux holds all
        #    %output for the whole session until a reply block completes.
        #    The capture-vs-subscribe byte gap this allows is healed by the
        #    mirror's connect-time reconcile moments later; only scrollback
        #    can miss those few bytes.
        #    `-S -N` asks for N lines of scrollback; tmux clamps to the
        #    pane's history-limit.
        initial = await loop.run_in_executor(None, lambda: tmux(
            "capture-pane", "-t", target, "-p", "-e", "-S", "-10000"))
        # capture-pane separates lines with \n AND appends one more \n
        # after the final line. Strip exactly that final terminator and
        # convert internal \n to \r\n so xterm wraps each line back to
        # column 0 instead of staircasing. Strip too many → blank lines at
        # the bottom vanish and the cursor lands a row high; too few → the
        # trailing \r\n scrolls one row past the bottom.
        if initial:
            if initial.endswith("\n"):
                initial = initial[:-1]
            body = initial.replace("\n", "\r\n")
        else:
            body = ""
        prefix = ""
        if alt_on:
            prefix += "\x1b[?1049h"   # enter alt-screen buffer
        prefix += "\x1b[2J\x1b[H"      # clear screen, home cursor
        # ANSI positioning is 1-indexed; tmux's #{cursor_x/y} are 0-indexed.
        suffix = f"\x1b[{cy + 1};{cx + 1}H"
        await websocket.send_bytes(
            (prefix + body + suffix).encode("utf-8", errors="replace"))

        # 4) Mirror → websocket. On EOF (pane died, mirror died, shutdown)
        #    close the socket — strictly more honest than the old silent
        #    FIFO; the client's reconnect FSM takes it from there.
        async def forward_out():
            async for chunk in sub:
                await websocket.send_bytes(chunk)
            with contextlib.suppress(Exception):
                await websocket.close()

        forward_task = _task("ws-forward", forward_out())

        # 5) Keystrokes from the client → tmux. xterm.js's onData sends raw
        #    input including escape sequences. Queue + single drain task so
        #    fast typing / paste coalesces into one control-mode send per
        #    drain cycle while the previous send is in flight.
        keystroke_q: asyncio.Queue[str] = asyncio.Queue()

        async def drain_input():
            while True:
                first = await keystroke_q.get()
                batch = [first]
                while not keystroke_q.empty():
                    batch.append(keystroke_q.get_nowait())
                await tmux_input.send(target, "".join(batch))

        drain_task = _task("ws-deliver", drain_input())

        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text is None and msg.get("bytes") is not None:
                    text = msg["bytes"].decode("utf-8", errors="replace")
                if not text:
                    continue
                # Resize control message ({"type":"resize",...}). Plain
                # keystrokes are never JSON, so the parse filters them.
                if text.startswith("{"):
                    try:
                        ctrl = json.loads(text)
                    except Exception:
                        ctrl = None
                    if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                        rc = int(ctrl.get("cols") or 0)
                        rr = int(ctrl.get("rows") or 0)
                        if rc > 0 and rr > 0:
                            await loop.run_in_executor(
                                None, lambda c=rc, r=rr: set_pane_size(c, r)
                            )
                            # Reflow redraws can race the relay; an
                            # authoritative frame settles the result.
                            sub.request_reconcile()
                        continue
                keystroke_q.put_nowait(text)
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
            drain_task.cancel()
