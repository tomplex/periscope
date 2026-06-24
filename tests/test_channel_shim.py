"""Integration tests for channel_shim.py.

Spawns the shim as a real subprocess against a fake unix-socket server,
exercises the reconnect path that lets Claude's MCP session survive a
periscope restart without /clear.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeServer:
    """Minimal asyncio unix server that mimics periscope's MCP wire shape:
    reads a hello frame, then auto-responds to whitelisted JSON-RPC methods.
    Records every line received per connection so tests can assert on what
    the shim sent."""

    def __init__(self, socket_path: str, respond_to: set[str] | None = None):
        self._socket_path = socket_path
        # None = respond to all requests; otherwise only respond to listed
        # methods (lets tests force an in-flight request to orphan).
        self._respond_to = respond_to
        self._server: asyncio.base_events.Server | None = None
        # One record per accepted connection: {"pane": ..., "lines": [bytes, ...]}.
        self.records: list[dict] = []
        self._conn_event = asyncio.Event()

    async def start(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle, path=self._socket_path
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # close_clients yanks open connections so per-conn tasks unblock.
        # Python 3.13+; getattr keeps the test runnable on stubs that lag.
        close_clients = getattr(self._server, "close_clients", None)
        if close_clients is not None:
            close_clients()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
        except (TimeoutError, Exception):
            pass
        self._server = None

    async def wait_for_connection(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._conn_event.wait(), timeout=timeout)
        self._conn_event.clear()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        record: dict = {"pane": None, "lines": []}
        self.records.append(record)
        try:
            hello = await reader.readline()
            try:
                record["pane"] = json.loads(hello).get("pane")
            except Exception:
                pass
            self._conn_event.set()
            while True:
                line = await reader.readline()
                if not line:
                    return
                record["lines"].append(line)
                msg = _parse(line)
                if not msg or "id" not in msg or "method" not in msg:
                    continue
                method = msg["method"]
                if self._respond_to is not None and method not in self._respond_to:
                    continue
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-periscope", "version": "0.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": []}
                else:
                    result = {"echoed": method}
                writer.write(
                    (json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n").encode()
                )
                try:
                    await writer.drain()
                except (OSError, BrokenPipeError):
                    return
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _parse(line: bytes) -> dict | None:
    try:
        msg = json.loads(line)
    except Exception:
        return None
    return msg if isinstance(msg, dict) else None


async def _spawn_shim(socket_path: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(REPO_ROOT / "channel_shim.py"),
        env={
            "TMUX_PANE": "%42",
            "PERISCOPE_MCP_SOCKET_PATH": socket_path,
            "PERISCOPE_MCP_RECONNECT_BACKOFF_S": "0.05",
            "PATH": os.environ.get("PATH", ""),
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _send(proc: asyncio.subprocess.Process, msg: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    await proc.stdin.drain()


async def _recv(proc: asyncio.subprocess.Process, timeout: float = 2.0) -> dict:
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    parsed = _parse(line)
    assert parsed is not None, f"non-JSON line from shim: {line!r}"
    return parsed


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()


def _methods_in(lines: list[bytes]) -> list[str]:
    out = []
    for ln in lines:
        msg = _parse(ln)
        if msg and isinstance(msg.get("method"), str):
            out.append(msg["method"])
    return out


@pytest.fixture
def short_sock(request):
    # macOS caps AF_UNIX path length around 104 chars and pytest's default
    # tmp_path is typically way longer than that. Route each test to a short
    # path under /tmp instead.
    path = f"/tmp/periscope-shim-test-{os.getpid()}-{abs(hash(request.node.name)) % 100000}.sock"
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
    yield path
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def test_basic_handshake_passes_through(short_sock):
    sock = short_sock

    async def run() -> None:
        server = FakeServer(sock)
        await server.start()
        proc = await _spawn_shim(sock)
        try:
            await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = await _recv(proc)
            assert resp["id"] == 1
            assert resp["result"]["serverInfo"]["name"] == "fake-periscope"
            assert server.records[0]["pane"] == "%42"
            assert "initialize" in _methods_in(server.records[0]["lines"])
        finally:
            await _terminate(proc)
            await server.stop()

    asyncio.run(run())


def test_reconnects_replays_initialize_and_synth_tools_list(short_sock):
    sock = short_sock

    async def run() -> None:
        server1 = FakeServer(sock)
        await server1.start()
        proc = await _spawn_shim(sock)
        try:
            # Initial handshake.
            await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = await _recv(proc)
            assert resp["id"] == 1
            await _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            # Wait for that notification to reach the server before the kill.
            await asyncio.sleep(0.1)

            # Periscope dies.
            await server1.stop()

            # New periscope comes up on the same path.
            server2 = FakeServer(sock)
            await server2.start()
            try:
                await server2.wait_for_connection(timeout=3.0)
                # Give the shim a moment to flush the replay + synth tools/list.
                await asyncio.sleep(0.2)

                rec = server2.records[0]
                assert rec["pane"] == "%42"
                methods = _methods_in(rec["lines"])
                assert "initialize" in methods, methods
                assert "notifications/initialized" in methods, methods
                assert "tools/list" in methods, methods

                # The replay's initialize response (id=1, from server2) must
                # NOT be forwarded to Claude. A new request with a fresh id
                # should round-trip cleanly.
                await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
                resp2 = await _recv(proc, timeout=3.0)
                assert resp2["id"] == 2, f"expected id=2 (the new request), got {resp2!r}"
            finally:
                await server2.stop()
        finally:
            await _terminate(proc)

    asyncio.run(run())


def test_inflight_request_gets_synthetic_error_on_socket_death(short_sock):
    sock = short_sock

    async def run() -> None:
        # Server only responds to initialize; tools/call requests will sit
        # in flight until we kill the server.
        server = FakeServer(sock, respond_to={"initialize"})
        await server.start()
        proc = await _spawn_shim(sock)
        try:
            await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = await _recv(proc)
            assert resp["id"] == 1
            # Send a tool call the server will never answer.
            await _send(proc, {"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {}})
            await asyncio.sleep(0.1)

            # Kill the server; the shim should synth an error for id=99.
            await server.stop()
            resp = await _recv(proc, timeout=3.0)
            assert resp["id"] == 99
            assert "error" in resp
            assert resp["error"]["code"] == -32099
        finally:
            await _terminate(proc)

    asyncio.run(run())


def test_exits_cleanly_on_missing_tmux_pane(short_sock):
    sock = short_sock

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(REPO_ROOT / "channel_shim.py"),
            env={
                "TMUX_PANE": "",  # the failure mode under test
                "PERISCOPE_MCP_SOCKET_PATH": sock,
                "PERISCOPE_MCP_RECONNECT_BACKOFF_S": "0.05",
                "PATH": os.environ.get("PATH", ""),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            proc.terminate()
            await proc.wait()
            pytest.fail("shim did not exit on missing TMUX_PANE")
        # Exit code 0 is the contract — non-zero exits pop macOS's crash
        # reporter every time Claude respawns the shim.
        assert proc.returncode == 0
        assert proc.stderr is not None
        stderr = (await proc.stderr.read()).decode()
        assert "caller id" in stderr

    asyncio.run(run())


def test_stdin_eof_terminates_shim(short_sock):
    sock = short_sock

    async def run() -> None:
        server = FakeServer(sock)
        await server.start()
        proc = await _spawn_shim(sock)
        try:
            # Wait for the shim to connect (so it's in the pump loop).
            await server.wait_for_connection(timeout=2.0)
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                pytest.fail("shim did not exit after stdin EOF")
            assert proc.returncode == 0
        finally:
            await _terminate(proc)
            await server.stop()

    asyncio.run(run())


def test_caller_id_prefers_explicit_handle(monkeypatch):
    import importlib

    import channel_shim
    monkeypatch.setenv("PERISCOPE_CALLER_ID", "cmdr:abc")
    monkeypatch.setenv("TMUX_PANE", "%9")
    importlib.reload(channel_shim)
    assert channel_shim.CALLER_ID == "cmdr:abc"
    monkeypatch.delenv("PERISCOPE_CALLER_ID")
    importlib.reload(channel_shim)
    assert channel_shim.CALLER_ID == "%9"
