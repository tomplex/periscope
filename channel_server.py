# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.27.*",
#   "httpx",
#   "websockets>=15,<17",
# ]
# ///
"""Periscope channel server.

Runs as a stdio child of `claude` when launched with
`claude --dangerously-load-development-channels server:periscope`.
Bridges Claude's MCP transport to periscope's HTTP/WS API:

  - Pushes from periscope (via WS /ws/channel) become MCP
    `notifications/claude/channel` events that Claude sees as
    `<channel source="periscope" ...>` blocks on the next turn.
  - Calls to the `reply` tool POST to periscope's /api/channel/reply
    and surface in the pane's modal messages strip.

See docs/superpowers/specs/2026-05-14-channels-design.md.
"""

import anyio
import asyncio
import json
import os
import sys


def _assert_sdk_compatibility() -> None:
    """Fail loud if the MCP SDK's private API has shifted away from what we
    rely on. Runs at import time so this error fires before any notification
    is attempted; the deeper guard is the smoke test in tests/."""
    import inspect
    from mcp.shared.session import BaseSession
    sig = inspect.signature(BaseSession.__init__)
    if "write_stream" not in sig.parameters:
        raise RuntimeError(
            "mcp SDK version mismatch: BaseSession.__init__ no longer "
            "accepts write_stream. Pinned: mcp==1.27.*. "
            "Update channel_server.py to match the new SDK API."
        )


_assert_sdk_compatibility()


import httpx
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp import types
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    ServerCapabilities,
    ToolsCapability,
)
from websockets.asyncio.client import connect as ws_connect


TMUX_PANE = os.environ.get("TMUX_PANE", "")
PERISCOPE_URL = os.environ.get("PERISCOPE_URL", "http://127.0.0.1:8765")
PERISCOPE_WS = PERISCOPE_URL.replace("http://", "ws://").replace("https://", "wss://")

INSTRUCTIONS = """\
Messages from periscope arrive as <channel source="periscope" ...> blocks
on each turn. They may include severity, kind, or other meta attributes.

Use the `reply` tool to surface status back to periscope's UI:
  - kind="need_human" when blocked and waiting on the user
  - kind="done" when the current task is complete
  - kind="info" (default) for everything else

A <channel> block with meta.kind="dropped" is an infrastructure
notice — periscope's queue overflowed and dropped earlier events.
It is not a message from the user.

The pane this channel is attached to is identified by $TMUX_PANE on
the server side; you don't need to address it explicitly.
"""


server = Server("periscope")


async def main():
    if not TMUX_PANE.startswith("%"):
        print(
            f"channel_server: TMUX_PANE is missing or malformed ({TMUX_PANE!r}); "
            "this server must be launched as a child of a tmux pane.",
            file=sys.stderr,
        )
        sys.exit(2)

    init_options = InitializationOptions(
        server_name="periscope",
        server_version="0.1.0",
        capabilities=ServerCapabilities(
            experimental={"claude/channel": {}},
            tools=ToolsCapability(listChanged=False),
        ),
        instructions=INSTRUCTIONS,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    anyio.run(main)
