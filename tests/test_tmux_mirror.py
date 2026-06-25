"""tmux_mirror — control-mode mirroring with self-healing reconciliation.

Unit layers (parser / decoder / frames / timer) need zero tmux. The
integration tests at the bottom drive a real tmux server on a dedicated
socket and verify the design's thesis: the mirrored stream converges to
tmux's own grid even when bytes are dropped.
"""

import asyncio
import shutil
import subprocess
import uuid

import pytest

import periscope.tmux_mirror as tmux_mirror
from periscope.tmux_mirror import (
    QUIESCE_S,
    ControlParser,
    Exit,
    GridSnapshot,
    LayoutChange,
    Output,
    ReconcileTimer,
    Reply,
    ReplyError,
    _SessionMirror,
    build_reconcile_frame,
    decode_octal,
    snapshot_from_replies,
)

# --- ControlParser ---

def test_output_event_payload_split():
    p = ControlParser()
    ev = p.feed_line(rb"%output %26 hello \033[31m world")
    assert ev == Output(pane_id="%26", raw=rb"hello \033[31m world")


def test_output_event_empty_payload():
    p = ControlParser()
    assert p.feed_line(b"%output %26 ") == Output(pane_id="%26", raw=b"")


def test_reply_block_accumulates_body():
    p = ControlParser()
    assert p.feed_line(b"%begin 111 22 1") is None
    assert p.feed_line(b"row one") is None
    assert p.feed_line(b"row two") is None
    ev = p.feed_line(b"%end 111 22 1")
    assert ev == Reply(body=(b"row one", b"row two"))


def test_end_requires_matching_begin_tokens():
    # A capture BODY line can legitimately start with "%end" (a pane
    # showing control-mode logs). Only the line echoing %begin's
    # <time> <number> tokens terminates the block.
    p = ControlParser()
    p.feed_line(b"%begin 111 22 1")
    assert p.feed_line(b"%end 999 88 1") is None     # body line, wrong tokens
    ev = p.feed_line(b"%end 111 22 1")
    assert ev == Reply(body=(b"%end 999 88 1",))


def test_error_block():
    p = ControlParser()
    p.feed_line(b"%begin 111 22 1")
    p.feed_line(b"can't find pane: %99")
    ev = p.feed_line(b"%error 111 22 1")
    assert ev == ReplyError(body=(b"can't find pane: %99",))


def test_notifications_between_blocks():
    p = ControlParser()
    assert p.feed_line(b"%layout-change @5 b25d,80x24,0,0,0 ...") == LayoutChange(window_id="@5")
    assert isinstance(p.feed_line(b"%exit"), Exit)
    assert p.feed_line(b"%session-changed $344 foo") is None   # unknown → ignored
    assert p.feed_line(b"%unknown-future-notification x") is None


# --- decode_octal ---

def test_decode_octal_escapes():
    assert decode_octal(rb"\033[31mRED\033[0m") == b"\x1b[31mRED\x1b[0m"
    assert decode_octal(rb"a\011b\015\012") == b"a\tb\r\n"


def test_decode_octal_backslash_is_134():
    # tmux escapes backslash as \134 (vis octal), not as doubled backslash.
    assert decode_octal(rb"\134033") == b"\\033"


def test_decode_passthrough():
    # Valid UTF-8 passes through unescaped (verified vs tmux 3.6a); a
    # backslash not followed by 3 octal digits is literal.
    snake = "🐍".encode()
    assert decode_octal(snake) == snake
    assert decode_octal(b"a\\9z") == b"a\\9z"
    assert decode_octal(b"tail\\") == b"tail\\"


def test_decode_split_multibyte_concatenates():
    # UTF-8 may split across %output notifications; byte-level decode of
    # each half must concatenate into a valid codepoint.
    raw = "🐍".encode()
    half = decode_octal(raw[:2]) + decode_octal(raw[2:])
    assert half.decode() == "🐍"


# --- frame builder ---

def _snap(**kw):
    base = {"rows": (b"row1", b"row2"), "height": 2, "cursor_x": 3, "cursor_y": 1,
                "alt_on": False, "cursor_visible": True}
    base.update(kw)
    return GridSnapshot(**base)


def test_frame_normal_screen_exact_bytes():
    frame = build_reconcile_frame(_snap())
    assert frame == (
        b"\x1b[?1049l"                      # heal stuck-in-alt; no-op if normal
        b"\x1b[1;1Hrow1\x1b[0m\x1b[K"
        b"\x1b[2;1Hrow2\x1b[0m\x1b[K"
        b"\x1b[2;4H"                        # cursor 0-indexed (3,1) → 1-indexed (4,2)
        b"\x1b[?25h"
    )


def test_frame_normal_screen_never_clears():
    # 2J would be visually harmless but J-class erases are banned on the
    # normal screen as a guard rail: scrollback must not be touched.
    frame = build_reconcile_frame(_snap())
    assert b"\x1b[2J" not in frame


def test_frame_alt_screen_prefix():
    frame = build_reconcile_frame(_snap(alt_on=True))
    assert frame.startswith(b"\x1b[?1049h")
    assert b"\x1b[?1049l" not in frame


def test_frame_pads_short_capture_to_height():
    # The row loop is the coverage mechanism (xterm.js treats 1049h
    # re-entry as a no-op — it clears nothing). Every row to height must
    # be written even if capture returned fewer lines.
    frame = build_reconcile_frame(_snap(rows=(b"only",), height=3))
    assert b"\x1b[2;1H\x1b[0m\x1b[K" in frame
    assert b"\x1b[3;1H\x1b[0m\x1b[K" in frame


def test_frame_cursor_hidden():
    assert build_reconcile_frame(_snap(cursor_visible=False)).endswith(b"\x1b[?25l")


def test_snapshot_from_replies():
    cap = Reply(body=(b"r1", b"r2"))
    disp = Reply(body=(b"2|3|1|0|1",))     # height|cx|cy|alt|cursor_flag
    snap = snapshot_from_replies(cap, disp)
    assert snap.rows == (b"r1", b"r2")
    assert snap.height == 2
    assert (snap.cursor_x, snap.cursor_y) == (3, 1)
    assert snap.alt_on is False
    assert snap.cursor_visible is True


# --- ReconcileTimer ---
# The deadline math is the unit under test; _arm is patched to record the
# computed delay so no event loop is needed.

def _timer_with_recorder(t0=0.0):
    clock = [t0]
    delays = []
    timer = ReconcileTimer(fire=lambda: None, now=lambda: clock[0])
    timer._arm = lambda d: delays.append(d)
    return timer, clock, delays


def test_quiesce_rearms_on_each_output():
    timer, clock, delays = _timer_with_recorder()
    timer.note_output()
    clock[0] = 0.10
    timer.note_output()
    assert delays == [pytest.approx(QUIESCE_S), pytest.approx(QUIESCE_S)]


def test_max_interval_caps_sustained_streaming():
    timer, clock, delays = _timer_with_recorder()
    timer.note_output()                  # streaming_since = 0.0
    clock[0] = 0.95
    timer.note_output()                  # quiesce→1.10 capped to max→1.0
    assert delays[-1] == pytest.approx(0.05)


def test_reconcile_resets_streaming_window():
    timer, clock, delays = _timer_with_recorder()
    timer.note_output()
    clock[0] = 0.95
    timer.note_reconciled()              # window resets
    timer.note_output()                  # streaming_since = 0.95 → plain quiesce
    assert delays[-1] == pytest.approx(QUIESCE_S)


def test_request_fires_immediately():
    timer, clock, delays = _timer_with_recorder()
    timer.request()
    assert delays == [0.0]


# --- _SessionMirror dispatch (no subprocess: feed events into _dispatch) ---

def test_dispatch_output_routes_decoded_bytes_to_subscribers():
    async def drive():
        m = _SessionMirror("sess")
        sub = m.subscribe("%7")
        m._dispatch(Output(pane_id="%7", raw=rb"hi\033[m"))
        m._dispatch(Output(pane_id="%99", raw=b"other-pane"))  # cheap skip
        assert sub._q.get_nowait() == b"hi\x1b[m"
        assert sub._q.empty()
    asyncio.run(drive())


def test_dispatch_reply_with_empty_callback_queue_is_dropped():
    # The attach handshake emits an unsolicited %begin/%end block.
    async def drive():
        m = _SessionMirror("sess")
        m._dispatch(Reply(body=()))        # must not raise
    asyncio.run(drive())


def test_reconcile_frame_enqueued_synchronously_on_display_reply():
    # The ordering rule: after the display reply dispatches, the frame is
    # already in the queue — no task switch in between.
    async def drive():
        m = _SessionMirror("sess")
        m._proc = object()   # _fire_reconcile guards on a live client
        sent = []
        m._send_command = lambda cmd, cb: (sent.append(cmd), m._reply_callbacks.append(cb))
        sub = m.subscribe("%7")
        m._fire_reconcile("%7")
        assert sent[0].startswith("capture-pane -p -e -t %7")
        assert sent[1].startswith("display-message -p -t %7")
        m._dispatch(Reply(body=(b"row",)))                # capture reply
        m._dispatch(Reply(body=(b"1|0|0|0|1",)))          # display reply
        frame = sub._q.get_nowait()
        assert frame.startswith(b"\x1b[?1049l")
        assert b"row" in frame
    asyncio.run(drive())


def test_reply_error_ends_pane_subscriptions():
    async def drive():
        m = _SessionMirror("sess")
        m._proc = object()   # _fire_reconcile guards on a live client
        m._send_command = lambda cmd, cb: m._reply_callbacks.append(cb)
        sub = m.subscribe("%7")
        m._fire_reconcile("%7")
        m._dispatch(ReplyError(body=(b"can't find pane",)))
        m._dispatch(ReplyError(body=(b"can't find pane",)))
        assert sub._q.get_nowait() is None                # EOF sentinel
    asyncio.run(drive())


def test_layout_change_requests_reconcile_for_subscribed_panes():
    async def drive():
        m = _SessionMirror("sess")
        m.subscribe("%7")
        fired = []
        m._timers["%7"].request = lambda: fired.append(True)
        m._dispatch(LayoutChange(window_id="@1"))
        assert fired == [True]
    asyncio.run(drive())


def test_read_loop_drops_attach_handshake_before_queued_replies():
    # The connect-time reconcile usually beats the attach handshake to the
    # callback queue, so the handshake block arrives with callbacks already
    # queued — the reader must drop that first block or every callback
    # shifts off by one (the integration oracle caught this live).
    async def drive():
        m = _SessionMirror("sess")
        lines = [
            b"%begin 1 0 0\n", b"%end 1 0 0\n",          # attach handshake
            b"%begin 2 1 1\n", b"real\n", b"%end 2 1 1\n",
            b"",                                          # EOF
        ]

        class FakeStdout:
            async def readline(self):
                return lines.pop(0)

        class FakeProc:
            stdout = FakeStdout()
            stdin = None
            returncode = 0

        m._proc = FakeProc()
        got = []
        m._reply_callbacks.append(got.append)
        await m._read_loop()
        assert got == [Reply(body=(b"real",))]
    asyncio.run(drive())


def test_unsubscribe_last_viewer_arms_linger():
    async def drive():
        m = _SessionMirror("sess")
        m._proc = object()   # pretend a client is running (linger guard)
        sub = m.subscribe("%7")
        async with sub:
            pass
        assert m._linger is not None
        m._linger.cancel()
    asyncio.run(drive())


# --- integration: real tmux, pyte as the client-side oracle ---

# Unique per run: concurrent suites (parallel worktrees/agents) on a fixed
# socket name kill each other's test servers via the finally kill-server,
# surfacing as phantom integration failures.
TEST_SOCKET = f"periscope-mirror-test-{uuid.uuid4().hex[:8]}"
needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def _t(*args):
    # -f /dev/null: the first _t() call boots a fresh server on the test
    # socket, which would otherwise source ~/.tmux.conf — on a host with
    # tmux-continuum restore-on-start, that resurrects the user's entire
    # real session layout (claude processes included) onto the test server.
    return subprocess.run(
        ["tmux", "-L", TEST_SOCKET, "-f", "/dev/null", *args],
        capture_output=True, text=True, check=False,
    ).stdout


def _mk_session():
    name = f"mtest-{uuid.uuid4().hex[:8]}"
    # /bin/sh, not the login shell: zsh prompt segments can repaint AFTER
    # the last reconcile frame and break the final grid comparison.
    _t("new-session", "-d", "-s", name, "-x", "80", "-y", "24", "/bin/sh")
    pane_id = _t("display-message", "-p", "-t", name, "#{pane_id}").strip()
    return name, pane_id


def _feed_pyte(screen, chunks):
    import pyte
    stream = pyte.ByteStream(screen)
    for c in chunks:
        stream.feed(c)


def _grids_equal(screen, pane_id):
    cap = _t("capture-pane", "-p", "-t", pane_id).split("\n")
    cap = cap[:24] + [""] * (24 - len(cap[:24]))
    pyte_rows = [row.rstrip() for row in screen.display]
    return pyte_rows == [r.rstrip() for r in cap]


async def _drain(sub, seconds):
    """Collect every chunk the subscription yields within a window."""
    chunks = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        timeout = deadline - loop.time()
        if timeout <= 0:
            return chunks
        try:
            chunks.append(await asyncio.wait_for(sub.__anext__(), timeout))
        except (TimeoutError, StopAsyncIteration):
            return chunks


@needs_tmux
def test_oracle_mirror_matches_tmux_grid(monkeypatch):
    monkeypatch.setattr(tmux_mirror, "_TMUX", ("tmux", "-L", TEST_SOCKET))
    import pyte
    name, pane_id = _mk_session()

    async def drive():
        sub = await tmux_mirror.subscribe(name, pane_id)
        async with sub:
            _t("send-keys", "-t", pane_id,
               "printf '\\033[31mRED\\033[0m line\\n'; seq 1 30", "Enter")
            chunks = await _drain(sub, 2.0)   # includes ≥1 reconcile frame
            screen = pyte.Screen(80, 24)
            _feed_pyte(screen, chunks)
            assert _grids_equal(screen, pane_id), (
                f"pyte:\n{chr(10).join(screen.display)}")
        await tmux_mirror.shutdown()

    try:
        asyncio.run(drive())
    finally:
        _t("kill-server")


@needs_tmux
def test_thesis_byte_drops_converge_after_reconcile(monkeypatch):
    """The design's reason to exist, as an assertion: drop chunks from the
    relay, and the grid still converges because a reconcile frame is
    blindly idempotent."""
    monkeypatch.setattr(tmux_mirror, "_TMUX", ("tmux", "-L", TEST_SOCKET))
    import pyte
    name, pane_id = _mk_session()

    async def drive():
        sub = await tmux_mirror.subscribe(name, pane_id)
        async with sub:
            _t("send-keys", "-t", pane_id, "seq 100 140", "Enter")
            burst = await _drain(sub, 1.0)
            # The burst contains reconcile frames too (connect-time +
            # quiesce). Drop deterministically and grid-affectingly:
            # exclude ALL frames (they start with the 1049 mode prefix),
            # then drop the LAST output chunk — it carries the final
            # lines/prompt, which are on the final grid. Index-mod
            # dropping would flake: a surviving frame self-heals the
            # control, and early drops only lose lines that scroll away.
            outs = [c for c in burst if not c.startswith(b"\x1b[?1049")]
            kept = outs[:-1]
            sub.request_reconcile()
            heal = await _drain(sub, 1.0)
            screen = pyte.Screen(80, 24)
            _feed_pyte(screen, kept + heal)
            assert _grids_equal(screen, pane_id)

            # Control: without the healing frames, the dropped tail DOES
            # corrupt — proving the reconcile is what fixes it.
            corrupt = pyte.Screen(80, 24)
            _feed_pyte(corrupt, kept)
            assert not _grids_equal(corrupt, pane_id)
        await tmux_mirror.shutdown()

    try:
        asyncio.run(drive())
    finally:
        _t("kill-server")


@needs_tmux
def test_two_subscribers_multiplex(monkeypatch):
    monkeypatch.setattr(tmux_mirror, "_TMUX", ("tmux", "-L", TEST_SOCKET))
    name, pane_id = _mk_session()

    async def drive():
        a = await tmux_mirror.subscribe(name, pane_id)
        b = await tmux_mirror.subscribe(name, pane_id)
        assert len(tmux_mirror._MIRRORS) == 1     # one client, two subs
        _t("send-keys", "-t", pane_id, "echo multiplexed", "Enter")
        ca = b"".join(await _drain(a, 1.0))
        cb = b"".join(await _drain(b, 1.0))
        assert b"multiplexed" in ca and b"multiplexed" in cb
        async with a, b:
            pass
        await tmux_mirror.shutdown()

    try:
        asyncio.run(drive())
    finally:
        _t("kill-server")
