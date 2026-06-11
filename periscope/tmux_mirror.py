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
        # The attach handshake is one unsolicited %begin/%end block — and
        # the connect-time reconcile usually wins the race against it, so
        # by the time it arrives the callback queue already holds that
        # reconcile's callbacks. Drop the FIRST reply block here (replies
        # arrive in command order; attach's implicit command is first) so
        # it can't shift every queued callback off by one.
        handshake_pending = True
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
                if handshake_pending and isinstance(event, (Reply, ReplyError)):
                    handshake_pending = False
                    continue
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
            # else: belt-and-braces — the attach handshake is dropped in
            # _read_loop before dispatch; this guards a hypothetical
            # second unsolicited block.
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
