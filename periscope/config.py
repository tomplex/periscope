"""Cross-cutting paths and constants. Imported widely; should never import
from any other periscope.* module — keep this a leaf."""

import os
from pathlib import Path

# Static asset root for FastAPI's app.mount("/", StaticFiles(...)) call.
# Computed relative to the repo root, NOT to this file — server.py lives at
# the repo root, periscope/ is a subdirectory.
STATIC = Path(__file__).parent.parent / "static"

# Unix socket the in-process MCP server listens on. channel_shim.py connects
# here from each Claude pane. Lifespan unlinks this on shutdown; channels.py
# must never unlink it (see spec §"MCP_SOCKET_PATH cleanup").
MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"

# Tmux session prefix for periscope-spawned `claude /usage` scrape sessions.
# panes.list_windows filters these out; usage.py creates them.
USAGE_SESSION_PREFIX = "periscope-usage-"

# Port the FastAPI server binds. Default 8765 = "prod" (launchd-managed).
# Override via PERISCOPE_PORT=8766 for a dev instance running alongside
# prod. Read once at module load — server.py invokes load_dotenv before
# importing anything else, so .env is honored. Modules that need to react
# to test-time monkeypatching access this as `config.PORT`, not via
# `from periscope.config import PORT` (which would snapshot the value).
PORT = int(os.environ.get("PERISCOPE_PORT", "8765"))
