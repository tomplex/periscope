"""Entry point for `python -m periscope`. Stage A still routes users
through `uv run server.py`; Stage B's Peel 9 replaces this with
`uvicorn.run(periscope.app:app, ...)`."""

import sys

print(
    "periscope: during the server-split migration, use `uv run server.py` "
    "from the repo root instead of `python -m periscope`.",
    file=sys.stderr,
)
sys.exit(2)
