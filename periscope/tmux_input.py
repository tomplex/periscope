"""Persistent tmux control-mode client for low-latency keystroke delivery.

`tmux.deliver_input` forks `tmux send-keys` once per drain cycle — ~20-80ms
of fork+exec on a loaded host, which is the dominant, *felt* latency when
typing into a pane through the dashboard. Control mode (`tmux -C`) keeps one
long-lived client whose stdin accepts commands the server runs in order,
with no new process per keystroke. We route the fast path (single keys,
short escape sequences, normal pastes) through it; large pastes still use
the fork path where latency is irrelevant.

The control client attaches to a dedicated, hidden session
(`INPUT_CTL_SESSION`) so its client size never influences a user pane's
window size, and so it survives any user session being killed. That session
is filtered out of `list_windows` alongside the /usage scraper's.

Every failure mode degrades to the fork path so input never silently drops:
a spawn failure disables control mode for the process lifetime (a tmux that
can't do `-C` won't start to); a dead client (tmux restart) is respawned on
the next keystroke.
"""

import asyncio
import contextlib

from periscope.config import INPUT_CTL_SESSION
from periscope.log import _task, log
from periscope.tmux import _SEND_KEYS_H_MAX, deliver_input

# Test seam (same shape as tmux_mirror._TMUX): the integration test points
# this at a dedicated `-L` socket so the control client never touches the
# default server, where prod periscope's live INPUT_CTL_SESSION lives.
_TMUX: tuple[str, ...] = ("tmux",)

_proc: asyncio.subprocess.Process | None = None
_drain: asyncio.Task | None = None
_lock = asyncio.Lock()
_disabled = False  # set once if `tmux -C` can't be started here


async def _drain_stdout(proc: asyncio.subprocess.Process) -> None:
    # Control mode streams %begin/%end/%output blocks; we issue fire-and-forget
    # send-keys and never read replies, so drain and discard to keep the pipe
    # from filling and blocking the tmux server.
    assert proc.stdout is not None
    while True:
        if not await proc.stdout.read(4096):
            break


async def _spawn() -> asyncio.subprocess.Process:
    global _drain
    # Dedicated hidden session for the client to attach to. `-A` attaches-or-
    # creates so a respawn after a tmux restart reuses it; detached, an idle
    # shell, shares no windows with user panes.
    create = await asyncio.create_subprocess_exec(
        *_TMUX, "new-session", "-d", "-A", "-s", INPUT_CTL_SESSION,
        "-x", "80", "-y", "24",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await create.wait()
    proc = await asyncio.create_subprocess_exec(
        *_TMUX, "-C", "attach", "-t", INPUT_CTL_SESSION,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _drain = _task("tmux-ctl-drain", _drain_stdout(proc))
    log.info("tmux control-mode input client up (pid=%s)", proc.pid)
    return proc


async def _ensure() -> asyncio.subprocess.Process:
    global _proc, _disabled
    if _proc is None or _proc.returncode is not None:
        try:
            _proc = await _spawn()
        except Exception as e:
            # A tmux that can't start a control client won't on retry either —
            # stop trying so we don't fork a session per keystroke.
            _disabled = True
            log.warning("tmux control mode unavailable (%s); using fork path", e)
            raise
    return _proc


def _cmd(target: str, encoded: bytes) -> bytes:
    # Single-quote the target — session names carry slashes/colons (fine
    # unquoted) but quoting is robust against any tmux metacharacter. The hex
    # bytes are never command-parsed, so there's no injection surface.
    hexes = " ".join(f"{b:02x}" for b in encoded)
    return f"send-keys -t '{target}' -H {hexes}\n".encode()


async def _fork_fallback(target: str, text: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: deliver_input(target, text))


async def send(target: str, text: str) -> None:
    """Deliver keystrokes to a pane. Fast path goes through the persistent
    control client; large pastes and any control-mode failure fall back to
    the fork path so input is never dropped."""
    encoded = text.encode("utf-8")
    if _disabled or len(encoded) > _SEND_KEYS_H_MAX:
        await _fork_fallback(target, text)
        return
    line = _cmd(target, encoded)
    async with _lock:
        try:
            proc = await _ensure()
            assert proc.stdin is not None
            proc.stdin.write(line)
            await proc.stdin.drain()
            return
        except Exception as e:
            # Dead client (tmux restart) or a spawn that flipped _disabled.
            # Reset so the next keystroke respawns, and fork this one.
            global _proc
            _proc = None
            log.warning("tmux control send failed (%s); forking this keystroke", e)
    await _fork_fallback(target, text)


async def shutdown() -> None:
    global _proc, _drain
    if _drain is not None:
        _drain.cancel()
        _drain = None
    if _proc is not None and _proc.returncode is None:
        with contextlib.suppress(Exception):
            _proc.terminate()
    _proc = None
