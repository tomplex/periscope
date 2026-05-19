"""WS /ws/pane — bridges xterm.js to a tmux pane via pipe-pane FIFO.

Initial paint mirrors tmux's screen state (size, cursor, alt-screen) so
incremental updates from the FIFO land at the right cursor and don't
leave ghost text. Resize messages from the client snapshot the window's
original size/mode the first time and restore on disconnect.
"""

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from periscope.log import _task
from periscope.panes import note_action
from periscope.tmux import tmux, deliver_input

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
    # Modal-open is the canonical "opened in periscope" event. The grid view's
    # `focused_at` doesn't move (no tmux focus shift here), but the stream view
    # should treat this as engagement with the window.
    note_action(target)
    fifo_path = f"/tmp/periscope.{uuid.uuid4().hex}.fifo"
    fd = None
    loop = asyncio.get_running_loop()
    pipe_active = False

    # Periscope owns the pane width. Once we resize a window to the modal's
    # dims, we LEAVE IT THERE — no restore on disconnect. Rationale: the
    # save/restore dance only matters if a real terminal is also attached
    # at a different size, but in periscope-primary workflows the restore
    # just causes churn — each modal open/close pair would reflow the
    # buffer twice (down to modal, back up to "original"), and reflows
    # during streaming are what produce duplicated table fragments in
    # Claude's scrollback. Holding the pane at periscope's size means
    # subsequent opens at the same width are no-ops on tmux's side.
    def set_pane_size(c: int, r: int) -> None:
        try:
            tmux("setw", "-t", target, "window-size", "manual")
            tmux("resize-window", "-t", target, "-x", str(c), "-y", str(r))
        except Exception:
            pass

    try:
        # 1) If the client told us up front what cols/rows it can display,
        #    resize tmux to match BEFORE capture-pane. Otherwise the initial
        #    blob is rendered for tmux's current pane width (often a real
        #    terminal also attached to this session at 200+ cols) and xterm
        #    has to reflow it down to the modal's width — which mangles
        #    box-drawing TUIs (Claude's tables, Ink frames) for the first
        #    frame until the next full repaint.
        if cols > 0 and rows > 0:
            await loop.run_in_executor(None, lambda: set_pane_size(cols, rows))

        # 2) Get tmux's view of the pane: size, cursor position, alt-screen
        #    state. We need all three to render the initial blob into an xterm
        #    state that matches what tmux thinks the pane currently looks like.
        #    If we don't, incremental updates from the stream (e.g. "cursor to
        #    row 5 col 15, write '20s'") land at xterm's stale cursor and
        #    leave ghost text from the old buffer.
        try:
            meta = tmux(
                "display-message", "-t", target, "-p",
                "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{alternate_on}",
            ).strip()
            cols_s, rows_s, cx_s, cy_s, alt_s = meta.split("|")
            cols, rows = int(cols_s), int(rows_s)
            cx, cy = int(cx_s), int(cy_s)
            alt_on = alt_s == "1"
        except Exception:
            cols, rows, cx, cy, alt_on = 120, 40, 0, 0, False

        await websocket.send_text(
            json.dumps({"type": "size", "cols": cols, "rows": rows})
        )

        # `-S -N` asks for N lines of scrollback above the visible region;
        # tmux clamps to the pane's own history-limit (default 2000, Tom's
        # tmux is 50k). A few thousand makes the modal genuinely useful for
        # scrolling back through long Claude turns — the prior 200 lines
        # only covered the most recent screen-ful or two.
        initial = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-10000")
        # capture-pane separates lines with \n AND appends one more \n after
        # the final line. We strip exactly that final terminator (not any
        # blank-line content above it) and convert internal \n to \r\n so
        # xterm wraps each new line back to column 0 instead of staircasing.
        # If we strip too many, blank lines at the bottom of the pane vanish
        # and the cursor lands one row above where tmux says it is. If we
        # strip too few, the trailing \r\n scrolls xterm one row past the
        # bottom and the cursor lands one row below.
        if initial:
            if initial.endswith("\n"):
                initial = initial[:-1]
            body = initial.replace("\n", "\r\n")
        else:
            body = ""
        # Build a prefix that puts xterm into the same screen mode tmux is in,
        # clears any stale rendering, then a suffix that parks the cursor
        # where tmux thinks it is.
        prefix = ""
        if alt_on:
            prefix += "\x1b[?1049h"  # enter alt-screen buffer
        prefix += "\x1b[2J\x1b[H"     # clear screen, home cursor
        # ANSI cursor positioning is 1-indexed; tmux's #{cursor_x/y} are 0-indexed.
        suffix = f"\x1b[{cy + 1};{cx + 1}H"
        blob = (prefix + body + suffix).encode("utf-8", errors="replace")
        await websocket.send_bytes(blob)

        # 2) Set up the pipe. mkfifo + open(O_RDONLY|O_NONBLOCK) returns
        #    immediately; tmux opens the write end via the cat subprocess.
        os.mkfifo(fifo_path)
        tmux("pipe-pane", "-O", "-t", target, f"cat > {fifo_path}")
        pipe_active = True
        fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)

        # 3) Bridge FIFO → queue → websocket. asyncio.add_reader notifies us
        #    when the fd is readable; we drain in non-blocking chunks.
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable():
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                return
            if chunk:
                out_queue.put_nowait(chunk)

        loop.add_reader(fd, on_readable)

        async def forward_out():
            while True:
                chunk = await out_queue.get()
                await websocket.send_bytes(chunk)

        forward_task = _task(forward_out(), "ws-forward")

        # 4) Main loop: receive keystrokes from the client and push to tmux.
        #    xterm.js's onData sends raw input including escape sequences
        #    (e.g. "\x1b[A" for up arrow). We deliver them via load-buffer +
        #    paste-buffer rather than send-keys -l because tmux's argv parser
        #    treats a standalone ";" as a command separator — typing a single
        #    semicolon would otherwise be silently dropped.
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
                # Resize control message: client measured how many cols/rows
                # fit in its modal and asks tmux to match. Sent as JSON text
                # ({"type":"resize","cols":N,"rows":M}). Plain keystrokes are
                # always non-JSON, so the json.loads path filters them out.
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
                        continue
                await loop.run_in_executor(
                    None, lambda t=text: deliver_input(target, t)
                )
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
    finally:
        # Cleanup in reverse setup order. Each step is best-effort because
        # any of them could fail mid-teardown if the pane already died.
        # We intentionally do NOT restore the window size here — see
        # set_pane_size above for why periscope holds the pane.
        if pipe_active:
            try:
                tmux("pipe-pane", "-t", target)  # no command = stop piping
            except Exception:
                pass
        if fd is not None:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass
        if os.path.exists(fifo_path):
            try:
                os.unlink(fifo_path)
            except Exception:
                pass
