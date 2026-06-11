"""tmux_mirror — control-mode pane mirroring with self-healing reconciliation.

Transport: one `tmux -C attach` client per session being viewed; `%output`
notifications carry pane bytes (octal-escaped, line-framed) — no FIFO, no
attach gap. Convergence: after output quiesces (or at a max interval during
sustained streaming), an authoritative repaint frame built from tmux's own
grid (`capture-pane`) is pushed to subscribers. Frames are blindly
idempotent — any mirror desync, from any cause, heals at the next frame.

Ordering rule (load-bearing): reconcile frames are built and enqueued
synchronously inside the reader task's handling of a reply's final `%end`
line. A future-based wait would let `%output` arriving after the reply
reach subscriber queues before the woken task enqueues its frame, making
the frame revert newer output. Design spec:
docs/superpowers/specs/2026-06-10-terminal-mirror-reconciliation-design.md
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Callable

from periscope.log import log, _task

QUIESCE_S = 0.15        # reconcile this long after output stops
MAX_INTERVAL_S = 1.0    # ... or at least this often during sustained output
LINGER_S = 20.0         # keep a session client alive after its last viewer
                        # (rail navigation unmounts before remounting)
STREAM_LIMIT = 1 << 20  # asyncio readline limit; %output burst lines can
                        # exceed the 64KB default

# Integration tests monkeypatch this to target a dedicated `-L` server.
_TMUX: tuple[str, ...] = ("tmux",)


@dataclass(frozen=True)
class Output:
    pane_id: str
    raw: bytes  # still octal-escaped — decode_octal() before use


@dataclass(frozen=True)
class Reply:
    body: tuple[bytes, ...]


@dataclass(frozen=True)
class ReplyError:
    body: tuple[bytes, ...]


@dataclass(frozen=True)
class LayoutChange:
    window_id: str


@dataclass(frozen=True)
class Exit:
    pass


class ControlParser:
    """Line-in, event-out parser for tmux control-mode stdout.

    Reply blocks accumulate statefully; the %end/%error line must echo
    %begin's <time> <number> tokens — a reply *body* line may itself start
    with "%end", so a prefix match is not sufficient.
    """

    def __init__(self) -> None:
        self._block_tokens: tuple[bytes, bytes] | None = None
        self._block_lines: list[bytes] = []

    def feed_line(
        self, line: bytes
    ) -> Output | Reply | ReplyError | LayoutChange | Exit | None:
        if self._block_tokens is not None:
            for marker, cls in ((b"%end ", Reply), (b"%error ", ReplyError)):
                if line.startswith(marker):
                    parts = line.split(b" ")
                    if tuple(parts[1:3]) == self._block_tokens:
                        body = tuple(self._block_lines)
                        self._block_tokens = None
                        self._block_lines = []
                        return cls(body=body)
            self._block_lines.append(line)
            return None
        if line.startswith(b"%output "):
            # %output %<pane> <payload>; payload may contain spaces.
            parts = line.split(b" ", 2)
            return Output(
                pane_id=parts[1].decode(),
                raw=parts[2] if len(parts) == 3 else b"",
            )
        if line.startswith(b"%begin "):
            parts = line.split(b" ")
            self._block_tokens = (parts[1], parts[2])
            self._block_lines = []
            return None
        if line.startswith(b"%layout-change "):
            return LayoutChange(window_id=line.split(b" ")[1].decode())
        if line == b"%exit" or line.startswith(b"%exit "):
            return Exit()
        return None  # unknown notification — forward-compatible ignore


def decode_octal(raw: bytes) -> bytes:
    """Decode tmux control-mode escaping: `\\ooo` — exactly three octal
    digits (`\\033` ESC, `\\134` backslash). Valid UTF-8 passes through
    unescaped (verified vs tmux 3.6a). Bytes in, bytes out — multibyte
    sequences may split across %output notifications, so never decode to
    str here."""
    if b"\\" not in raw:
        return raw
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C and i + 4 <= n:
            d1, d2, d3 = raw[i + 1], raw[i + 2], raw[i + 3]
            if 0x30 <= d1 <= 0x37 and 0x30 <= d2 <= 0x37 and 0x30 <= d3 <= 0x37:
                out.append(((d1 - 0x30) << 6) | ((d2 - 0x30) << 3) | (d3 - 0x30))
                i += 4
                continue
        out.append(c)
        i += 1
    return bytes(out)


@dataclass(frozen=True, kw_only=True)
class GridSnapshot:
    rows: tuple[bytes, ...]   # capture-pane -e body lines (raw — reply
                              # bodies are NOT octal-escaped, verified)
    height: int
    cursor_x: int             # 0-indexed, as tmux reports
    cursor_y: int
    alt_on: bool
    cursor_visible: bool


# Queried per reconcile; pane_height is included so the frame's row loop
# tracks resizes without trusting a stale subscribe-time value.
DISPLAY_FMT = "#{pane_height}|#{cursor_x}|#{cursor_y}|#{alternate_on}|#{cursor_flag}"


def snapshot_from_replies(capture: Reply, display: Reply) -> GridSnapshot:
    height, cx, cy, alt, cvis = display.body[0].decode().split("|")
    return GridSnapshot(
        rows=capture.body,
        height=int(height),
        cursor_x=int(cx),
        cursor_y=int(cy),
        alt_on=alt == "1",
        cursor_visible=cvis == "1",
    )


def build_reconcile_frame(snap: GridSnapshot) -> bytes:
    """One blindly-idempotent repaint — correct regardless of client state.

    The 1..height row loop is the coverage mechanism: every cell gets
    overwritten. (xterm.js gates DECSET 1049 on an actual buffer change —
    re-entry is a no-op and clears nothing, so the mode prefix only fixes
    a missed screen *switch*.) Normal-screen frames lead with 1049l to
    heal the stuck-in-alt class and never emit 2J: scrollback stays
    untouched. One frame = one atomic WS message = no flicker.
    """
    parts = [b"\x1b[?1049h" if snap.alt_on else b"\x1b[?1049l"]
    rows = list(snap.rows[: snap.height])
    rows += [b""] * (snap.height - len(rows))
    for i, row in enumerate(rows):
        parts.append(b"\x1b[%d;1H" % (i + 1) + row + b"\x1b[0m\x1b[K")
    parts.append(b"\x1b[%d;%dH" % (snap.cursor_y + 1, snap.cursor_x + 1))
    parts.append(b"\x1b[?25h" if snap.cursor_visible else b"\x1b[?25l")
    return b"".join(parts)
