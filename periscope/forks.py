"""Every fork the server makes, counted by command over a rolling minute.

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
