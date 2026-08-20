"""GET /api/healthz — liveness probe with metadata.

Returns pid, port, uptime, and git short-SHA. Used as a quick "is this
periscope alive and which version" check from `bin/periscope status`
and as a future frontend reconnect probe.
"""

import json
import os
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter

from periscope import config
from periscope.codex_hook_config import EVENTS, codex_home, command_for
from periscope.tmux_persist import status as resurrect_status

router = APIRouter()


def _git_short_sha() -> str:
    """Captured at module load. Falls back to 'unknown' if git isn't on
    PATH or the working tree isn't a git repo — the launchd PATH is
    minimal and the worktree case is well-defined."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent.parent,
            capture_output=True, text=True, timeout=2.0,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"


_BOOT_TS = time.time()
_REPO = Path(__file__).resolve().parent.parent.parent
_VERSION = _git_short_sha()
CODEX_HOOK_STALE_S = 7 * 24 * 60 * 60


def _codex_hook_health() -> dict:
    home = codex_home()
    hook_file = home / "hooks.json"
    expected = command_for(Path(__file__).resolve().parent.parent.parent)
    definitions = {
        event: {"present": False, "target_exists": False} for event in EVENTS
    }
    try:
        with hook_file.open() as stream:
            document = json.load(stream)
        hooks = document.get("hooks", {}) if isinstance(document, dict) else {}
        for event in EVENTS:
            groups = hooks.get(event, []) if isinstance(hooks, dict) else []
            entries = [
                entry
                for group in groups if isinstance(group, dict)
                for entry in group.get("hooks", []) if isinstance(entry, dict)
            ]
            commands = [
                entry.get("command")
                for entry in entries
                if entry.get("type") == "command"
            ]
            present = expected in commands
            target_exists = False
            if present:
                try:
                    argv = shlex.split(expected)
                    target_exists = len(argv) == 2 and Path(argv[1]).is_file()
                except ValueError:
                    pass
            definitions[event] = {
                "present": present,
                "target_exists": target_exists,
            }
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    observed: dict[str, dict] = {}
    try:
        with sqlite3.connect(
            f"file:{config.ACTIVITY_DB}?mode=ro", uri=True, timeout=0.25
        ) as conn:
            rows = conn.execute(
                "SELECT event, MAX(observed_at), "
                "(SELECT cli_version FROM agent_session_events latest "
                " WHERE latest.provider='codex' AND latest.event=e.event "
                " ORDER BY latest.observed_at DESC, latest.id DESC LIMIT 1) "
                "FROM agent_session_events e WHERE provider='codex' "
                "GROUP BY event"
            ).fetchall()
        now = int(time.time())
        for event, last_seen_at, cli_version in rows:
            if event not in EVENTS:
                continue
            observed[event] = {
                "last_seen_at": last_seen_at,
                "cli_version": cli_version,
                "stale": now - last_seen_at > CODEX_HOOK_STALE_S,
            }
    except sqlite3.Error:
        pass
    return {
        "codex_home": str(home),
        "verification": "unresolved",
        "definition": definitions,
        "hook_version": 1,
        "observed": observed,
        # Trust is deliberately absent: hooks.json does not expose it. Hook
        # attribution remains unresolved until live Stage-0 capture verifies
        # TMUX_PANE and subagent behavior.
    }


@router.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "pid": os.getpid(),
        "port": config.PORT,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
        "version": _VERSION,
        "codex_hook": _codex_hook_health(),
        "resurrect": resurrect_status(_REPO),
    }
