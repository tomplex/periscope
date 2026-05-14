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


# Module-level reference set when MCP request_context first becomes available
# (typically during tools/list right after initialize). T8/T9 read this for
# notification emission.
_ACTIVE_SESSION = None


server = Server("periscope")


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    global _ACTIVE_SESSION
    if _ACTIVE_SESSION is None:
        try:
            _ACTIVE_SESSION = server.request_context.session  # type: ignore[attr-defined]
        except LookupError:
            # Not in a request context yet. Won't happen for list_tools, but
            # the defensive try costs nothing and protects against SDK changes.
            pass
    return [
        types.Tool(
            name="reply",
            description=(
                "Surface a message in periscope's UI for this pane. "
                "Use kind=\"need_human\" when blocked and waiting on the user, "
                "kind=\"done\" when the current task is complete, "
                "otherwise kind=\"info\"."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["info", "need_human", "done"],
                        "default": "info",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "good", "warning", "bad"],
                        "default": "info",
                    },
                },
                "required": ["message"],
            },
        )
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _ACTIVE_SESSION
    if _ACTIVE_SESSION is None:
        try:
            _ACTIVE_SESSION = server.request_context.session  # type: ignore[attr-defined]
        except LookupError:
            pass

    if name != "reply":
        raise ValueError(f"unknown tool: {name}")

    message = arguments["message"]
    kind = arguments.get("kind", "info")
    severity = arguments.get("severity", "info")

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.post(
                f"{PERISCOPE_URL}/api/channel/reply",
                params={"pane": TMUX_PANE},
                json={
                    "message": message,
                    "kind": kind,
                    "severity": severity,
                },
            )
            body = {
                "ok": resp.is_success,
                "status": resp.status_code,
                "kind": kind,
                "severity": severity,
            }
        except httpx.HTTPError as e:
            body = {
                "ok": False,
                "error": f"periscope unreachable: {e}",
                "kind": kind,
                "severity": severity,
            }

    return [types.TextContent(type="text", text=json.dumps(body))]


async def emit_channel_event(session, content: str, meta: dict | None) -> None:
    """Push a notifications/claude/channel notification to Claude.

    The public session.send_notification is constrained to a closed Pydantic
    union of spec-defined notification types and does not accept arbitrary
    method strings. We mirror what the SDK does internally: wrap the
    JSONRPCNotification in a JSONRPCMessage, then a SessionMessage, then send
    via the session's write stream.
    """
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    await session._write_stream.send(
        SessionMessage(message=JSONRPCMessage(notification))
    )


# Bounded buffer for events that arrive over WS before the session is ready.
# Drained into Claude once _ACTIVE_SESSION is set. Bounded so a runaway
# pre-handshake push storm doesn't consume unbounded memory.
_PRE_HANDSHAKE_BUFFER: list[dict] = []
_PRE_HANDSHAKE_MAX = 100


async def _drain_pre_handshake_buffer() -> None:
    """Flush any events that arrived before the session was captured."""
    if _ACTIVE_SESSION is None or not _PRE_HANDSHAKE_BUFFER:
        return
    pending = _PRE_HANDSHAKE_BUFFER[:]
    _PRE_HANDSHAKE_BUFFER.clear()
    for event in pending:
        await emit_channel_event(
            _ACTIVE_SESSION,
            event.get("content", ""),
            event.get("meta") or {},
        )


async def _ws_consumer(url: str) -> None:
    """Subscribe to periscope's /ws/channel and emit each event to Claude.

    Reconnects with exponential backoff capped at 5s. Each connection
    flushes the periscope-side backlog in order before live events resume.
    Events arriving before the MCP handshake completes are queued in
    _PRE_HANDSHAKE_BUFFER and drained when the session becomes available.
    """
    backoff = 0.5
    while True:
        try:
            async with ws_connect(url) as websocket:
                backoff = 0.5  # reset on successful connect
                async for raw in websocket:
                    if not isinstance(raw, str):
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if _ACTIVE_SESSION is None:
                        # Buffer until handshake completes. Drop oldest on
                        # overflow rather than block — the periscope-side
                        # queue is the source of truth.
                        if len(_PRE_HANDSHAKE_BUFFER) >= _PRE_HANDSHAKE_MAX:
                            _PRE_HANDSHAKE_BUFFER.pop(0)
                        _PRE_HANDSHAKE_BUFFER.append(event)
                        continue
                    await _drain_pre_handshake_buffer()
                    await emit_channel_event(
                        _ACTIVE_SESSION,
                        event.get("content", ""),
                        event.get("meta") or {},
                    )
        except Exception as e:
            print(
                f"channel_server: WS to {url} failed ({e}); "
                f"reconnecting in {backoff:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)


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

    ws_url = f"{PERISCOPE_WS}/ws/channel?pane={TMUX_PANE}"

    async with stdio_server() as (read_stream, write_stream):
        async with anyio.create_task_group() as tg:
            # WS consumer runs alongside the MCP loop. When stdin EOFs and
            # server.run() returns, we cancel the task group so the WS task
            # exits and the process can terminate.
            tg.start_soon(_ws_consumer, ws_url)

            await server.run(read_stream, write_stream, init_options)

            # MCP loop ended (stdin EOF or transport error). Cancel siblings
            # so the process exits within seconds rather than hanging on the
            # WS task's infinite reconnect loop.
            tg.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)
