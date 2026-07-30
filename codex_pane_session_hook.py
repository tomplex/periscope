#!/usr/bin/env python3
"""Best-effort Codex lifecycle hook for Periscope pane/session bindings.

This command is deliberately silent and always successful: a monitoring hook
must never prevent Codex from accepting a prompt or completing a turn.
"""

import contextlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from periscope import session_binding_db

HOOK_VERSION = 1
EVENTS = frozenset(("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"))


@dataclass(frozen=True)
class HookEvent:
    session_id: str
    session_path: str
    event: str
    turn_id: str | None
    cli_version: str | None


def _codex_home() -> Path:
    # An empty value is not a usable root. Until Codex's empty-value behavior
    # is verified, use the documented default and never inspect cwd-relative
    # paths.
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _root_rollout(path_text: str, session_id: str) -> bool:
    """Conservatively distinguish the interactive root TUI rollout.

    Stage-0 subagent attribution evidence is unavailable. Consequently a hook
    may bind only a rollout under CODEX_HOME/sessions whose own session_meta
    identifies the same session as a codex-tui originator.
    """
    try:
        path = Path(path_text).resolve(strict=True)
        sessions = (_codex_home() / "sessions").resolve(strict=True)
        path.relative_to(sessions)
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            for _ in range(32):
                raw = stream.readline(64 * 1024)
                if not raw or len(raw) >= 64 * 1024:
                    break
                record = json.loads(raw)
                if record.get("type") != "session_meta":
                    continue
                meta = record.get("payload") or {}
                meta_id = meta.get("session_id") or meta.get("id")
                return (
                    meta_id == session_id
                    and meta.get("originator") == "codex-tui"
                )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return False


def parse_payload(stdin: TextIO) -> HookEvent | None:
    try:
        payload = json.load(stdin)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    session_path = payload.get("transcript_path")
    if (
        event not in EVENTS
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(session_path, str)
        or not session_path
        or not _root_rollout(session_path, session_id)
    ):
        return None
    turn_id = payload.get("turn_id")
    cli_version = payload.get("cli_version")
    return HookEvent(
        session_id=session_id,
        session_path=session_path,
        event=event,
        turn_id=turn_id if isinstance(turn_id, str) and turn_id else None,
        cli_version=(
            cli_version if isinstance(cli_version, str) and cli_version else None
        ),
    )


def record(event: HookEvent, *, pane_id: str, db_path: Path) -> None:
    now = int(time.time())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=2.0) as conn:
        session_binding_db.ensure_schema(conn)
        current = session_binding_db.get_binding(conn, pane_id)
        # Until root/subagent attribution is proven, a later hook session must
        # not replace an existing pane binding. This intentionally means
        # resume/clear repair is disabled during the evidence-gate period.
        if current is None or (
            current.provider == "codex" and current.session_id == event.session_id
        ):
            session_binding_db.upsert_binding(
                conn,
                session_binding_db.AgentSessionBinding(
                    pane_id=pane_id,
                    provider="codex",
                    session_id=event.session_id,
                    session_path=event.session_path,
                    updated_at=now,
                    # Stage-0 has not yet proven hook/subagent attribution. This
                    # binding is captured for verification but must not be
                    # treated as authoritative by state reconciliation.
                    evidence="codex-hook-unverified",
                ),
            )
        session_binding_db.append_hook_event(
            conn,
            session_binding_db.AgentHookEvent(
                pane_id=pane_id,
                provider="codex",
                session_id=event.session_id,
                turn_id=event.turn_id,
                event=event.event,
                hook_version=HOOK_VERSION,
                cli_version=event.cli_version,
                observed_at=now,
            ),
        )


def main() -> None:
    with contextlib.suppress(Exception):
        pane = os.environ.get("TMUX_PANE", "")
        if not pane.startswith("%"):
            return
        event = parse_payload(sys.stdin)
        if event is None:
            return
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        record(event, pane_id=pane, db_path=base / "periscope" / "periscope.db")


if __name__ == "__main__":
    main()
