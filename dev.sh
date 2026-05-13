#!/bin/sh
# Run server.py + vite as one foreground command. `trap 'kill 0' EXIT INT TERM`
# kills the entire process group when this script exits for any reason
# (including ctrl+c), so we never leave orphaned uvicorn reload workers behind
# regardless of how uv / python / uvicorn forward signals between themselves.
# This replaces the npm `concurrently` wrapper, which relied on each layer
# propagating SIGTERM correctly and occasionally didn't.

trap 'kill 0' EXIT INT TERM

uv run server.py &
vite &
wait
