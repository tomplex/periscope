# work-dashboard

A live tmux dashboard. Shows every window as a card, parses Claude pane status
(branch, PR, CI state, recap, spinner), groups by session, click to view full
pane or focus in tmux.

## Run

```sh
uv run server.py
```

Open http://127.0.0.1:8765/. Polls every 3s.

## Endpoints

- `GET  /api/state` — current state of all tmux windows
- `GET  /api/pane/{session}/{index}?lines=200` — capture last N lines of a pane
- `POST /api/focus/{session}/{index}` — switch every attached tmux client to that window
