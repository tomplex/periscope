#!/bin/sh
# Run server.py + vite as one foreground command. `trap 'kill 0' EXIT INT TERM`
# kills the entire process group when this script exits for any reason
# (including ctrl+c), so we never leave orphaned uvicorn reload workers behind
# regardless of how uv / python / uvicorn forward signals between themselves.
# This replaces the npm `concurrently` wrapper, which relied on each layer
# propagating SIGTERM correctly and occasionally didn't.

trap 'kill 0' EXIT INT TERM

# PERISCOPE_DEV=1 enables uvicorn's reload supervisor so backend edits
# bounce the worker. Production (`uv run server.py`) runs as a single
# process so it's easy to kill and leaves no orphans.
PERISCOPE_DEV=1 uv run server.py &
vite &
# Rebuild the committed static/dist/ bundle on every static/src/ change.
# --emptyOutDir false mirrors vite.config.js: never wipe the committed dist.
vite build --watch --emptyOutDir false &
wait
