# periscope

A live dashboard over your tmux sessions. Every window becomes a card; click
into a card to open a full live terminal (xterm.js + tmux `pipe-pane` over
WebSocket), send keystrokes, focus the window in tmux, or rename it. Parses
Claude Code pane status — branch, PR, CI state, recap, spinner — and surfaces
what's pending input.

Built for the "I have 30 tmux windows across 5 sessions with various Claude
Code agents running, and I want a single pane of glass" workflow.

## Requirements

- `tmux` (any reasonably modern version)
- [`uv`](https://docs.astral.sh/uv/) for running the single-file script
- A modern browser (uses WebSockets + vanilla JS)
- Optional: `ANTHROPIC_API_KEY` for the ✨ auto-rename feature
- Optional: Node 20+ if you want HMR while iterating on the frontend

## Run

```sh
uv run server.py
```

Open <http://127.0.0.1:8765/>. Polls every 3s; the modal opens a live
WebSocket bridge to the selected pane.

For the always-on / launchd-managed setup and the prod/dev port split,
see [CLAUDE.md → Development workflow](./CLAUDE.md#development-workflow-prod--dev-split)
and `bin/periscope`.

### Frontend HMR (optional)

For hot-reload while editing `static/app.js` or `static/styles.css`:

```sh
npm install        # one-time
npm run dev        # then visit http://127.0.0.1:5174/
```

`npm run dev` runs the FastAPI server **and** Vite together via
`concurrently`; ctrl+c stops both. Vite proxies `/api/*` and `/ws/*` to
FastAPI, so only one URL matters in the browser. There's no build step —
production still loads `static/` as-is from FastAPI on :8765.

## Native app (macOS)

Periscope can run as a native `.app` — its own Dock icon and Cmd-Tab
entry instead of a browser tab. The app is a thin [Tauri](https://tauri.app)
shell that loads the dashboard from `http://127.0.0.1:8765`, so the
server still has to be running (`uv run server.py`, or the launchd
service via `bin/periscope install`).

Download the latest `.dmg` from
[Releases](https://github.com/tomplex/periscope/releases), or build it
from source:

```sh
cd src-tauri
cargo tauri build        # needs the Rust toolchain + `cargo tauri`
open target/release/bundle/macos/Periscope.app
```

Release `.dmg`s are **unsigned**, so Gatekeeper blocks them on first
launch. After dragging Periscope.app into /Applications:

```sh
xattr -dr com.apple.quarantine /Applications/Periscope.app
```

## Auto-rename (optional)

The ✨ button on each session header asks Haiku 4.5 to suggest fresh,
descriptive names for every window in the session based on current pane
content. Requires an Anthropic API key:

```sh
cp .env.example .env
# then edit .env and paste your key
```

## Endpoints

- `GET  /api/state` — every tmux window with parsed Claude status
- `GET  /api/pane?session=…&index=…&lines=200` — capture last N lines (with ANSI escapes)
- `POST /api/focus?session=…&index=…` — switch every attached tmux client to that window
- `POST /api/send?session=…&index=…` — body `{keys: [...], paste: "..."}`; sends keystrokes / bracketed paste
- `POST /api/rename?session=…&index=…` — body `{name: "..."}`; renames a window
- `POST /api/auto-rename-session?session=…` — Haiku-driven batch rename of every window in a session
- `WS   /ws/pane?session=…&index=…` — live bidirectional pane stream (xterm.js powers the modal)
- `POST /api/channel/push?pane=%N` — body `{content, meta?}`; queues an event for the pane's channel server
- `POST /api/channel/reply?pane=%N` — body `{message, kind?, severity?}`; called by the channel server's `reply` tool
- `POST /api/channel/clear-unread?pane=%N` — zeroes the pane's unread reply count
- `WS   /ws/channel?pane=%N` — subscriber endpoint for the per-pane channel server (last-writer-wins)

## Channels (Claude push/reply)

Periscope can push messages into the Claude Code sessions it spawns and
surface Claude's replies in its UI. This uses Claude Code's
[channels](https://code.claude.com/docs/en/channels) feature, currently
in research preview.

### One-time setup

Claude Code keeps user-level MCP servers under the `mcpServers` key in
`~/.claude.json`. Merge the periscope entry in (don't overwrite the
file — it holds lots of other Claude Code state):

```sh
jq '.mcpServers.periscope = {
  "command": "uv",
  "args": ["run", "--script", "/ABSOLUTE/PATH/TO/periscope/channel_shim.py"]
}' ~/.claude.json > ~/.claude.json.tmp && mv ~/.claude.json.tmp ~/.claude.json
```

Replace `/ABSOLUTE/PATH/TO/periscope/` with your local checkout path.
After this, restart any running `claude` sessions you want channels in
(it's read at invocation time) or spawn fresh ones via periscope's
`+ claude` button.

### How it works

When you click `+ claude` in periscope, the spawned command is
`claude --dangerously-load-development-channels server:periscope`. Claude
launches `channel_shim.py` as a stdio child. The shim proxies MCP
messages over a unix socket (`/tmp/periscope-mcp.sock`) to periscope's
in-process MCP server, and reconnects transparently across periscope
restarts.

The `--dangerously-load-development-channels` flag is required because
channels are in research preview — bare `--channels` only resolves
allowlisted entries.

### Using channels

- **Push to Claude:** open a pane's modal, type a message in the
  composer in the Messages section, and submit. Claude sees it on its
  next turn as a `<channel source="periscope">` block.
- **Replies from Claude:** Claude can call the `reply` tool with
  `kind="need_human"`, `kind="done"`, or `kind="info"` (the default).
  Messages show in the modal's Messages section; `need_human` triggers a
  pulsing red border on the pane card and fades the rest of the grid.

If a session was started outside periscope (no dev-channels flag), the
push composer is disabled with a tooltip.

## License

MIT — see [LICENSE](./LICENSE).
