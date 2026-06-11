"""tmux_mirror — control-mode mirroring with self-healing reconciliation.

Unit layers (parser / decoder / frames / timer) need zero tmux. The
integration tests at the bottom drive a real tmux server on a dedicated
socket and verify the design's thesis: the mirrored stream converges to
tmux's own grid even when bytes are dropped.
"""

import pytest

from periscope.tmux_mirror import (
    ControlParser, Output, Reply, ReplyError, LayoutChange, Exit,
    decode_octal,
    GridSnapshot, build_reconcile_frame, snapshot_from_replies,
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
    base = dict(rows=(b"row1", b"row2"), height=2, cursor_x=3, cursor_y=1,
                alt_on=False, cursor_visible=True)
    base.update(kw)
    from periscope.tmux_mirror import GridSnapshot
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
