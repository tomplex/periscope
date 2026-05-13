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

## License

MIT — see [LICENSE](./LICENSE).
