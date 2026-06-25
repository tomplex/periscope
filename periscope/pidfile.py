"""Pidfile / single-instance reclaim.

Called from server.py's __main__ block BEFORE uvicorn binds the port so
`uv run server.py` is idempotent — starting periscope kicks out the
previous instance.
"""

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

from periscope import config
from periscope.log import log


def _pidfile_path() -> Path:
    # config.PORT accessed via module attribute (not snapshot import) so
    # tests can monkeypatch periscope.config.PORT and observe new paths.
    return config.config_dir() / f"periscope-{config.PORT}.pid"


def _pid_is_periscope(os_pid: int) -> bool:
    """True if `os_pid` is alive and looks like a periscope process. Checks
    the command line for 'server.py' to avoid SIGTERMing some unrelated
    process that happens to have inherited an old pid.

    `os_pid` is an OS process id — distinct from the codebase's `pid`,
    which everywhere else is the periscope per-window id (@periscope_id)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(os_pid), "-o", "command="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if out.returncode != 0:
        return False
    return "server.py" in out.stdout


def _reclaim_existing_instance() -> None:
    """If the pidfile points at a live periscope on the same port, SIGTERM
    it (escalate to SIGKILL after 3s) so we can bind the port cleanly.

    Refuses to act when the pidfile's recorded port differs from the
    current PORT — that means the pidfile belongs to a different
    periscope (theoretically impossible given per-port pidfile paths,
    but the safety net against a stale pidfile from a recycled pid).
    Pidfiles without a port line are treated as legacy and reclaimed.
    """
    path = _pidfile_path()
    try:
        text = path.read_text().strip()
    except OSError:
        return
    lines = text.split("\n")
    try:
        prev = int(lines[0])
    except (ValueError, IndexError):
        return
    if len(lines) >= 2:
        try:
            recorded_port = int(lines[1])
        except ValueError:
            recorded_port = None
        if recorded_port is not None and recorded_port != config.PORT:
            log.warning(
                "pidfile %s has port %d, expected %d — refusing reclaim",
                path, recorded_port, config.PORT,
            )
            return
    # Legacy pidfile (no port line) — fall through to reclaim.
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
    with contextlib.suppress(ProcessLookupError):
        os.kill(prev, signal.SIGKILL)


def _write_pidfile() -> None:
    """Pidfile format: '{pid}\\n{port}\\n'. Two lines so reclaim can verify
    it's about to SIGTERM the right port's prior instance."""
    path = _pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n{config.PORT}\n")


def _remove_pidfile() -> None:
    """Only remove if the file's first line matches our pid."""
    path = _pidfile_path()
    try:
        first_line = path.read_text().split("\n", 1)[0].strip()
        if first_line == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
