"""Tests for WS /ws/pane.

The WS handler creates a FIFO, calls tmux pipe-pane, and bridges a
non-blocking fd into a websocket. None of that works in pytest without
a live tmux and writable /tmp, so the test mocks the IO surface and
verifies the initial size frame + initial paint blob are sent before
disconnecting.
"""

import json


def test_ws_pane_initial_paint(client, mocker):
    """Connect, receive size frame + initial paint blob, then disconnect."""
    # tmux is called from two places at the top of the handler:
    # 1) display-message → "cols|rows|cx|cy|alt_on"
    # 2) capture-pane    → raw screen bytes
    # 3) pipe-pane       → "" (sets up the FIFO bridge)
    def fake_tmux(*args):
        if args and args[0] == "display-message":
            return "80|24|0|0|0"
        if args and args[0] == "capture-pane":
            return "hello\n"
        return ""

    # The handler imports `tmux` and `deliver_input` from periscope.tmux at
    # module top. After Peel 8a moves the route to periscope.routes.ws,
    # patch there. For step-1 (route still in server.py), patch on server.
    for path in ("periscope.routes.ws.tmux", "server.tmux"):
        try:
            mocker.patch(path, side_effect=fake_tmux)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    # Bridge plumbing: skip the real FIFO + fd dance.
    mocker.patch("os.mkfifo")
    mocker.patch("os.open", return_value=42)
    mocker.patch("os.read", side_effect=BlockingIOError)
    mocker.patch("os.close")
    mocker.patch("os.path.exists", return_value=False)
    # add_reader/remove_reader are loop-instance methods; patch on the
    # selector event loop class so all loops inherit the no-op.
    mocker.patch("asyncio.selector_events.BaseSelectorEventLoop.add_reader")
    mocker.patch("asyncio.selector_events.BaseSelectorEventLoop.remove_reader")

    with client.websocket_connect("/ws/pane?session=main&index=0") as ws:
        # First message: size frame as JSON text.
        msg = ws.receive_text()
        payload = json.loads(msg)
        assert payload == {"type": "size", "cols": 80, "rows": 24}

        # Second message: initial paint blob as bytes.
        blob = ws.receive_bytes()
        # We sent capture-pane = "hello\n"; the handler strips the final \n,
        # so the body contains "hello" sandwiched between clear-screen and
        # cursor-park escape sequences.
        assert b"hello" in blob
        assert b"\x1b[2J" in blob  # clear screen


def test_ws_pane_resizes_tmux_before_capture(client, mocker):
    """Connect-time cols/rows hint triggers resize-window before capture-pane.

    This is the fix for the initial-paint width race: without it, tmux's
    pane is captured at whatever width a real attached terminal set, then
    xterm has to reflow the body down to the modal's width — which mangles
    box-drawing TUIs for the first frame.
    """
    calls: list[tuple] = []

    def fake_tmux(*args):
        calls.append(args)
        if args and args[0] == "display-message":
            # Pretend tmux honored the resize: report back 100x30.
            if "#{pane_width}|#{pane_height}|" in args[-1]:
                return "100|30|0|0|0"
            if "#{window_width}" in args[-1]:
                return "200 60"
            return ""
        if args and args[0] == "capture-pane":
            return "hello\n"
        if args and args[0] == "show-option":
            return "latest"
        return ""

    for path in ("periscope.routes.ws.tmux", "server.tmux"):
        try:
            mocker.patch(path, side_effect=fake_tmux)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    mocker.patch("os.mkfifo")
    mocker.patch("os.open", return_value=42)
    mocker.patch("os.read", side_effect=BlockingIOError)
    mocker.patch("os.close")
    mocker.patch("os.path.exists", return_value=False)
    mocker.patch("asyncio.selector_events.BaseSelectorEventLoop.add_reader")
    mocker.patch("asyncio.selector_events.BaseSelectorEventLoop.remove_reader")

    with client.websocket_connect("/ws/pane?session=main&index=0&cols=100&rows=30") as ws:
        _ = ws.receive_text()
        _ = ws.receive_bytes()

    # Verify ordering: the resize-window call must happen before capture-pane.
    # If we capture first, the body is at the wrong width and the whole fix
    # is moot.
    op_seq = [c[0] for c in calls]
    assert "resize-window" in op_seq, f"no resize-window in {op_seq}"
    assert "capture-pane" in op_seq
    assert op_seq.index("resize-window") < op_seq.index("capture-pane")

    # And the resize was for the hinted dims.
    resize = next(c for c in calls if c[0] == "resize-window")
    assert "-x" in resize and "100" in resize
    assert "-y" in resize and "30" in resize

    # Periscope holds the pane size after disconnect — no restore.
    # If we ever start save+restoring again, the resize-window count would
    # be 2 (forward + restore) and the second one would carry different dims.
    resizes = [c for c in calls if c[0] == "resize-window"]
    assert len(resizes) == 1, (
        f"expected exactly one resize-window (no restore), got {resizes}"
    )
