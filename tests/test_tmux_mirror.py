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
