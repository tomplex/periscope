# /// script
# requires-python = ">=3.11"
# ///
"""Periscope channel shim.

Trivial bidirectional bytes proxy between Claude's stdio MCP transport and
periscope's unix-socket MCP listener. Spawned by Claude as a subprocess
when launched with `claude --dangerously-load-development-channels server:periscope`.

On connect, sends a single hello frame ({"pane": $TMUX_PANE}) so periscope
knows which pane this connection belongs to. Then pumps stdin↔socket in
both directions until either side closes. Exits immediately when either
half-pump terminates — closing stdin (Claude exiting) or losing the socket
(periscope going down) should both tear this down within a few hundred ms.

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


def main() -> None:
    if not TMUX_PANE.startswith("%"):
        print(
            f"channel_shim: TMUX_PANE missing or malformed ({TMUX_PANE!r}); "
            "this script must run as a child of a tmux pane.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(
            f"channel_shim: can't reach periscope at {SOCKET_PATH} ({e}); "
            "is `uv run server.py` running?",
            file=sys.stderr,
        )
        sys.exit(3)

    # Hello frame — periscope reads exactly one line on accept() to learn
    # which pane this connection is for.
    hello = (json.dumps({"pane": TMUX_PANE}) + "\n").encode()
    sock.sendall(hello)

    def stdin_to_socket() -> None:
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        finally:
            # Claude closed stdin → tear the whole process down. (os._exit
            # because we want SIGTERM-like immediacy, not orderly shutdown.)
            os._exit(0)

    def socket_to_stdout() -> None:
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                os.write(1, chunk)
        finally:
            # Socket closed (periscope down or restarted) → exit and let
            # Claude observe stdout EOF.
            os._exit(0)

    threading.Thread(target=stdin_to_socket, daemon=True).start()
    socket_to_stdout()  # runs in main thread so a Python exception surfaces


if __name__ == "__main__":
    main()
