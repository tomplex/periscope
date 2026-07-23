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
        # The handshake chains setw ; resize-window ; display-message into one
        # invocation; real tmux returns the final display-message output for
        # the whole chain, so match the command anywhere in the argv.
        if "display-message" in args:
            # fields: width|height|cx|cy|alt|pane_id|session|window|mouse_any
            return "80|24|0|0|0|%7|main|0|0"
        if "capture-pane" in args:
            return "hello\n"
        return ""
    return fake


def test_ws_pane_initial_paint(client, mocker):
    """Connect → size frame + initial paint blob arrive, in that order."""
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?pane_id=%7") as ws:
        payload = json.loads(ws.receive_text())
        assert payload == {"type": "size", "cols": 80, "rows": 24}
        mouse = json.loads(ws.receive_text())
        assert mouse == {"type": "mouse", "on": False}
        blob = ws.receive_bytes()
        assert b"hello" in blob
        assert b"\x1b[2J" in blob  # initial paint still clears before body

    assert sub.session == "main"
    assert sub.pane_id == "%7"
    assert sub.exited  # disconnect unsubscribed


def test_ws_pane_recency_stamp_keyed_on_session_index(client, mocker):
    """Connect stamps acted_at on the recency map keyed by session:index
    (from display-message), NOT on the %pane_id URL param — window_view
    reads recency_stamps_for(f"{session}:{index}"), so a %N key would be
    a dead write."""
    from periscope.panes import recency_stamps_for

    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?pane_id=%7") as ws:
        _ = ws.receive_text()   # size
        _ = ws.receive_text()   # mouse
        _ = ws.receive_bytes()

    assert recency_stamps_for("main:0")["acted_at"] > 0
    # The pane-id key must NOT have been stamped.
    assert recency_stamps_for("%7")["acted_at"] == 0


def test_ws_pane_resizes_tmux_before_capture(client, mocker):
    """Connect-time cols/rows hint resizes the pane before capture-pane — the
    initial-paint width-race fix. The resize is chained with display-message
    into one tmux invocation (one fork, not two) whose ordering guarantees the
    resize lands before the meta read; capture-pane follows as its own call."""
    calls: list[tuple] = []
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux(calls))
    _patch_mirror(mocker)

    with client.websocket_connect(
        "/ws/pane?pane_id=%7&cols=100&rows=30"
    ) as ws:
        _ = ws.receive_text()   # size
        _ = ws.receive_text()   # mouse
        _ = ws.receive_bytes()

    def call_idx(pred):
        return next(i for i, c in enumerate(calls) if pred(c))

    resize_i = call_idx(lambda c: "resize-window" in c)
    capture_i = call_idx(lambda c: "capture-pane" in c)
    assert resize_i < capture_i

    # The resize rides in the same chained call as the meta read, carrying the
    # client's hinted dims, and there's exactly one of them (periscope holds the
    # pane size after disconnect — no restore).
    resize_calls = [c for c in calls if "resize-window" in c]
    assert len(resize_calls) == 1
    resize = resize_calls[0]
    assert "display-message" in resize
    assert "-x" in resize and "100" in resize
    assert "-y" in resize and "30" in resize


def test_ws_pane_handshake_survives_resize_failure(client, mocker):
    """tmux aborts a `;`-chain at the first failing command, yielding empty
    output. A resize failure has always been non-fatal, so the handler must
    re-ask for the meta alone rather than close the socket on an empty chain."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        # The chained call (contains resize-window) "fails": empty output, as
        # tmux does when an earlier command in the chain errors. A standalone
        # display-message still succeeds.
        if "resize-window" in args:
            return ""
        if "display-message" in args:
            return "80|24|0|0|0|%7|main|0|0"
        if "capture-pane" in args:
            return "hello\n"
        return ""

    mocker.patch("periscope.routes.ws.tmux", side_effect=fake)
    _patch_mirror(mocker)

    with client.websocket_connect(
        "/ws/pane?pane_id=%7&cols=100&rows=30"
    ) as ws:
        assert json.loads(ws.receive_text())["type"] == "size"   # handshake survived
        _ = ws.receive_text()   # mouse
        assert ws.receive_bytes()

    # The chained attempt ran, then a bare display-message recovered the meta.
    assert any("resize-window" in c for c in calls)
    standalone = [c for c in calls if "display-message" in c and "resize-window" not in c]
    assert len(standalone) == 1


def test_ws_streams_subscription_bytes(client, mocker):
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?pane_id=%7") as ws:
        _ = ws.receive_text()   # size
        _ = ws.receive_text()   # mouse
        _ = ws.receive_bytes()
        sub.push(b"live-bytes")
        assert ws.receive_bytes() == b"live-bytes"


def test_ws_closes_on_subscription_eof(client, mocker):
    """EOF (pane/mirror died) closes the websocket — feeds the client's
    reconnect FSM instead of going silent like the old dead FIFO."""
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?pane_id=%7") as ws:
        _ = ws.receive_text()   # size
        _ = ws.receive_text()   # mouse
        _ = ws.receive_bytes()
        sub.push(None)  # EOF sentinel
        with pytest.raises(Exception):  # noqa: B017 — starlette's closed-ws exception type is an internal detail
            ws.receive_bytes()


def test_ws_resize_message_triggers_reconcile(client, mocker):
    mocker.patch("periscope.routes.ws.tmux", side_effect=_fake_tmux())
    sub = _patch_mirror(mocker)

    with client.websocket_connect("/ws/pane?pane_id=%7") as ws:
        _ = ws.receive_text()   # size
        _ = ws.receive_text()   # mouse
        _ = ws.receive_bytes()
        ws.send_text(json.dumps({"type": "resize", "cols": 90, "rows": 30}))
        # Resize handling is async; poll briefly for the side effect.
        for _i in range(50):
            if sub.reconciles:
                break
            import time
            time.sleep(0.02)
        assert sub.reconciles == 1
