# periscope

A live dashboard over your tmux sessions. Every window becomes a card; click
into a card to see the full pane (with ANSI colors), send keystrokes, focus
the window in tmux, or rename it. Parses Claude pane status — branch, PR, CI
state, recap, spinner — and surfaces what's pending input.

## Run

```sh
uv run server.py
```

Open http://127.0.0.1:8765/. Polls every 3s.

## Auto-rename (optional)

The ✨ rename button on each session header asks Haiku 4.5 to suggest fresh,
descriptive names for every window in the session based on current pane
content. Requires an Anthropic API key:

```sh
cp .env.example .env
# then edit .env and paste your key
```

## Endpoints

- `GET  /api/state` — every tmux window with parsed Claude status
- `GET  /api/pane?session=...&index=...&lines=200` — capture last N lines (with ANSI escapes)
- `POST /api/focus?session=...&index=...` — switch every attached tmux client to that window
- `POST /api/send?session=...&index=...` — body `{keys: [...], paste: "..."}`; sends keystrokes / bracketed paste
- `POST /api/rename?session=...&index=...` — body `{name: "..."}`; renames a window
- `POST /api/auto-rename-session?session=...` — Haiku-driven batch rename of every window in a session
