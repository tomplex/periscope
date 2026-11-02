"""Every fork the server makes, counted by command over a rolling minute, and
what each dashboard refresh costs (/api/state `refresh`).

Counted by a Python audit hook on `subprocess.Popen` — the event
`subprocess.run`, `check_output` and asyncio's subprocesses all raise — so no
call site changes and background threads count too. It sees only the server's
own forks: what a child spawns (gh's `security`, `gh send-telemetry`) needs a
process-tree sample."""

import contextlib
import os
import re
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Sequence
from typing import Any

from periscope.log import log

_WINDOW_S = 60.0
# The subcommand is the first word-like argument after the binary, so
# `git -C <dir> status` counts as `git status` and `ps -axo pid=,…` as `ps`.
_SUBCOMMAND_RE = re.compile(r"[a-z][a-z0-9-]*")
# Options whose value comes next and could pass for a subcommand
# (`tmux -L <socket>`, `git -C <relative dir>`, `sh -c <script>`).
_VALUE_FLAGS = frozenset({"-C", "-L", "-S", "-c"})

_lock = threading.Lock()
# (monotonic time, command key), oldest first.
_forks: deque[tuple[float, str]] = deque()
_installed = False


def _command_key(argv: Sequence[Any]) -> str:
    args = [os.fsdecode(a) for a in argv]
    key = os.path.basename(args[0])
    skip = False
    for a in args[1:]:
        if skip:
            skip = False
        elif a in _VALUE_FLAGS:
            skip = True
        elif _SUBCOMMAND_RE.fullmatch(a):
            return f"{key} {a}"
    return key


def _prune(q: deque, now: float) -> None:
    while q and q[0][0] < now - _WINDOW_S:
        q.popleft()


def _on_audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "subprocess.Popen":
        return
    # args is (executable, argv, cwd, env). An exception escaping an audit hook
    # aborts the Popen it audits, so nothing below may raise: a fork this
    # can't count goes uncounted.
    with contextlib.suppress(Exception):
        key = _command_key(args[1])
        now = time.monotonic()
        with _lock:
            _forks.append((now, key))
            _prune(_forks, now)


def install() -> None:
    """Start counting. An audit hook can't be removed, and the lifespan runs
    more than once in one process under tests — so add it once."""
    global _installed
    if not _installed:
        sys.addaudithook(_on_audit)
        _installed = True


def fork_counts() -> Counter[str]:
    """Forks per command over the last minute."""
    now = time.monotonic()
    with _lock:
        _prune(_forks, now)
        return Counter(key for _, key in _forks)


# A build this slow is logged with the commands that forked most — at most
# once a minute, since a slow machine makes every build slow.
_SLOW_BUILD_S = 3.0
_SLOW_LOG_EVERY_S = 60.0
_slow_logged_at: float | None = None
# (monotonic time, build_state seconds), oldest first.
_builds: deque[tuple[float, float]] = deque()
# The state hub's last gap between two broadcasts; None until it has made two.
_last_gap: float | None = None


def _secs(s: float | None) -> float | None:
    return None if s is None else round(s, 2)


def record_build(seconds: float) -> None:
    """Note one build_state's duration; a slow one is logged with the commands
    that forked most over the last minute."""
    global _slow_logged_at
    now = time.monotonic()
    with _lock:
        _builds.append((now, seconds))
        log_it = seconds > _SLOW_BUILD_S and (_slow_logged_at is None or now - _slow_logged_at >= _SLOW_LOG_EVERY_S)
        if log_it:
            _slow_logged_at = now
    if log_it:
        log.warning(
            "slow refresh: build_state took %.1f s; top forks in the last minute: %s",
            seconds,
            fork_counts().most_common(5),
        )


def record_gap(seconds: float) -> None:
    """Note the state hub's gap between two broadcasts — how often a dashboard
    actually updates."""
    global _last_gap
    _last_gap = seconds


def refresh_stats() -> dict:
    """/api/state's `refresh` block: the last build, the slowest over the last
    minute, the hub's last gap, and the minute's forks — total and the five
    largest commands."""
    counts = fork_counts()
    now = time.monotonic()
    with _lock:
        _prune(_builds, now)
        last = _builds[-1][1] if _builds else None
        slowest = max((s for _, s in _builds), default=None)
    return {
        "build_s": _secs(last),
        "build_max_s": _secs(slowest),
        "gap_s": _secs(_last_gap),
        "forks_per_min": sum(counts.values()),
        "top_forks": counts.most_common(5),
    }
