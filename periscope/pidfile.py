"""Pidfile / single-instance reclaim.

Called from server.py's __main__ block BEFORE uvicorn binds the port so
`uv run server.py` is idempotent — starting periscope kicks out the
previous instance.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from periscope.log import log


def _pidfile_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "periscope.pid"


def _pid_is_periscope(pid: int) -> bool:
    """True if `pid` is alive and looks like a periscope process. Checks
    the command line for 'server.py' to avoid SIGTERMing some unrelated
    process that happens to have inherited an old pid."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if out.returncode != 0:
        return False
    return "server.py" in out.stdout


def _reclaim_existing_instance() -> None:
    """If the pidfile points at a live periscope, SIGTERM it (escalate to
    SIGKILL after 3s) so we can bind the port cleanly."""
    path = _pidfile_path()
    try:
        prev = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    if prev == os.getpid() or not _pid_is_periscope(prev):
        return
    log.info("reclaiming previous periscope instance pid=%d", prev)
    try:
        os.kill(prev, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_is_periscope(prev):
            return
        time.sleep(0.1)
    log.warning("pid=%d ignored SIGTERM; sending SIGKILL", prev)
    try:
        os.kill(prev, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _write_pidfile() -> None:
    path = _pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _remove_pidfile() -> None:
    path = _pidfile_path()
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
