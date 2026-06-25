"""Tests for WS /ws/pane.

The handler consumes periscope.tmux_mirror's Subscription; tests patch
the subscribe boundary with a fake (the only mocked seam — parsing,
frames, and timing are unit-tested in tests/test_tmux_mirror.py) and
fake_tmux for the fork-path display-message/capture-pane calls.
"""

import asyncio
import json

import pytest


class FakeSubscription:
    def __init__(self):
        self.q = asyncio.Queue()
        self.loop = None        # captured in fake_subscribe (app's loop)
        self.reconciles = 0
        self.exited = False

    def push(self, chunk):
        # TestClient runs the app loop in another thread; a plain
        # put_nowait from the test thread rides on anyio internals.
        self.loop.call_soon_threadsafe(self.q.put_nowait, chunk)

    def request_reconcile(self):
        self.reconciles += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.exited = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self.q.get()
        if chunk is None:
            raise StopAsyncIteration
        return chunk


def _patch_mirror(mocker):
    sub = FakeSubscription()

    async def fake_subscribe(session, pane_id):
        sub.session, sub.pane_id = session, pane_id
        sub.loop = asyncio.get_running_loop()
        return sub

    mocker.patch("periscope.routes.ws.tmux_mirror.subscribe",
                 side_effect=fake_subscribe)
    return sub


def _fake_tmux(calls=None):
    def fake(*args):
        if calls is not None:
            calls.append(args)
        if args and args[0] == "display-message":
            return "80|24|0|0|0|%7"
        if args and args[0] == "capture-pane":
            return "hello\n"
        return ""
    return fake


def test_ws_pane_initial_paint(client, mocker):
    """Connect → size frame + initial paint blob arrive, in that order."""
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?session=main&index=0") as ws:
        payload = json.loads(ws.receive_text())
        assert payload == {"type": "size", "cols": 80, "rows": 24}
        blob = ws.receive_bytes()
        assert b"hello" in blob
        assert b"\x1b[2J" in blob  # initial paint still clears before body

    assert sub.session == "main"
    assert sub.pane_id == "%7"
    assert sub.exited  # disconnect unsubscribed


def test_ws_pane_resizes_tmux_before_capture(client, mocker):
    """Connect-time cols/rows hint triggers resize-window before
    capture-pane — the initial-paint width-race fix. Kept verbatim in
    spirit from the FIFO era; the ordering invariant is transport-
    independent."""
    calls: list[tuple] = []
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux(calls))
    _patch_mirror(mocker)

    with client.websocket_connect(
        "/ws/pane?session=main&index=0&cols=100&rows=30"
    ) as ws:
        _ = ws.receive_text()
        _ = ws.receive_bytes()

    op_seq = [c[0] for c in calls]
    assert "resize-window" in op_seq, f"no resize-window in {op_seq}"
    assert "capture-pane" in op_seq
    assert op_seq.index("resize-window") < op_seq.index("capture-pane")

    resize = next(c for c in calls if c[0] == "resize-window")
    assert "-x" in resize and "100" in resize
    assert "-y" in resize and "30" in resize

    # Periscope holds the pane size after disconnect — no restore.
    resizes = [c for c in calls if c[0] == "resize-window"]
    assert len(resizes) == 1


def test_ws_streams_subscription_bytes(client, mocker):
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?session=main&index=0") as ws:
        _ = ws.receive_text()
        _ = ws.receive_bytes()
        sub.push(b"live-bytes")
        assert ws.receive_bytes() == b"live-bytes"


def test_ws_closes_on_subscription_eof(client, mocker):
    """EOF (pane/mirror died) closes the websocket — feeds the client's
    reconnect FSM instead of going silent like the old dead FIFO."""
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?session=main&index=0") as ws:
        _ = ws.receive_text()
        _ = ws.receive_bytes()
        sub.push(None)  # EOF sentinel
        with pytest.raises(Exception):  # noqa: B017 — starlette's closed-ws exception type is an internal detail
            ws.receive_bytes()


def test_ws_resize_message_triggers_reconcile(client, mocker):
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?session=main&index=0") as ws:
        _ = ws.receive_text()
        _ = ws.receive_bytes()
        ws.send_text(json.dumps({"type": "resize", "cols": 90, "rows": 30}))
        # Resize handling is async; poll briefly for the side effect.
        for _i in range(50):
            if sub.reconciles:
                break
            import time
            time.sleep(0.02)
        assert sub.reconciles == 1
