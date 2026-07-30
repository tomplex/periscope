"""Process-tree evidence for interactive coding agents.

This module is deliberately stdlib-only.  A failed ``ps`` invocation means
"no opinion" rather than "the agent exited"; callers use ``None`` to preserve
that distinction.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    started_at: int
    comm: str
    argv: str
    state: str


_PS_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+"
    r"(\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+"
    r"(\S+)\s+(\S+)(?:\s+(.*))?$"
)
_PS_TTL_S = 1.0
_lock = threading.Lock()
_snapshot_cache: tuple[float, dict[int, ProcessInfo] | None] | None = None
# pane_id -> (pane pid, pane start, agent pid, agent start)
_pane_cache: dict[str, tuple[int, int, int, int]] = {}


def parse_ps_snapshot(output: str) -> dict[int, ProcessInfo]:
    """Parse macOS ``ps`` output emitted by :func:`_take_snapshot`."""
    result: dict[int, ProcessInfo] = {}
    for line in output.splitlines():
        match = _PS_ROW_RE.match(line)
        if not match:
            continue
        pid, ppid, started, state, comm, argv = match.groups()
        try:
            started_at = int(
                datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
            )
        except ValueError:
            continue
        info = ProcessInfo(
            pid=int(pid),
            ppid=int(ppid),
            started_at=started_at,
            comm=comm,
            argv=argv or comm,
            state=state,
        )
        result[info.pid] = info
    return result


def _take_snapshot() -> dict[int, ProcessInfo] | None:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,state=,comm=,args="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ps_snapshot(proc.stdout)


def process_snapshot() -> dict[int, ProcessInfo] | None:
    """Return a briefly cached system process snapshot."""
    global _snapshot_cache
    now = time.monotonic()
    with _lock:
        if _snapshot_cache and now - _snapshot_cache[0] < _PS_TTL_S:
            return _snapshot_cache[1]
        snapshot = _take_snapshot()
        _snapshot_cache = (now, snapshot)
        return snapshot


def _is_codex_executable(proc: ProcessInfo) -> bool:
    # Match the executable itself, never an arbitrary mention in argv.
    return os.path.basename(proc.comm).lower() in {"codex", "codex.exe"}


def _descendants(root_pid: int, snapshot: dict[int, ProcessInfo]):
    children: dict[int, list[ProcessInfo]] = {}
    for proc in snapshot.values():
        children.setdefault(proc.ppid, []).append(proc)
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        proc = snapshot.get(parent)
        if proc is not None:
            yield proc
        pending.extend(child.pid for child in children.get(parent, ()))


def codex_process_for_pane(pane_id: str, pane_pid: int | str | None) -> bool | None:
    """Return True/False for verified process evidence, or None on ps failure.

    PID start timestamps are part of every cached identity, preventing a
    recycled PID from inheriting a previous pane's Codex classification.
    """
    try:
        root_pid = int(pane_pid or 0)
    except (TypeError, ValueError):
        return None
    if root_pid <= 0:
        return None

    snapshot = process_snapshot()
    if snapshot is None:
        return None
    root = snapshot.get(root_pid)
    if root is None or root.state.upper().startswith("Z"):
        _pane_cache.pop(pane_id, None)
        return False

    cached = _pane_cache.get(pane_id)
    if cached and cached[:2] == (root_pid, root.started_at):
        agent = snapshot.get(cached[2])
        if (
            agent
            and agent.started_at == cached[3]
            and not agent.state.upper().startswith("Z")
            and _is_codex_executable(agent)
        ):
            return True
        _pane_cache.pop(pane_id, None)

    for proc in _descendants(root_pid, snapshot):
        if not proc.state.upper().startswith("Z") and _is_codex_executable(proc):
            _pane_cache[pane_id] = (
                root_pid,
                root.started_at,
                proc.pid,
                proc.started_at,
            )
            return True
    return False


def clear_caches() -> None:
    """Test/support hook; runtime callers normally rely on TTL/invalidation."""
    global _snapshot_cache
    with _lock:
        _snapshot_cache = None
        _pane_cache.clear()
