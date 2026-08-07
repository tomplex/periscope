"""Self-update: how far behind origin the checkout is, and driving the update.

Two halves, and the nag is the load-bearing one — a coworker daily-driving
periscope had gone many commits stale simply because nothing ever told him to
pull. `check()` counts commits behind upstream (throttled to hourly, run from
the activity worker's tick) and the count surfaces on /api/state as a header
pill.

`start()` runs `bin/periscope update` DETACHED (`start_new_session=True`).
That is not incidental: the script calls `launchctl bootout`, which tears down
the launchd job — an inline or plain-child process would be killed mid-update
by the very teardown it just requested. A new session escapes the job's
process group, so the updater outlives the server it is replacing.

Failure surfacing relies on the script's ordering: `git pull --ff-only` runs
before anything touches launchd, so the common failures (dirty tree, diverged
branch) abort with the server still alive to serve the error back through
`status()`. Only after the pull succeeds does the process become unkillable-
by-design, and by then the outcome is visible as a changed SHA.
"""

import subprocess
import threading
import time
from pathlib import Path

from periscope import config
from periscope.log import log

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hourly. The check forks a `git fetch`, so it is not free, and a checkout
# that is 40 commits behind does not become urgent within the hour.
CHECK_INTERVAL_S = 3600

_LOCK = threading.Lock()
_checked_at = 0.0
_behind = 0
_proc: subprocess.Popen | None = None


def log_path() -> Path:
    return config.config_dir() / "update.log"


def _git(*args: str, timeout: float = 10.0) -> str | None:
    """Run git in the checkout. None on any failure — every caller treats a
    failed probe as 'no update information', never as an error worth raising."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def check(force: bool = False) -> int:
    """Fetch and recount commits behind upstream. Returns the count (0 when
    up to date or when the state can't be determined). Throttled to
    CHECK_INTERVAL_S unless `force`. Blocking — call from a worker thread."""
    global _checked_at, _behind
    with _LOCK:
        if not force and time.time() - _checked_at < CHECK_INTERVAL_S:
            return _behind
        _checked_at = time.time()
    # Compare against the tracked upstream rather than a hardcoded origin/main,
    # matching the `git pull --ff-only` the update itself will run.
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        return 0                      # detached HEAD or no tracking branch
    if _git("fetch", "--quiet", timeout=30.0) is None:
        return 0                      # offline, or no credentials — stay quiet
    count = _git("rev-list", "--count", f"HEAD..{upstream}")
    behind = int(count) if count and count.isdigit() else 0
    with _LOCK:
        _behind = behind
    return behind


def running() -> bool:
    """True while a spawned updater is still alive. Naturally self-clearing: a
    SUCCESSFUL update kills this process, so the replacement boots with no
    handle and a changed SHA. A FAILED update leaves the handle here, exited,
    with the reason in the log."""
    with _LOCK:
        return _proc is not None and _proc.poll() is None


def summary() -> dict:
    """The nag, without the log. Rides on every /api/state poll (3s), so it
    must stay allocation-cheap and touch no disk."""
    with _LOCK:
        behind, checked_at = _behind, _checked_at
    return {"behind": behind, "checked_at": checked_at, "running": running()}


def status() -> dict:
    """summary() plus the current run's transcript — for the on-demand
    /api/update/status probe, which is the only caller that needs the log."""
    return {**summary(), "log": tail()}


def tail(limit: int = 40) -> list[str]:
    try:
        lines = log_path().read_text(errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def start() -> None:
    """Spawn the detached updater. Raises RuntimeError if this instance must
    not self-update or one is already in flight."""
    global _proc
    # Prod-only. A dev instance runs from a worktree on a feature branch, where
    # `git pull --ff-only` would either fail or pull the WRONG branch over the
    # work in progress.
    if not config.is_prod():
        raise RuntimeError("self-update is prod-only (this is a dev instance)")
    with _LOCK:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("an update is already running")
        script = REPO_ROOT / "bin" / "periscope"
        log_path().parent.mkdir(parents=True, exist_ok=True)
        # Truncate: the log is the CURRENT run's transcript, which is what
        # status() reports back to a dashboard asking "why did that fail?".
        handle = log_path().open("w")
        try:
            _proc = subprocess.Popen(
                [str(script), "update"],
                cwd=str(REPO_ROOT),
                stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,   # survive the bootout it will trigger
            )
        finally:
            handle.close()               # the child holds its own dup
    log.info("self-update started (pid %d)", _proc.pid)
