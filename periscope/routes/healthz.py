"""GET /api/healthz — liveness probe with metadata.

Returns pid, port, uptime, and git short-SHA. Used as a quick "is this
periscope alive and which version" check from `bin/periscope status`
and as a future frontend reconnect probe.
"""

import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter

from periscope import config

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
_VERSION = _git_short_sha()


@router.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "pid": os.getpid(),
        "port": config.PORT,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
        "version": _VERSION,
    }
