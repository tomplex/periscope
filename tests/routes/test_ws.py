"""Tests for WS /ws/pane.

The WS handler creates a FIFO, calls tmux pipe-pane, and bridges a
non-blocking fd into a websocket. None of that works in pytest without
a live tmux and writable /tmp, so the test mocks the IO surface and
verifies the initial size frame + initial paint blob are sent before
disconnecting.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server import app
    return TestClient(app)


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
