# /// script
# requires-python = ">=3.11"
# ///
"""Periscope channel shim.

Trivial bidirectional bytes proxy between Claude's stdio MCP transport and
periscope's unix-socket MCP listener. Spawned by Claude as a subprocess
when launched with `claude --dangerously-load-development-channels server:periscope`.

On connect, sends a single hello frame ({"pane": $TMUX_PANE}) so periscope
knows which pane this connection belongs to. Then pumps stdin↔socket in
both directions until either side closes.

Failure modes (missing TMUX_PANE, periscope not running, unreachable
socket) all exit cleanly with code 0 — Claude continues without the
periscope MCP attached. Non-zero exits cause macOS's crash reporter to
pop a dialog every time Claude reconnects; that's the wrong UX for a
nice-to-have channel.

All the actual MCP server logic lives in periscope's server.py; this file
is the documented stdio entry point Claude requires.
"""

import json
import os
import socket
import sys
import threading


SOCKET_PATH = "/tmp/periscope-mcp.sock"
TMUX_PANE = os.environ.get("TMUX_PANE", "")


def _quiet_exit(reason: str, code: int = 0) -> None:
    """Exit cleanly without tripping macOS's crash reporter.

    Reason goes to stderr (visible in Claude's MCP-server logs) but the
    exit code stays 0 so the OS doesn't flag this as a Python crash."""
    print(f"channel_shim: {reason}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    if not TMUX_PANE.startswith("%"):
        # No tmux pane → periscope can't address this Claude. Bail cleanly;
        # the Claude session still works, just without the periscope MCP.
        _quiet_exit(
            f"TMUX_PANE missing or malformed ({TMUX_PANE!r}); "
            "periscope MCP inactive for this session"
        )

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        _quiet_exit(
            f"can't reach periscope at {SOCKET_PATH} ({e}); "
            "is `uv run server.py` running?"
        )

    # Hello frame — periscope reads exactly one line on accept() to learn
    # which pane this connection is for.
    try:
        hello = (json.dumps({"pane": TMUX_PANE}) + "\n").encode()
        sock.sendall(hello)
    except OSError as e:
        _quiet_exit(f"failed to send hello frame ({e})")

    # Cascade pattern: when stdin closes (Claude exited), T1 shuts down the
    # socket's write side. Periscope's server detects EOF, finishes any
    # in-flight response, and closes its end. T2 (main thread) detects
    # socket EOF, returns, and the process exits naturally.
    #
    # This avoids os._exit() — which triggers macOS's crash reporter even
    # on code 0 because it bypasses Python's normal shutdown sequence.
    def stdin_to_socket() -> None:
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    break
                try:
                    sock.sendall(chunk)
                except (BrokenPipeError, OSError):
                    break
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=stdin_to_socket, daemon=True).start()

    # Main thread: socket → stdout. When socket EOFs (periscope's response
    # stream closes, either from a clean shutdown or because periscope
    # crashed), we return and the process exits. The daemon stdin-reader
    # thread dies with us.
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            try:
                os.write(1, chunk)
            except (BrokenPipeError, OSError):
                # Claude closed stdout; nothing more to deliver.
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
