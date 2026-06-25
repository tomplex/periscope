"""Cross-cutting paths and constants. Imported widely; should never import
from any other periscope.* module — keep this a leaf."""

import os
import shutil
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

# Prefix of the historical /usage scraper's tmux session. The scraper itself
# is gone (5bb7224 replaced it with the OAuth usage endpoint — no hidden tmux
# sessions), so nothing matches this today, but the single-session migration
# still refuses to move a session named like it: a defensive skip so the
# consolidation never sweeps up a scraper session if one ever returns.
USAGE_SESSION_PREFIX = "periscope-usage-"

# The single tmux session periscope owns once tracks land. Every managed
# pane is a window here; grouping is metadata (pane_tracks), never the session.
MANAGED_SESSION = "periscope"

# Command line periscope sends into tmux when spawning a Claude window.
# The dev-channels flag is what makes Claude connect to periscope's MCP
# socket via channel_shim.py — without it, `link_pr` / `notify` / `+ link
# pull request` etc. all silently no-op for that pane. Every code path that
# spawns Claude must use this constant; new hardcoded `"claude"` strings
# reintroduce the channel-less-spawn bug.
CLAUDE_EXEC = "claude --dangerously-load-development-channels server:periscope"


def claude_exec() -> str:
    return os.environ.get("PERISCOPE_CLAUDE_EXEC", CLAUDE_EXEC)

# Port the FastAPI server binds. Default 8765 = "prod" (launchd-managed).
# Override via PERISCOPE_PORT=8766 for a dev instance running alongside
# prod. Read once at module load — server.py invokes load_dotenv before
# importing anything else, so .env is honored. Modules that need to react
# to test-time monkeypatching access this as `config.PORT`, not via
# `from periscope.config import PORT` (which would snapshot the value).
PORT = int(os.environ.get("PERISCOPE_PORT", "8765"))

# Every dev flow (dev.sh, the worktree workflow) exports PERISCOPE_DEV=1;
# the prod launchd plist sets only PATH/HOME. So DEV is the authoritative
# "is this a developer's instance" signal — more reliable than PORT, which
# dev.sh historically left at the 8765 default and thus spent Haiku.
DEV = bool(os.environ.get("PERISCOPE_DEV"))

def is_prod() -> bool:
    """True only for the real prod instance: on the prod port AND not a dev
    process. Gates everything that costs money or owns a singleton resource
    (Claude-spending activity worker, MCP socket). A dev instance never
    trips this even if it somehow lands on 8765.

    A function, not a constant: reads module globals PORT/DEV at call time so
    tests that monkeypatch `config.PORT` observe the new value — the same
    live-access contract PORT itself documents above."""
    return PORT == 8765 and not DEV


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

# --- bg commander (per-command `claude --bg` dispatch) ---
def claude_bin() -> str:
    """The BARE `claude` executable for argv[0]. NOT claude_exec() — that returns
    a multi-word shell-command string with --dangerously-load-development-channels
    (the dev-channels MCP transport). A --bg commander reaches MCP via --mcp-config
    instead, dispatched through subprocess (no shell), so it needs the binary alone.
    PERISCOPE_CLAUDE_BIN overrides for tests."""
    return os.environ.get("PERISCOPE_CLAUDE_BIN") or shutil.which("claude") or "claude"


PERISCOPE_MCP_CONFIG = config_dir() / "bg-mcp-config.json"        # generated at boot
ORCHESTRATOR_PROMPT_FILE = config_dir() / "orchestrator-prompt.txt"  # written from bg_commander.ROLE_PROMPT
CHANNEL_SHIM_PATH = Path(__file__).resolve().parent.parent / "channel_shim.py"
BG_COMMANDER_MODEL = "sonnet"
# Pinned to pane-INDEPENDENT tools (NOT mcp__periscope__*). The wildcard would
# grant pane-dependent tools (notify/link_pr/open_document/report/…) that resolve
# the caller against a real %N window or feed the handle to tmux; a cmdr: handle
# matches no window, so those error or leak alert state _channel_gc never reaps.
# These are exactly the delegator/discovery set the role prompt advertises.
BG_COMMANDER_ALLOWED_TOOLS = (
    "Read,Grep,Glob,"
    "mcp__periscope__catalog,mcp__periscope__open,"
    "mcp__periscope__create_workspace,mcp__periscope__list_workspaces,"
    "mcp__periscope__spawn_claude,"
    "mcp__periscope__search_history,mcp__periscope__resume_session"
)

# Activity timeline window: git commits + CI runs newer than this many
# days show in the modal's Activity section. Was a hardcoded 24h.
ACTIVITY_DAYS = int(os.environ.get("PERISCOPE_ACTIVITY_DAYS", "7"))
