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

from periscope import state_hub, tmux_input, tmux_mirror
from periscope.log import _task, log
from periscope.panes import note_action
from periscope.tmux import tmux

router = APIRouter()


@router.websocket("/ws/state")
async def ws_state(websocket: WebSocket):
    """Push the dashboard state blob (the /api/state payload) on the hub's
    clock. Replaces the browser's 3s poll; the REST endpoint stays as fallback.
    """
    await websocket.accept()
    q = state_hub.subscribe()

    # A reader task so a client disconnect is noticed promptly even between
    # ticks (the send loop alone would only see it on the next blob).
    async def drain_in():
        with contextlib.suppress(Exception):
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break

    reader = _task("ws-state-reader", drain_in())
    try:
        while True:
            blob = await q.get()
            await websocket.send_text(blob)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: send after the socket closed (reader saw disconnect).
        pass
    finally:
        reader.cancel()
        state_hub.unsubscribe(q)


@router.websocket("/ws/pane")
async def ws_pane(
    websocket: WebSocket,
    pane_id: str,
    cols: int = 0,
    rows: int = 0,
):
    await websocket.accept()
    # The terminal address is the stable tmux pane id (%N), not the
    # session:index composite: under the shared session with
    # `renumber-windows on`, a window's index drifts on every kill/new
    # — which would stale an OPEN terminal mid-stream. Pane ids never
    # drift. tmux accepts %id for pane commands (capture-pane,
    # display-message, send-keys) and resolves it to its window for
    # window commands (resize-window, setw).
    target = pane_id
    loop = asyncio.get_running_loop()

    # Periscope owns the pane width. Once we resize a window to the modal's
    # dims, we LEAVE IT THERE — no restore on disconnect. Rationale: the
    # save/restore dance only matters if a real terminal is also attached
    # at a different size, but in periscope-primary workflows the restore
    # just causes churn — each modal open/close pair would reflow the
    # buffer twice, and reflows during streaming produce duplicated table
    # fragments in Claude's scrollback. Holding the pane at periscope's
    # size means subsequent opens at the same width are no-ops.
    # A tmux invocation runs its `;`-separated commands in order, so pairing
    # them costs one fork instead of two. Each fork is ~20ms — measurable on
    # the pane-switch path, where this runs on every connect.
    def set_pane_size(c: int, r: int) -> None:
        with contextlib.suppress(Exception):
            tmux("setw", "-t", target, "window-size", "manual", ";",
                 "resize-window", "-t", target, "-x", str(c), "-y", str(r))

    # 1+2) One fork: resize to the client's hint, then read tmux's view of the
    #    pane back. The resize must land BEFORE the initial capture or box-
    #    drawing TUIs mangle on the first frame, and chaining guarantees that
    #    ordering — display-message reports the post-resize geometry.
    #    The meta itself is size, cursor and alt-screen (all three needed to
    #    render the initial blob into a matching xterm state) plus
    #    #{session_name}/#{window_index} for the mirror subscription (keyed on
    #    session NAME) and the recency stamp (keyed on session:index, NOT the
    #    %pane_id address — window_view reads recency_stamps_for(
    #    f"{session}:{index}")). If the pane is gone, close: the client's
    #    reconnect FSM handles it.
    meta_fmt = ("#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}"
                "|#{alternate_on}|#{pane_id}|#{session_name}|#{window_index}"
                "|#{mouse_any_flag}")

    def size_then_meta(c: int, r: int) -> str:
        if c > 0 and r > 0:
            out = tmux("setw", "-t", target, "window-size", "manual", ";",
                       "resize-window", "-t", target, "-x", str(c), "-y", str(r), ";",
                       "display-message", "-t", target, "-p", meta_fmt)
            # tmux aborts the chain at the first failing command. A resize
            # failure has always been non-fatal here, so don't let it take the
            # handshake down — re-ask for the meta on its own.
            if out.strip():
                return out
        return tmux("display-message", "-t", target, "-p", meta_fmt)

    try:
        meta = await loop.run_in_executor(
            None, lambda: size_then_meta(cols, rows))
        (cols_s, rows_s, cx_s, cy_s, alt_s,
         _pane_id, session_name, window_index, mouse_s) = meta.strip().split("|")
        cols, rows = int(cols_s), int(rows_s)
        cx, cy = int(cx_s), int(cy_s)
        alt_on = alt_s == "1"
        # tmux consumes the app's mouse-mode DECSET (that's why it's a pane
        # flag), so the xterm mirror never sees `\e[?1003h` and can't forward
        # wheel as mouse events — it converts them to arrows instead. Tell the
        # client the pane's mouse state so it can synthesize wheel reports.
        mouse_on = mouse_s == "1"
    except Exception as e:
        # Usually the pane died between the poll that listed it and this
        # connect, which the client's reconnect FSM handles. But a caller that
        # forgot to percent-encode the `%` of a pane id lands here too, and a
        # bare close() — code 1000, no reason — makes that indistinguishable
        # from a clean shutdown. Name it.
        log.warning("ws/pane handshake failed for target=%r (%s)", target, e)
        await websocket.close()
        return

    # Opening a terminal is the canonical "opened in periscope" action.
    # Stamp it on the recency map's session:index key (window_view's read
    # key), now that we've parsed it — a %pane_id key would be a dead write.
    note_action(f"{session_name}:{window_index}")

    sub = await tmux_mirror.subscribe(session_name, pane_id)
    async with sub:
        await websocket.send_text(
            json.dumps({"type": "size", "cols": cols, "rows": rows})
        )
        await websocket.send_text(
            json.dumps({"type": "mouse", "on": mouse_on})
        )

        # 3) Initial paint, via the fork path — NOT the control client: a
        #    50k-history `-e` capture can run multi-MB, and tmux holds all
        #    %output for the whole session until a reply block completes.
        #    The capture-vs-subscribe byte gap this allows is healed by the
        #    mirror's connect-time reconcile moments later; only scrollback
        #    can miss those few bytes.
        #    `-S -N` asks for N lines of scrollback; tmux clamps to the
        #    pane's history-limit.
        # -N preserves trailing spaces; without it capture-pane strips them and
        # the cursor row renders short while the cursor goes to tmux's true
        # column, leaving a one-cell gap (see tmux_mirror._fire_reconcile).
        initial = await loop.run_in_executor(None, lambda: tmux(
            "capture-pane", "-t", target, "-p", "-e", "-N", "-S", "-10000"))
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
