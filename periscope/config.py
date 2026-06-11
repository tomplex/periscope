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

# Hidden tmux session the control-mode input client (periscope/tmux_input.py)
# attaches to. It lives apart from user sessions so the control client's
# size never reflows a user pane, and so it survives any user session being
# killed. panes.list_windows filters it out.
INPUT_CTL_SESSION = "periscope-input"

# Command line periscope sends into tmux when spawning a Claude window.
# The dev-channels flag is what makes Claude connect to periscope's MCP
# socket via channel_shim.py — without it, `link_pr` / `notify` / `+ link
# pull request` etc. all silently no-op for that pane. Every code path that
# spawns Claude must use this constant; new hardcoded `"claude"` strings
# reintroduce the channel-less-spawn bug.
CLAUDE_EXEC = "claude --dangerously-load-development-channels server:periscope"

# Port the FastAPI server binds. Default 8765 = "prod" (launchd-managed).
# Override via PERISCOPE_PORT=8766 for a dev instance running alongside
# prod. Read once at module load — server.py invokes load_dotenv before
# importing anything else, so .env is honored. Modules that need to react
# to test-time monkeypatching access this as `config.PORT`, not via
# `from periscope.config import PORT` (which would snapshot the value).
PORT = int(os.environ.get("PERISCOPE_PORT", "8765"))

def config_dir() -> Path:
    """The periscope config directory ($XDG_CONFIG_HOME/periscope, default
    ~/.config/periscope). Computed per call — not a module constant — so tests
    can redirect all periscope state by monkeypatching XDG_CONFIG_HOME at
    runtime (see tests/conftest.py). Callers that need a frozen value (e.g.
    ACTIVITY_DB) evaluate it once at import."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope"


# Activity store (periscope/activity.py). Named generically — periscope.db,
# not activity.db — because it is the destination for other persistent
# state (prefs, projects) that may migrate out of state.json later; the
# generic name avoids a future rename. Path mirrors store.py:_state_path.
ACTIVITY_DB = config_dir() / "periscope.db"

# Activity timeline window: git commits + CI runs newer than this many
# days show in the modal's Activity section. Was a hardcoded 24h.
ACTIVITY_DAYS = int(os.environ.get("PERISCOPE_ACTIVITY_DAYS", "7"))
