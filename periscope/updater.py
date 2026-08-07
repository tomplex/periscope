"""Self-update: how far behind origin the checkout is, and driving the update.

See CLAUDE.md > "Updating" for the design and why a plain `git pull` +
`restart` is not equivalent. The invariants that live in THIS file:

- `start()` spawns DETACHED (`start_new_session=True`). The script calls
  `launchctl bootout`, which tears down the launchd job — a plain child would
  be killed mid-update by the very teardown it requested.
- Both entry points are prod-only. A dev instance runs from a worktree on a
  feature branch, where a pull would clobber work in progress.
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

# An updater still alive past this is wedged, not working: the script's own
# worst case is ~5 min (bootout wait + bootstrap retries + healthz poll) plus
# the pull. Without a cap, one hung `git pull` pins running() true forever and
# every later attempt 409s until the server restarts — days, on a box that is
# only ever restarted BY this feature.
STALE_PROC_S = 15 * 60

_LOCK = threading.Lock()
_checked_at = 0.0
_behind = 0
_proc: subprocess.Popen | None = None
_started_at = 0.0


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
    """Fetch and recount commits behind upstream. Throttled to
    CHECK_INTERVAL_S unless `force`. Blocking — call from a worker thread.

    A probe that can't answer LEAVES THE LAST COUNT STANDING rather than
    resetting to zero: going offline doesn't make the checkout less behind, and
    publishing 0 would claim "up to date", which is the one wrong answer.
    """
    global _checked_at, _behind
    with _LOCK:
        if not force and time.time() - _checked_at < CHECK_INTERVAL_S:
            return _behind
        _checked_at = time.time()
        known = _behind
    # Compare against the tracked upstream rather than a hardcoded origin/main,
    # matching the `git pull --ff-only` the update itself will run.
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        return known                  # detached HEAD or no tracking branch
    if _git("fetch", "--quiet", timeout=30.0) is None:
        return known                  # offline, or no credentials
    count = _git("rev-list", "--count", f"HEAD..{upstream}")
    if not (count and count.isdigit()):
        return known
    behind = int(count)
    with _LOCK:
        _behind = behind
    return behind


def _running_locked() -> bool:
    """Caller must hold _LOCK. A spawned updater counts as running only until
    STALE_PROC_S — past that it is wedged, and treating it as live would 409
    every future attempt forever."""
    if _proc is None or _proc.poll() is not None:
        return False
    return time.time() - _started_at < STALE_PROC_S


def running() -> bool:
    """True while a spawned updater is live. Naturally self-clearing: a
    SUCCESSFUL update kills this process, so the replacement boots with no
    handle and a changed SHA. A FAILED update leaves the handle here, exited,
    with the reason in the log."""
    with _LOCK:
        return _running_locked()


def summary() -> dict:
    """The nag, without the log. Rides on every /api/state poll (3s), so it
    must stay allocation-cheap and touch no disk.

    Reads everything under ONE acquire — `_LOCK` is a plain Lock, so calling
    the public `running()` from inside the block would self-deadlock the
    hottest endpoint in the app.
    """
    with _LOCK:
        return {"behind": _behind, "checked_at": _checked_at,
                "running": _running_locked()}


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
    global _proc, _started_at
    # Prod-only. A dev instance runs from a worktree on a feature branch, where
    # `git pull --ff-only` would either fail or pull the WRONG branch over the
    # work in progress. (`bin/periscope update` refuses from a worktree too —
    # this gate and that one are independent, since the script is also a
    # user-facing verb.)
    if not config.is_prod():
        raise RuntimeError("self-update is prod-only (this is a dev instance)")
    with _LOCK:
        if _running_locked():
            raise RuntimeError("an update is already running")
        # Past STALE_PROC_S but still alive = wedged (a `git pull` blocked on
        # the network). Kill it before spawning, or two updaters race on the
        # same checkout and launchd job.
        if _proc is not None and _proc.poll() is None:
            log.warning("killing wedged updater (pid %d)", _proc.pid)
            _proc.kill()
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
        _started_at = time.time()
        log.info("self-update started (pid %d)", _proc.pid)
