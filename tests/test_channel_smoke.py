# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.27.*",
#   "anyio",
#   "httpx",
#   "websockets>=15,<17",
# ]
# ///
"""Smoke test: channel_server.py emits notifications/claude/channel correctly.

Verifies the wire wrappers (JSONRPCNotification → JSONRPCMessage →
SessionMessage) round-trip cleanly through `emit_channel_event` against
the pinned mcp==1.27.*. Combined with the `_assert_sdk_compatibility()`
startup check in channel_server.py, this catches the most likely failure
modes of an SDK upgrade.

This does NOT exercise the real ServerSession path — it uses a hand-rolled
fake session whose `_write_stream` is an in-memory anyio stream. The fake
is a representative shim; a regression in how `SessionMessage` is consumed
by the actual `stdio_server` would not be caught here. Combine with the
manual end-to-end check from Task 9 Step 5 for that case.

Run with: uv run --script tests/test_channel_smoke.py
"""

import os
import sys
from pathlib import Path

import anyio


REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNEL_SERVER = REPO_ROOT / "channel_server.py"


async def main():
    # channel_server.py imports httpx and websockets at module top level; the
    # PEP-723 header above carries those transitive deps. We also stub
    # TMUX_PANE so channel_server.py imports cleanly (otherwise its module
    # body is fine, but the main()-guard isn't hit during import).
    os.environ.setdefault("TMUX_PANE", "%99")
    sys.path.insert(0, str(REPO_ROOT))
    from channel_server import emit_channel_event  # noqa: E402

    from mcp.shared.message import SessionMessage

    # In-memory anyio object stream pair (matches what the SDK uses).
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=10)

    class FakeSession:
        _write_stream = send_stream

    await emit_channel_event(
        FakeSession(),
        content="test event",
        meta={"severity": "good", "kind": "info"},
    )

    received = await receive_stream.receive()
    assert isinstance(received, SessionMessage), f"expected SessionMessage, got {type(received)}"

    # The wrapped message is a JSONRPCMessage (Pydantic RootModel); .root
    # holds the notification.
    inner = received.message.root
    assert inner.method == "notifications/claude/channel", inner.method
    assert inner.params == {
        "content": "test event",
        "meta": {"severity": "good", "kind": "info"},
    }, inner.params

    print("PASS: channel_server emits notifications/claude/channel correctly")


if __name__ == "__main__":
    anyio.run(main)
