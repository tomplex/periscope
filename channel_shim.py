# /// script
# requires-python = ">=3.11"
# ///
"""Periscope channel shim.

Stdio MCP transport spawned by Claude (via `claude --dangerously-load-
development-channels server:periscope`). Pumps MCP messages between
Claude's stdio and periscope's unix-socket MCP listener, and transparently
reconnects across periscope restarts so Claude's MCP connection survives
without the user having to /clear or restart.

Reconnect dance after a socket drop:
1. Synthesize JSON-RPC error responses for any tool calls that were in
   flight when the socket died, so Claude doesn't hang.
2. Retry connecting at RECONNECT_BACKOFF_S intervals until the unix
   socket comes back (or stdin EOFs — Claude has exited).
3. On a fresh socket, re-send the hello frame, replay the saved
   `initialize` request, replay `notifications/initialized`, and synth
   a `tools/list` so the new periscope's `_list_tools` handler captures
   the per-pane session it uses for push notifications and tool calls.
4. Suppress server responses to the replayed initialize and the synth
   tools/list — Claude has already seen its initialize response from
   the original connection, and the tools/list is shim-internal.

The stdin reader runs in a single background task feeding a queue, so
Claude can keep sending requests while the shim is mid-reconnect; they
flush to the new socket once it's up.

Failure modes (missing TMUX_PANE, can't attach stdin reader) still exit
cleanly with code 0 — non-zero exits pop macOS's crash reporter every
time Claude respawns the shim, which is intolerable for a nice-to-have
channel.
"""

import asyncio
import contextlib
import json
import os
import sys

SOCKET_PATH = os.environ.get(
    "PERISCOPE_MCP_SOCKET_PATH", "/tmp/periscope-mcp.sock"
)
TMUX_PANE = os.environ.get("TMUX_PANE", "")
# The caller handle: an explicit commander id (cmdr:<session_id>) when periscope
# dispatched us via --bg, else the tmux pane id for a normal pane.
CALLER_ID = os.environ.get("PERISCOPE_CALLER_ID", "") or TMUX_PANE
RECONNECT_BACKOFF_S = float(
    os.environ.get("PERISCOPE_MCP_RECONNECT_BACKOFF_S", "1.0")
)

# Synthetic JSON-RPC ids the shim generates internally (currently just for
# the post-reconnect tools/list). Picked well above any plausible client-
# assigned id so they don't collide with Claude's outgoing requests.
_SYNTHETIC_ID_BASE = 9_000_000

# JSON-RPC error code returned to Claude for in-flight requests orphaned
# by a periscope restart. -32099 is in the JSON-RPC "server error" range.
_RECONNECT_ERROR_CODE = -32099


def _err(msg: str) -> None:
    sys.stderr.write(f"channel_shim: {msg}\n")
    sys.stderr.flush()


class Shim:
    def __init__(self) -> None:
        # First initialize request seen on stdin — replayed on each reconnect.
        self._saved_initialize: bytes | None = None
        self._initialize_id: object | None = None
        # First notifications/initialized seen on stdin — replayed on each
        # reconnect so the server lands in the post-init state.
        self._saved_initialized: bytes | None = None
        # Request ids sent to periscope that haven't received a response yet.
        # When the socket dies these get synthetic error responses so Claude
        # doesn't hang. dict for insertion order, not the values.
        self._inflight_ids: dict[object, None] = {}
        # Response ids the shim must swallow rather than forward to Claude:
        # the duplicate initialize response on each reconnect, plus the
        # synthetic tools/list response.
        self._swallow_ids: set[object] = set()
        self._next_synthetic_id = _SYNTHETIC_ID_BASE
        self._stdin_eof = asyncio.Event()
        self._stdin_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def run(self) -> None:
        if not (CALLER_ID.startswith(("%", "cmdr:"))):
            _err(
                f"caller id missing or malformed ({CALLER_ID!r}); "
                "periscope MCP inactive for this session"
            )
            return

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        try:
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
        except Exception as e:
            _err(f"can't attach stdin reader ({e}); inactive")
            return

        stdin_task = asyncio.create_task(self._stdin_pump(reader))

        try:
            while not self._stdin_eof.is_set():
                try:
                    sock_r, sock_w = await asyncio.open_unix_connection(
                        SOCKET_PATH
                    )
                except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                    _err(
                        f"can't reach periscope at {SOCKET_PATH} ({e}); "
                        f"retrying in {RECONNECT_BACKOFF_S}s"
                    )
                    if await self._wait_or_eof(RECONNECT_BACKOFF_S):
                        return
                    continue

                try:
                    await self._serve(sock_r, sock_w)
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    _err(f"socket error ({e}); reconnecting")
                finally:
                    try:
                        sock_w.close()
                        await sock_w.wait_closed()
                    except (OSError, BrokenPipeError):
                        pass

                if self._stdin_eof.is_set():
                    return

                # Connection dropped while Claude is still alive — synth
                # errors for orphaned in-flight requests so Claude unblocks,
                # then loop back and try to reconnect.
                self._fail_inflight()
                if await self._wait_or_eof(RECONNECT_BACKOFF_S):
                    return
        finally:
            stdin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stdin_task

    async def _stdin_pump(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                self._stdin_eof.set()
                await self._stdin_queue.put(None)
                return
            await self._stdin_queue.put(line)

    async def _wait_or_eof(self, secs: float) -> bool:
        """Sleep `secs`. Return True if stdin EOF arrived during the wait —
        the caller uses this to short-circuit out of the reconnect loop."""
        try:
            await asyncio.wait_for(self._stdin_eof.wait(), timeout=secs)
            return True
        except TimeoutError:
            return False

    async def _serve(
        self, sock_r: asyncio.StreamReader, sock_w: asyncio.StreamWriter
    ) -> None:
        # 1. Hello frame — periscope reads one JSON line on accept() to learn
        # this connection's caller handle (%N for a pane, cmdr:<id> for a
        # commander). The JSON key stays "pane" (a private 2-file wire
        # contract); the value is now any handle.
        sock_w.write((json.dumps({"pane": CALLER_ID}) + "\n").encode())
        await sock_w.drain()

        # 2. Replay the captured handshake when reconnecting. Suppress the
        # duplicate initialize response (Claude already got one originally)
        # and re-send notifications/initialized so the server is in the
        # post-init state ready to accept tool calls.
        is_reconnect = self._saved_initialize is not None
        if self._saved_initialize is not None:
            self._swallow_ids.add(self._initialize_id)
            sock_w.write(self._saved_initialize)
            await sock_w.drain()
        if self._saved_initialized is not None:
            sock_w.write(self._saved_initialized)
            await sock_w.drain()

        # 3. After a reconnect, synth a tools/list so the server's _list_tools
        # handler captures _MCP_SESSIONS[pane] for this new connection.
        # Without this, periscope can't push notifications or route tool calls
        # to the right session. On the first connection Claude's own tools/list
        # does this for us; only needed on reconnect.
        if is_reconnect:
            self._next_synthetic_id += 1
            syn_id = self._next_synthetic_id
            self._swallow_ids.add(syn_id)
            sock_w.write(
                (json.dumps({"jsonrpc": "2.0", "id": syn_id, "method": "tools/list"}) + "\n").encode()
            )
            await sock_w.drain()

        # 4. Bidirectional pump until either side closes.
        out_task = asyncio.create_task(self._stdin_to_socket(sock_w))
        in_task = asyncio.create_task(self._socket_to_stdout(sock_r))
        _, pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _stdin_to_socket(self, sock_w: asyncio.StreamWriter) -> None:
        while True:
            line = await self._stdin_queue.get()
            if line is None:
                # Claude closed stdin. Half-close the socket so periscope
                # notices and unwinds its handler cleanly.
                try:
                    sock_w.write_eof()
                    await sock_w.drain()
                except (OSError, BrokenPipeError):
                    pass
                return

            self._observe_outbound(line)
            try:
                sock_w.write(line)
                await sock_w.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Socket died mid-write. _observe_outbound already recorded
                # this request's id in _inflight_ids if it was a request, so
                # the outer loop's _fail_inflight will synth an error for it.
                return

    async def _socket_to_stdout(self, sock_r: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await sock_r.readline()
            except (ConnectionResetError, OSError):
                return
            if not line:
                return
            if self._maybe_swallow_response(line):
                continue
            self._observe_inbound(line)
            try:
                os.write(1, line)
            except (BrokenPipeError, OSError):
                return

    def _observe_outbound(self, line: bytes) -> None:
        msg = _parse_json_object(line)
        if msg is None:
            return
        method = msg.get("method")
        # JSON-RPC shape: request = method + id, notification = method only,
        # response = id only.
        if method == "initialize" and self._saved_initialize is None:
            self._saved_initialize = line
            self._initialize_id = msg.get("id")
        elif method == "notifications/initialized" and self._saved_initialized is None:
            self._saved_initialized = line
        if method is not None and "id" in msg:
            self._inflight_ids[msg["id"]] = None

    def _observe_inbound(self, line: bytes) -> None:
        msg = _parse_json_object(line)
        if msg is None:
            return
        if "id" in msg and "method" not in msg:
            self._inflight_ids.pop(msg["id"], None)

    def _maybe_swallow_response(self, line: bytes) -> bool:
        if not self._swallow_ids:
            return False
        msg = _parse_json_object(line)
        if msg is None:
            return False
        rid = msg.get("id")
        if rid in self._swallow_ids and "method" not in msg:
            self._swallow_ids.discard(rid)
            return True
        return False

    def _fail_inflight(self) -> None:
        if not self._inflight_ids:
            return
        for rid in list(self._inflight_ids.keys()):
            err_resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {
                    "code": _RECONNECT_ERROR_CODE,
                    "message": "periscope channel reconnected; please retry",
                },
            }
            with contextlib.suppress(BrokenPipeError, OSError):
                os.write(1, (json.dumps(err_resp) + "\n").encode())
        self._inflight_ids.clear()


def _parse_json_object(line: bytes) -> dict | None:
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Shim().run())


if __name__ == "__main__":
    main()
