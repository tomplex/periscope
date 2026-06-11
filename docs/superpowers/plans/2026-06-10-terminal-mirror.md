# Terminal Mirror Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pipe-pane/FIFO terminal mirror with per-session tmux control-mode clients plus self-healing reconciliation frames, so the xterm.js mirror can never permanently desync from tmux's grid.

**Architecture:** New `periscope/tmux_mirror.py` (control parser → frame builder → reconcile timer → per-session client behind a `subscribe()` registry); `routes/ws.py` keeps its endpoint and wire protocol but consumes a mirror `Subscription` instead of a FIFO; the browser client is untouched. Spec: `docs/superpowers/specs/2026-06-10-terminal-mirror-reconciliation-design.md`. Structure: `docs/superpowers/specs/2026-06-10-terminal-mirror-structure.md`.

**Tech Stack:** Python 3.11 asyncio, tmux 3.6a control mode (`-C attach`), FastAPI WebSocket, pytest (+pyte as a test-only oracle).

**Empirical facts already verified on this machine (do NOT re-litigate):**
- `tmux -C attach -t <sess> -f ignore-size,read-only` is accepted by the installed tmux 3.6a.
- `%output` payloads are octal-escaped: `\033` for ESC, `\011` TAB, `\015\012` CRLF, **`\134` for backslash** (vis(3) octal style — NOT `\\`). Valid UTF-8 multibyte passes through *unescaped*.
- Reply bodies (`%begin`…`%end` blocks, e.g. `capture-pane -e` output) are **raw** — real ESC/TAB bytes, no octal escaping.
- The attach handshake emits one unsolicited `%begin/%end` block (no command issued) — reply dispatch must tolerate an empty callback queue.
- `#{cursor_flag}` exists (1 = cursor visible).
- A control client receives `%output` only for panes of its attached session.
- `capture-pane -p` returns exactly `pane_height` lines including trailing blanks.

---

### Task 0: Worktree setup

Periscope's `main` is live (prod runs from this checkout; launchd respawns on crash) and other sessions merge to it concurrently. All work happens in a worktree.

**Files:** none (setup only)

- [ ] **Step 1: Create worktree + branch**

```bash
git -C ~/dev/periscope worktree add ~/dev/periscope-mirror -b feature/terminal-mirror
cd ~/dev/periscope-mirror
```

- [ ] **Step 2: Confirm baseline is green**

Run: `uv run pytest -q`
Expected: all pass (353+ tests; if anything fails here, STOP — verify against `main` before proceeding).

---

### Task 1: Control protocol parser + octal decoder

**Files:**
- Create: `periscope/tmux_mirror.py`
- Create: `tests/test_tmux_mirror.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tmux_mirror.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.tmux_mirror'`

- [ ] **Step 3: Implement events, parser, decoder**

Create `periscope/tmux_mirror.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux_mirror.py tests/test_tmux_mirror.py
git commit -m "feat: tmux_mirror control-mode parser + octal decoder"
```

---

### Task 2: Reconcile frame builder

**Files:**
- Modify: `periscope/tmux_mirror.py` (append after `decode_octal`)
- Modify: `tests/test_tmux_mirror.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tmux_mirror.py`; extend the import to add `GridSnapshot, build_reconcile_frame, snapshot_from_replies`)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: FAIL — `ImportError: cannot import name 'GridSnapshot'`

- [ ] **Step 3: Implement** (append to `periscope/tmux_mirror.py`)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux_mirror.py tests/test_tmux_mirror.py
git commit -m "feat: tmux_mirror reconcile frame builder (row-loop coverage, 1049l heal, no 2J)"
```

---

### Task 3: ReconcileTimer

**Files:**
- Modify: `periscope/tmux_mirror.py` (append)
- Modify: `tests/test_tmux_mirror.py` (append)

- [ ] **Step 1: Write the failing tests** (append; add `ReconcileTimer, QUIESCE_S, MAX_INTERVAL_S` to the import)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: FAIL — `ImportError: cannot import name 'ReconcileTimer'`

- [ ] **Step 3: Implement** (append to `periscope/tmux_mirror.py`)

```python
class ReconcileTimer:
    """Quiesce/max-interval scheduling for one pane's reconciles.

    Armed only by output or an explicit request — a fully idle pane never
    reconciles. `now` is injectable (fake-clock unit tests); scheduling
    goes through `_arm`, a single patchable seam over loop.call_later.
    `fire` must be sync: it only *initiates* the capture commands — the
    frame itself is built later, inside the reader task (ordering rule).
    """

    def __init__(self, fire: Callable[[], None], *,
                 now: Callable[[], float] | None = None) -> None:
        self._fire = fire
        self._now = now
        self._handle: asyncio.TimerHandle | None = None
        self._streaming_since: float | None = None

    def _time(self) -> float:
        return self._now() if self._now else asyncio.get_running_loop().time()

    def note_output(self) -> None:
        t = self._time()
        if self._streaming_since is None:
            self._streaming_since = t
        deadline = min(t + QUIESCE_S, self._streaming_since + MAX_INTERVAL_S)
        self._arm(max(0.0, deadline - t))

    def note_reconciled(self) -> None:
        self._streaming_since = None

    def request(self) -> None:
        self._arm(0.0)

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _arm(self, delay: float) -> None:
        self.cancel()
        self._handle = asyncio.get_running_loop().call_later(delay, self._fired)

    def _fired(self) -> None:
        self._handle = None
        self._fire()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux_mirror.py tests/test_tmux_mirror.py
git commit -m "feat: tmux_mirror ReconcileTimer (quiesce + max-interval, injectable clock)"
```

---

### Task 4: Subscription, _SessionMirror, module API

**Files:**
- Modify: `periscope/tmux_mirror.py` (append)
- Modify: `tests/test_tmux_mirror.py` (append)

The reader loop delegates to a sync `_dispatch(event)` method so dispatch is unit-testable without a subprocess.

- [ ] **Step 1: Write the failing tests** (append; add `_SessionMirror, Subscription` to the import)

```python
# --- _SessionMirror dispatch (no subprocess: feed events into _dispatch) ---
import asyncio as _asyncio


def test_dispatch_output_routes_decoded_bytes_to_subscribers():
    async def drive():
        m = _SessionMirror("sess")
        sub = m.subscribe("%7")
        m._dispatch(Output(pane_id="%7", raw=rb"hi\033[m"))
        m._dispatch(Output(pane_id="%99", raw=b"other-pane"))  # cheap skip
        assert sub._q.get_nowait() == b"hi\x1b[m"
        assert sub._q.empty()
    _asyncio.run(drive())


def test_dispatch_reply_with_empty_callback_queue_is_dropped():
    # The attach handshake emits an unsolicited %begin/%end block.
    async def drive():
        m = _SessionMirror("sess")
        m._dispatch(Reply(body=()))        # must not raise
    _asyncio.run(drive())


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
    _asyncio.run(drive())


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
    _asyncio.run(drive())


def test_layout_change_requests_reconcile_for_subscribed_panes():
    async def drive():
        m = _SessionMirror("sess")
        m.subscribe("%7")
        fired = []
        m._timers["%7"].request = lambda: fired.append(True)
        m._dispatch(LayoutChange(window_id="@1"))
        assert fired == [True]
    _asyncio.run(drive())


def test_unsubscribe_last_viewer_arms_linger():
    async def drive():
        m = _SessionMirror("sess")
        m._proc = object()   # pretend a client is running (linger guard)
        sub = m.subscribe("%7")
        async with sub:
            pass
        assert m._linger is not None
        m._linger.cancel()
    _asyncio.run(drive())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: FAIL — `ImportError: cannot import name '_SessionMirror'`

- [ ] **Step 3: Implement** (append to `periscope/tmux_mirror.py`)

```python
class Subscription:
    """Async iterator of decoded pane bytes; None sentinel = EOF (pane
    died, client died, shutdown). Async context manager — exit
    unsubscribes."""

    def __init__(self, mirror: "_SessionMirror", pane_id: str) -> None:
        self._mirror = mirror
        self.pane_id = pane_id
        self._q: asyncio.Queue[bytes | None] = asyncio.Queue()

    def request_reconcile(self) -> None:
        self._mirror.request_reconcile(self.pane_id)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *_exc) -> None:
        self._mirror._unsubscribe(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._q.get()
        if chunk is None:
            raise StopAsyncIteration
        return chunk


class _SessionMirror:
    """One control-mode client (`tmux -C attach`) for one session.

    Owns the subprocess, the reader task, the FIFO reply-callback queue,
    per-pane subscriber lists, and per-pane reconcile timers.
    """

    def __init__(self, session: str) -> None:
        self.session = session
        self._parser = ControlParser()
        self._subs: dict[str, list[Subscription]] = {}
        self._timers: dict[str, ReconcileTimer] = {}
        self._reply_callbacks: deque[Callable[[Reply | ReplyError], None]] = deque()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._linger: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        # ignore-size: a control client is a real client; without the flag
        # it would participate in window sizing for the session's
        # non-viewed windows (viewed panes are already window-size manual).
        # read-only: belt and braces — the mirror only ever reads.
        self._proc = await asyncio.create_subprocess_exec(
            *_TMUX, "-C", "attach", "-t", self.session,
            "-f", "ignore-size,read-only",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=STREAM_LIMIT,
        )
        self._reader = _task(f"tmux-mirror:{self.session}", self._read_loop())
        log.info("tmux mirror client up for session %r (pid=%s)",
                 self.session, self._proc.pid)

    # -- subscriptions --

    def subscribe(self, pane_id: str) -> Subscription:
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        sub = Subscription(self, pane_id)
        self._subs.setdefault(pane_id, []).append(sub)
        if pane_id not in self._timers:
            self._timers[pane_id] = ReconcileTimer(
                fire=lambda p=pane_id: self._fire_reconcile(p))
        # Connect-time reconcile: ws.py sends the initial blob via the
        # fork path (a multi-MB reply here would head-of-line-block every
        # %output in the session); this heals the capture-vs-subscribe gap
        # moments later.
        self._timers[pane_id].request()
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.pane_id, [])
        if sub in subs:
            subs.remove(sub)
        if not subs:
            self._subs.pop(sub.pane_id, None)
            timer = self._timers.pop(sub.pane_id, None)
            if timer:
                timer.cancel()
        if not self._subs and self._proc is not None and self._linger is None:
            self._linger = asyncio.get_running_loop().call_later(
                LINGER_S, self._kill)

    def request_reconcile(self, pane_id: str) -> None:
        timer = self._timers.get(pane_id)
        if timer:
            timer.request()

    # -- reader / dispatch --

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                event = self._parser.feed_line(line.rstrip(b"\r\n"))
                if event is None:
                    continue
                if isinstance(event, Exit):
                    break
                self._dispatch(event)
        finally:
            self._finalize()

    def _dispatch(
        self, event: Output | Reply | ReplyError | LayoutChange
    ) -> None:
        if isinstance(event, Output):
            subs = self._subs.get(event.pane_id)
            if subs:  # pane-id check before decode: unviewed panes are free
                data = decode_octal(event.raw)
                for s in subs:
                    s._q.put_nowait(data)
                self._timers[event.pane_id].note_output()
        elif isinstance(event, (Reply, ReplyError)):
            if self._reply_callbacks:
                self._reply_callbacks.popleft()(event)
            # else: the unsolicited attach-handshake block — drop.
        elif isinstance(event, LayoutChange):
            # Window→pane mapping isn't tracked; reconciling every
            # subscribed pane is cheap and layout changes are rare.
            for timer in self._timers.values():
                timer.request()

    # -- reconciliation --

    def _send_command(
        self, cmd: str, on_reply: Callable[[Reply | ReplyError], None]
    ) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._reply_callbacks.append(on_reply)
        # No drain(): commands are tens of bytes; a full OS pipe here
        # would mean tmux itself is wedged.
        self._proc.stdin.write(cmd.encode() + b"\n")

    def _fire_reconcile(self, pane_id: str) -> None:
        if pane_id not in self._subs or self._proc is None:
            return
        got: dict[str, Reply | ReplyError] = {}

        def on_capture(reply: Reply | ReplyError) -> None:
            got["capture"] = reply

        def on_display(reply: Reply | ReplyError) -> None:
            # Runs synchronously inside the reader task at this reply's
            # %end — the ordering rule. No %output can leapfrog the frame.
            cap = got.get("capture")
            if isinstance(cap, ReplyError) or isinstance(reply, ReplyError):
                self._end_pane(pane_id)  # pane died mid-capture
                return
            subs = self._subs.get(pane_id)
            if not subs:
                return  # unsubscribed while the commands were in flight
            frame = build_reconcile_frame(snapshot_from_replies(cap, reply))
            for s in subs:
                s._q.put_nowait(frame)
            timer = self._timers.get(pane_id)
            if timer:
                timer.note_reconciled()

        # Capture first so the cursor sample (display-message) is the
        # fresher of the two.
        self._send_command(f"capture-pane -p -e -t {pane_id}", on_capture)
        self._send_command(
            f"display-message -p -t {pane_id} '{DISPLAY_FMT}'", on_display)

    # -- teardown --

    def _end_pane(self, pane_id: str) -> None:
        for s in self._subs.pop(pane_id, []):
            s._q.put_nowait(None)
        timer = self._timers.pop(pane_id, None)
        if timer:
            timer.cancel()
        if not self._subs and self._proc is not None and self._linger is None:
            self._linger = asyncio.get_running_loop().call_later(
                LINGER_S, self._kill)

    def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        # reader sees EOF → _finalize

    def _finalize(self) -> None:
        self._proc = None  # a late __aexit__ must not re-arm the linger
        for subs in self._subs.values():
            for s in subs:
                s._q.put_nowait(None)
        self._subs.clear()
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        if _MIRRORS.get(self.session) is self:
            del _MIRRORS[self.session]
        log.info("tmux mirror client for session %r closed", self.session)


# -- module API (surface style mirrors tmux_input) --

_MIRRORS: dict[str, _SessionMirror] = {}
_lock = asyncio.Lock()


async def subscribe(session: str, pane_id: str) -> Subscription:
    """Subscribe to a pane's mirrored byte stream, spawning or reusing the
    session's control client. No fork-path fallback by design: byte relay
    without reconciliation is exactly the bug this module removes
    (tmux_input degrades to forks because input has a working degraded
    mode; mirroring does not)."""
    async with _lock:
        mirror = _MIRRORS.get(session)
        if mirror is None:
            mirror = _SessionMirror(session)
            _MIRRORS[session] = mirror
            try:
                await mirror.start()
            except Exception:
                _MIRRORS.pop(session, None)
                raise
        return mirror.subscribe(pane_id)


async def shutdown() -> None:
    for mirror in list(_MIRRORS.values()):
        if mirror._reader is not None:
            mirror._reader.cancel()
        if mirror._proc is not None and mirror._proc.returncode is None:
            try:
                mirror._proc.terminate()
            except ProcessLookupError:
                pass
    _MIRRORS.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux_mirror.py tests/test_tmux_mirror.py
git commit -m "feat: tmux_mirror session client, subscriptions, callback-ordered reconcile"
```

---

### Task 5: Integration tests — the closed-loop oracle

**Files:**
- Modify: `pyproject.toml` (add pyte to dev group)
- Modify: `tests/test_tmux_mirror.py` (append)

- [ ] **Step 1: Add pyte as a test-only dependency**

In `pyproject.toml`, change the dev group to:

```toml
[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-mock>=3",
    # Test-only terminal emulator: the verification oracle for
    # tests/test_tmux_mirror.py. Deliberately NOT a runtime dep — the
    # design keeps emulation out of periscope (tmux is the emulator).
    "pyte>=0.8",
]
```

Run: `uv sync && uv run python -c "from importlib.metadata import version; print(version('pyte'))"`
Expected: prints a version ≥ 0.8 (note: `pyte.__version__` does not exist — don't use it)

- [ ] **Step 2: Write the integration tests** (append to `tests/test_tmux_mirror.py`)

These run a real tmux server on a dedicated socket (`-L periscope-mirror-test`), following `tests/test_tmux_input.py::test_roundtrip_into_real_pane`'s skipif pattern. pyte plays the role of xterm.js; assertions compare pyte's grid to `capture-pane`'s grid. The oracle tests use normal-screen shell content (pyte's alt-screen support is partial; the alt path is pinned by the frame-builder unit tests).

```python
# --- integration: real tmux, pyte as the client-side oracle ---
# (hoist these imports to the top of the file with the others; asyncio in
# particular is used by _drain and the test bodies)
import asyncio
import shutil
import subprocess
import uuid

import periscope.tmux_mirror as tmux_mirror

TEST_SOCKET = "periscope-mirror-test"
needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def _t(*args):
    return subprocess.run(
        ["tmux", "-L", TEST_SOCKET, *args],
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
        except (asyncio.TimeoutError, StopAsyncIteration):
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
```

- [ ] **Step 3: Run the integration tests**

Run: `uv run pytest tests/test_tmux_mirror.py -q -k "oracle or thesis or multiplex"`
Expected: 3 PASS (give them a few seconds — real tmux + sleeps). If `_grids_equal` fails on SGR-heavy rows, compare with the pyte screen dump in the assertion message; text content must match even if pyte renders attributes differently.

- [ ] **Step 4: Run the whole file, then commit**

Run: `uv run pytest tests/test_tmux_mirror.py -q`
Expected: all PASS

```bash
git add pyproject.toml uv.lock tests/test_tmux_mirror.py
git commit -m "test: tmux_mirror integration — pyte oracle, byte-drop convergence, multiplexing"
```

---

### Task 6: Rewrite ws.py internals + tests

**Files:**
- Modify: `periscope/routes/ws.py` (full rewrite below)
- Modify: `tests/routes/test_ws.py` (full rewrite below)

- [ ] **Step 1: Rewrite the tests first** (`tests/routes/test_ws.py`, full replacement)

```python
"""Tests for WS /ws/pane.

The handler consumes periscope.tmux_mirror's Subscription; tests patch
the subscribe boundary with a fake (the only mocked seam — parsing,
frames, and timing are unit-tested in tests/test_tmux_mirror.py) and
fake_tmux for the fork-path display-message/capture-pane calls.
"""

import asyncio
import json


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
        with pytest.raises(Exception):  # starlette raises on closed ws
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


import pytest  # noqa: E402  (used by test_ws_closes_on_subscription_eof)
```

(Put the `import pytest` at the top of the file with the other imports rather than the bottom — shown here last only to highlight it's newly needed.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/routes/test_ws.py -q`
Expected: FAIL — `AttributeError` (ws.py has no `tmux_mirror` attribute yet)

- [ ] **Step 3: Rewrite `periscope/routes/ws.py`** (full replacement)

```python
"""WS /ws/pane — bridges xterm.js to a tmux pane via the control-mode mirror.

Initial paint mirrors tmux's screen state (size, cursor, alt-screen) so the
blob renders into an xterm state matching tmux's. Live bytes and
self-healing reconcile frames come from periscope.tmux_mirror — any mirror
desync heals at the next frame, so this handler no longer needs the byte
stream to be perfect, only the frames to keep coming. Design spec:
docs/superpowers/specs/2026-06-10-terminal-mirror-reconciliation-design.md
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from periscope.log import _task
from periscope.panes import note_action
from periscope.tmux import tmux
from periscope import tmux_input, tmux_mirror

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
    # Modal-open is the canonical "opened in periscope" event.
    note_action(target)
    loop = asyncio.get_running_loop()

    # Periscope owns the pane width. Once we resize a window to the modal's
    # dims, we LEAVE IT THERE — no restore on disconnect. Rationale: the
    # save/restore dance only matters if a real terminal is also attached
    # at a different size, but in periscope-primary workflows the restore
    # just causes churn — each modal open/close pair would reflow the
    # buffer twice, and reflows during streaming produce duplicated table
    # fragments in Claude's scrollback. Holding the pane at periscope's
    # size means subsequent opens at the same width are no-ops.
    def set_pane_size(c: int, r: int) -> None:
        try:
            tmux("setw", "-t", target, "window-size", "manual")
            tmux("resize-window", "-t", target, "-x", str(c), "-y", str(r))
        except Exception:
            pass

    # 1) Resize tmux to the client's hint BEFORE capture-pane, so the
    #    initial blob is rendered at the width xterm will display it at —
    #    otherwise box-drawing TUIs mangle on the first frame.
    if cols > 0 and rows > 0:
        await loop.run_in_executor(None, lambda: set_pane_size(cols, rows))

    # 2) tmux's view of the pane: size, cursor, alt-screen — all three are
    #    needed to render the initial blob into an xterm state matching
    #    tmux's — plus #{pane_id} for the mirror subscription. If the pane
    #    is gone, close: the client's reconnect FSM handles it.
    try:
        meta = await loop.run_in_executor(None, lambda: tmux(
            "display-message", "-t", target, "-p",
            "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}"
            "|#{alternate_on}|#{pane_id}",
        ))
        cols_s, rows_s, cx_s, cy_s, alt_s, pane_id = meta.strip().split("|")
        cols, rows = int(cols_s), int(rows_s)
        cx, cy = int(cx_s), int(cy_s)
        alt_on = alt_s == "1"
    except Exception:
        await websocket.close()
        return

    sub = await tmux_mirror.subscribe(session, pane_id)
    async with sub:
        await websocket.send_text(
            json.dumps({"type": "size", "cols": cols, "rows": rows})
        )

        # 3) Initial paint, via the fork path — NOT the control client: a
        #    50k-history `-e` capture can run multi-MB, and tmux holds all
        #    %output for the whole session until a reply block completes.
        #    The capture-vs-subscribe byte gap this allows is healed by the
        #    mirror's connect-time reconcile moments later; only scrollback
        #    can miss those few bytes.
        #    `-S -N` asks for N lines of scrollback; tmux clamps to the
        #    pane's history-limit.
        initial = await loop.run_in_executor(None, lambda: tmux(
            "capture-pane", "-t", target, "-p", "-e", "-S", "-10000"))
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
            try:
                await websocket.close()
            except Exception:
                pass

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
```

- [ ] **Step 4: Run the route tests**

Run: `uv run pytest tests/routes/test_ws.py -q`
Expected: all PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS (watch for stragglers that referenced the FIFO behavior)

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/ws.py tests/routes/test_ws.py
git commit -m "feat: ws.py consumes tmux_mirror — FIFO/pipe-pane machinery deleted"
```

---

### Task 7: Lifespan shutdown

**Files:**
- Modify: `periscope/app.py:90-91`

- [ ] **Step 1: Register mirror shutdown**

In `periscope/app.py`, in the lifespan `finally` block, change:

```python
        from periscope import tmux_input
        await tmux_input.shutdown()
```

to:

```python
        from periscope import tmux_input, tmux_mirror
        await tmux_input.shutdown()
        await tmux_mirror.shutdown()   # control clients must not leak
                                       # across dev reloads
```

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add periscope/app.py
git commit -m "feat: lifespan shuts down tmux_mirror control clients"
```

---

### Task 8: CLAUDE.md + manual verification

**Files:**
- Modify: `CLAUDE.md` (architecture diagram, module table, invariants #3, tests section)

- [ ] **Step 1: Update CLAUDE.md**

1. In the architecture diagram, change
   `│ └── unix socket …` block's WS line:
   `├── /ws/pane     bidirectional terminal bridge (capture-pane snapshot + pipe-pane FIFO)`
   →
   `├── /ws/pane     bidirectional terminal bridge (capture-pane snapshot + control-mode mirror w/ reconcile frames)`
2. In the module table, add after the `tmux.py` row:
   `| periscope/tmux_mirror.py | Control-mode pane mirror: %output relay + self-healing reconcile frames |`
3. Rewrite Key invariant **#3** to:
   > **WebSocket paint is self-healing, not perfect.** The initial blob still
   > mirrors tmux's size/cursor/alt-screen state (all from `display-message`
   > before the capture body), but live bytes come from a per-session tmux
   > control-mode client (`tmux_mirror.py`), and the mirror periodically ships
   > an idempotent repaint of tmux's own grid. Reconcile frames are built
   > **inside the reader task at the reply's `%end`** — building them in a
   > future-woken task would let later `%output` land first and be reverted
   > by the frame. Don't "optimize" this to futures.
4. Key invariant **#4** (`\n` → `\r\n`) stays as-is — still true for the
   initial blob.
5. In the Tests section, add `tests/test_tmux_mirror.py` to the list with:
   `uv run pytest tests/test_tmux_mirror.py  # mirror protocol + pyte convergence oracle`

- [ ] **Step 2: Manual verification on the dev port**

```bash
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
```

In a browser at http://localhost:8766/ open a pane running a Claude session and verify:
1. Initial paint correct (cursor at prompt, no ghost rows).
2. Trigger heavy TUI redraws (e.g. a Claude session rendering AskUserQuestion, or run `top` then quit) — no persistent overlap; any transient tear heals within ~1s.
3. `tmux list-clients` shows one extra client (the mirror, `ignore-size,read-only` flags) while viewing, gone ~20s after navigating away.
4. Kill the viewed pane's session — the terminal shows "[periscope: reconnecting…]" (the WS closed, FSM retrying) rather than freezing silently.

Paste the observed results into the commit message of step 3 if all four pass; STOP and debug if any fail.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — terminal mirror replaces pipe-pane invariants"
```

---

### Task 9: Merge + prod restart

- [ ] **Step 1: Merge to main** (merge commit, not rebase — project rule)

```bash
git -C ~/dev/periscope merge feature/terminal-mirror
```

If `main` moved during the work (it does — live branch), re-run `uv run pytest -q` after the merge in `~/dev/periscope`.

- [ ] **Step 2: Restart prod + verify**

```bash
~/dev/periscope/bin/periscope restart
~/dev/periscope/bin/periscope status
```

Open the dashboard, view a pane, confirm the mirror works in prod (same four checks as Task 8 Step 2). No frontend rebuild is needed — `static/src/` is untouched.

- [ ] **Step 3: Clean up the worktree**

```bash
git -C ~/dev/periscope worktree remove ~/dev/periscope-mirror
git -C ~/dev/periscope branch -d feature/terminal-mirror
```

---

## Deviations from the structure doc

- `GridSnapshot` has no `width` field — the frame builder never uses it
  (rows are written as captured; `\x1b[K` handles the tail). YAGNI.
- `_TMUX` module constant added (not in the structure doc) — the
  integration tests need to target a `-L` socket; monkeypatching one
  tuple is the cheapest seam.
- The structure doc's thin future-based `_command()` wrapper for data-only callers was omitted entirely — no caller needs it (the initial blob goes via the fork path).
