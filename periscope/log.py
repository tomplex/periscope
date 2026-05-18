"""Logging + background-task crash capture.

Logging: rotating file at ~/.config/periscope/periscope.log + stderr. Set
up at import time so module-init, lifespan, and handlers all land in the
same sink. Background-task wrappers (_bg / _task) hoist exceptions from
fire-and-forget threads / coroutines into the log so they don't vanish.
"""

import asyncio
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from periscope import config


def _log_path() -> Path:
    # Read config.PORT via the module (not a snapshot import) so tests
    # can monkeypatch periscope.config.PORT and see the new value here.
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / f"periscope-{config.PORT}.log"


_LOG_PATH = _log_path()
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=2_000_000, backupCount=3
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("periscope")


def _bg(name: str, fn, *args, **kwargs) -> threading.Thread:
    """Start a daemon thread that logs any uncaught exception."""
    def wrapped():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("background thread %s crashed", name)
    t = threading.Thread(target=wrapped, daemon=True, name=name)
    t.start()
    return t


def _task(coro, name: str) -> asyncio.Task:
    """Schedule an asyncio task with a done-callback that logs crashes."""
    t = asyncio.create_task(coro, name=name)

    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("task %s crashed", name, exc_info=exc)

    t.add_done_callback(_done)
    return t
