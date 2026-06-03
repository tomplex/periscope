"""tmux_input — control-mode keystroke delivery.

The wire format (`send-keys -t '<target>' -H <hex...>`) is the regression-
prone surface, same spirit as the channel smoke test: if it drifts, every
keystroke through the dashboard silently breaks. The round-trip test proves
the control client actually delivers bytes into a real pane.
"""

import asyncio
import shutil
import subprocess
import uuid

import pytest

from periscope import tmux_input
from periscope.config import INPUT_CTL_SESSION


def test_cmd_wire_format():
    # "hi" -> 68 69; target single-quoted; trailing newline submits the command.
    assert tmux_input._cmd("sess:1.0", b"hi") == b"send-keys -t 'sess:1.0' -H 68 69\n"


def test_cmd_handles_slash_session_and_escapes():
    # Session names with slashes (tc/foo/bar) and raw escape bytes (\x1b[A) must
    # round-trip as hex without the target being mangled.
    line = tmux_input._cmd("tc/foo/bar:2.1", b"\x1b[A")
    assert line == b"send-keys -t 'tc/foo/bar:2.1' -H 1b 5b 41\n"


def test_disabled_falls_back_to_fork(monkeypatch):
    # When control mode is disabled, send() must route to the fork path and
    # never touch the control client.
    monkeypatch.setattr(tmux_input, "_disabled", True)
    seen = {}

    async def fake_fork(target, text):
        seen["target"], seen["text"] = target, text

    monkeypatch.setattr(tmux_input, "_fork_fallback", fake_fork)
    asyncio.run(tmux_input.send("sess:1.0", "x"))
    assert seen == {"target": "sess:1.0", "text": "x"}


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_roundtrip_into_real_pane():
    target_session = f"pstest-{uuid.uuid4().hex[:8]}"

    async def drive():
        # A throwaway target pane running `cat` so sent bytes echo into its
        # capture-pane output verbatim (no shell prompt interpretation).
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", target_session, "-x", "80", "-y", "24", "cat"],
            check=True,
        )
        # base-index may be 0 or 1 depending on the host's tmux config; ask
        # tmux for the real window index rather than assuming.
        idx = subprocess.run(
            ["tmux", "list-windows", "-t", target_session, "-F", "#{window_index}"],
            capture_output=True, text=True,
        ).stdout.strip()
        target = f"{target_session}:{idx}"
        await asyncio.sleep(0.2)
        await tmux_input.send(target, "hello")
        await asyncio.sleep(0.3)
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True, text=True,
        ).stdout
        return out

    try:
        out = asyncio.run(drive())
        assert "hello" in out
    finally:
        asyncio.run(tmux_input.shutdown())
        subprocess.run(["tmux", "kill-session", "-t", target_session], check=False)
        subprocess.run(["tmux", "kill-session", "-t", INPUT_CTL_SESSION], check=False)
