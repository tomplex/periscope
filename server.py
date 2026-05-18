# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "anthropic",
#     "httpx",
#     "python-dotenv",
#     "mcp==1.27.*",
#     "websockets>=15,<17",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py

The FastAPI app lives in periscope/app.py; this file is the entry-point
shim. It handles pidfile reclaim + signal install (which must happen
BEFORE uvicorn binds the port) and then hands off to uvicorn with the
periscope.app:app import string.

Nothing in periscope/ imports from server. That boundary keeps Python
from double-loading this file under both `__main__` and `server` module
names — see the design spec §"Critical: don't trigger a double-import."
"""

if __name__ == "__main__":
    import atexit
    import os
    import signal
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    import uvicorn

    # Load .env from this script's directory (existing env vars take
    # precedence). Done in __main__ so importing server.py for some
    # other reason doesn't tamper with the environment.
    load_dotenv(Path(__file__).parent / ".env")

    from periscope.log import log
    from periscope.pidfile import (
        _reclaim_existing_instance,
        _write_pidfile,
        _remove_pidfile,
    )

    # Reclaim any prior periscope before binding the port. Done here (not
    # in lifespan) because uvicorn binds the socket before lifespan runs —
    # by the time the worker starts up, a port collision has already
    # failed.
    # PERISCOPE_NO_RECLAIM=1 disables the SIGTERM-the-previous-instance
    # step. Use this when intentionally running a second periscope (e.g.
    # to inspect a launchd-managed prod from a debug session) where you
    # don't want the new instance to kill the existing one. See spec
    # §"Opt-out flag for reclaim."
    if os.environ.get("PERISCOPE_NO_RECLAIM") != "1":
        _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
    # SIGTERM otherwise bypasses atexit; install a handler that logs and
    # exits cleanly so atexit fires and the next start is idempotent.
    def _on_sigterm(signum, _frame):
        log.info("received signal %d; exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    # loop="asyncio" forces the stdlib selector loop instead of uvloop.
    # As of uvloop 0.22.1 + CPython 3.14, uvloop captures
    # asyncio.iscoroutinefunction at import time and calls it from
    # run_in_executor, which now emits a DeprecationWarning per call
    # (loud during WS resize traffic). Revert when uvloop ships a 3.14-
    # compatible release.
    #
    # reload=True watches periscope/ + server.py for changes and restarts
    # the worker. Gated on PERISCOPE_DEV=1 because the reload supervisor
    # adds a second process to the tree (worker + supervisor + multi-
    # processing helpers), which makes the server hard to kill cleanly
    # and produces orphans when signals don't propagate. dev.sh sets
    # PERISCOPE_DEV=1; bare `uv run server.py` runs as a single process.
    # reload_dirs is scoped to this file's parent so edits under static/
    # don't bounce the server — Vite handles frontend reloads in dev,
    # and direct browser hits pick up new static files without a restart.
    dev_mode = os.environ.get("PERISCOPE_DEV") == "1"
    from periscope.config import PORT
    uvicorn.run(
        "periscope.app:app",
        host="127.0.0.1",
        port=PORT,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
