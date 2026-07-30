"""Conservative Codex lifecycle state derived from rollout JSONL.

Hook events are intentionally not consumed here.  The Codex 0.146.0 evidence
gate has not established that root and subagent hook events are distinguishable,
so enabling them would risk binding a pane to the wrong session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

CodexState = Literal["working", "idle", "unknown"]
ProcessState = Literal["live", "dead", "unknown"]
EdgeKind = Literal["working", "idle"]

# Hard gate.  Change only after fixture-backed subagent discrimination is
# verified and hook-event reconciliation tests have landed.
HOOK_EVENT_REDUCTION_VERIFIED = False

MAX_READ_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_CURSORS = 256
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StateEdge:
    session_id: str
    turn_id: str
    kind: EdgeKind
    source: Literal["rollout", "hook", "tui"]
    order: int


@dataclass(frozen=True)
class ReconciledState:
    state: CodexState
    session_id: str
    active_turn_id: str | None
    source: Literal["rollout", "hook", "tui"] | None


@dataclass(frozen=True)
class RolloutCursor:
    device: int
    inode: int
    offset: int
    partial_line: bytes
    session_id: str | None
    active_turn_id: str | None
    last_edge: StateEdge | None
    cli_version: str | None
    lifecycle_seen: bool
    invalid: bool = False


_lock = threading.Lock()
_cursors: dict[Path, RolloutCursor] = {}


def _empty_cursor(stat: os.stat_result) -> RolloutCursor:
    return RolloutCursor(
        device=stat.st_dev,
        inode=stat.st_ino,
        offset=0,
        partial_line=b"",
        session_id=None,
        active_turn_id=None,
        last_edge=None,
        cli_version=None,
        lifecycle_seen=False,
    )


def _safe_rollout_path(path: Path, sessions_root: Path) -> Path | None:
    """Resolve a rollout only when it remains beneath the configured root."""
    try:
        resolved_root = sessions_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        stat = resolved.stat()
        root_stat = resolved_root.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    # A hard-linked outside file cannot be identified portably from stat alone.
    # Reject multiply-linked rollouts rather than trusting a supplied path.
    if stat.st_nlink != 1 or stat.st_dev != root_stat.st_dev:
        return None
    return resolved


def _reduce_line(cursor: RolloutCursor, raw: bytes, order: int) -> RolloutCursor:
    if not raw.strip():
        return cursor
    if len(raw) > MAX_LINE_BYTES:
        log.debug("ignoring oversized Codex rollout record")
        return cursor
    try:
        row = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.debug("ignoring malformed complete Codex rollout record")
        return cursor
    if not isinstance(row, dict):
        return cursor
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return cursor

    if row.get("type") == "session_meta":
        session_id = payload.get("session_id") or payload.get("id")
        originator = payload.get("originator")
        if (
            not isinstance(session_id, str)
            or not _UUID.fullmatch(session_id)
            or originator != "codex-tui"
        ):
            return replace(cursor, invalid=True)
        cli_version = payload.get("cli_version")
        return replace(
            cursor,
            session_id=session_id,
            cli_version=cli_version if isinstance(cli_version, str) else None,
        )

    if row.get("type") != "event_msg" or cursor.session_id is None:
        return cursor
    event = payload.get("type")
    turn_id = payload.get("turn_id")
    if event not in {"task_started", "task_complete"} or not isinstance(
        turn_id, str
    ):
        return cursor
    if event == "task_started":
        return replace(
            cursor,
            active_turn_id=turn_id,
            last_edge=StateEdge(
                cursor.session_id, turn_id, "working", "rollout", order
            ),
            lifecycle_seen=True,
        )
    # A stale/unrelated completion is not an idle edge for the active turn.
    if cursor.active_turn_id != turn_id:
        return cursor
    return replace(
        cursor,
        active_turn_id=None,
        last_edge=StateEdge(cursor.session_id, turn_id, "idle", "rollout", order),
        lifecycle_seen=True,
    )


def rollout_edge_for(
    path: Path, *, session_id: str, sessions_root: Path
) -> StateEdge | None:
    """Read only appended rollout bytes and return the latest valid edge.

    Missing, unsafe, oversized, truncated-without-a-complete-rescan, or
    mismatched metadata produces no opinion.
    """
    resolved = _safe_rollout_path(path, sessions_root)
    if resolved is None:
        return None
    try:
        stat = resolved.stat()
    except OSError:
        return None

    with _lock:
        cursor = _cursors.get(resolved)
        if (
            cursor is None
            or cursor.device != stat.st_dev
            or cursor.inode != stat.st_ino
            or stat.st_size < cursor.offset
        ):
            cursor = _empty_cursor(stat)
        if stat.st_size - cursor.offset > MAX_READ_BYTES:
            return None
        try:
            with resolved.open("rb") as fh:
                fh.seek(cursor.offset)
                chunk = fh.read(MAX_READ_BYTES + 1)
        except OSError:
            return None
        if len(chunk) > MAX_READ_BYTES:
            return None

        start_offset = cursor.offset - len(cursor.partial_line)
        data = cursor.partial_line + chunk
        complete, separator, partial = data.rpartition(b"\n")
        if not separator:
            if len(data) > MAX_LINE_BYTES:
                cursor = replace(
                    cursor, offset=cursor.offset + len(chunk), partial_line=b""
                )
            else:
                cursor = replace(
                    cursor, offset=cursor.offset + len(chunk), partial_line=data
                )
        else:
            position = start_offset
            for line in complete.splitlines(keepends=True):
                cursor = _reduce_line(cursor, line.rstrip(b"\r\n"), position)
                position += len(line)
            cursor = replace(
                cursor,
                offset=cursor.offset + len(chunk),
                partial_line=partial,
            )
        _cursors[resolved] = cursor
        while len(_cursors) > MAX_CURSORS:
            _cursors.pop(next(iter(_cursors)))

        if cursor.invalid or cursor.session_id != session_id:
            return None
        return cursor.last_edge


def reconcile_codex_state(
    *,
    session_id: str,
    process: ProcessState,
    rollout_edge: StateEdge | None,
    hook_edge: StateEdge | None = None,
    tui_marker: StateEdge | None = None,
    now_ms: int | None = None,
) -> ReconciledState | None:
    """Condition lifecycle evidence on liveness without inventing ordering."""
    del now_ms  # Reserved for a future, evidence-derived hook settle interval.
    if process == "dead":
        return None
    if process == "unknown":
        return ReconciledState("unknown", session_id, None, None)

    # The hook safety gate is unresolved.  Refuse hook input rather than
    # silently treating an unverified source as authoritative.
    if hook_edge is not None and not HOOK_EVENT_REDUCTION_VERIFIED:
        return ReconciledState("unknown", session_id, None, None)

    edges = [edge for edge in (rollout_edge, hook_edge, tui_marker) if edge]
    if not edges:
        return ReconciledState("unknown", session_id, None, None)
    if any(edge.session_id != session_id for edge in edges):
        return ReconciledState("unknown", session_id, None, None)

    turns = {edge.turn_id for edge in edges}
    if len(turns) != 1:
        return ReconciledState("unknown", session_id, None, None)
    turn_id = next(iter(turns))

    # Only order within one source.  Exact same-turn idle causally closes a
    # working edge; otherwise conflicting cross-source evidence is unknown.
    kinds = {edge.kind for edge in edges}
    if kinds == {"working", "idle"}:
        sources = {edge.source for edge in edges}
        if len(sources) == 1:
            latest = max(edges, key=lambda edge: edge.order)
            state = latest.kind
            source = latest.source
        else:
            state = "idle"
            source = next(edge.source for edge in edges if edge.kind == "idle")
    else:
        edge = max(edges, key=lambda item: item.order)
        state = edge.kind
        source = edge.source
    return ReconciledState(
        state, session_id, turn_id if state == "working" else None, source
    )


def clear_rollout_cache() -> None:
    with _lock:
        _cursors.clear()
