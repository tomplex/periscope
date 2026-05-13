# periscope — notes for Claude

## What this is

A single-file FastAPI server (`server.py`) plus a vanilla-JS frontend
(`static/`) that gives a browser dashboard over the host's tmux sessions.
No build step, no framework, no package manifest — `uv run server.py` reads
its dependencies from the PEP-723 inline metadata at the top of the file.

## Running

```sh
uv run server.py     # http://127.0.0.1:8765/
```

That's it. There's no test suite; iterate against the live dashboard.

## Architecture

```
browser (static/app.js, vanilla JS, polls /api/state every 3s)
   │
   │   ── modal open ──>  WS /ws/pane  ──>  xterm.js terminal
   │                                            ▲
   v                                            │
FastAPI (server.py)                       tmux pipe-pane FIFO
   │
   └─> tmux CLI (subprocess)
```

- **`server.py`** holds everything server-side: pane parsing, the focus
  bookkeeping, all `/api/*` routes, and the `/ws/pane` WebSocket bridge.
- **`static/app.js`** is the entire frontend — grid rendering, filters,
  drag-to-reorder sessions, modal, xterm.js wiring. No bundler; the file is
  served as-is.
- **`static/vendor/xterm.{js,css}`** is vendored upstream xterm.js. Don't
  edit; replace wholesale if upgrading.

## Key invariants (the things that broke and we fixed)

These are the non-obvious behaviors worth preserving:

1. **`focused_at` is server-tracked, not tmux's `window_activity`.**
   tmux's activity stamp bumps on any pane output (streaming logs, dev
   servers, Claude tokens). We instead record when a window becomes the
   active window in its session, or when the user acts on it via the
   dashboard (focus/send). See `update_focus_from_windows`.

2. **Claude detection requires status line in the last 4 non-empty lines.**
   Old status lines in scrollback should not trigger `is_claude=true` after
   the user has returned to a shell. See `parse_pane`.

3. **WebSocket initial paint mirrors tmux's screen state.** Width, height,
   cursor position, and alt-screen mode all come from `display-message`
   before the capture-pane body is sent. The prefix enters alt-screen if
   needed, clears the buffer, and the suffix parks the cursor where tmux
   thinks it is — without all three, incremental updates from `pipe-pane`
   land at the wrong cursor and leave ghost text.

4. **`capture-pane` separates rows with bare `\n`; xterm needs `\r\n`.**
   Forgetting the carriage return staircases every line right by the
   previous line's length.

5. **Multi-line input goes via tmux paste-buffer, then Enter via send-keys.**
   `send-keys` silently strips embedded newlines. There's a 100ms sleep
   between paste and Enter so TUIs (especially Claude Code) apply paste
   state before submit lands. See `/api/send`.

6. **Session/index are query params, not path segments.** Session names
   like `tc/foo/bar` contain slashes; path routing decoded `%2F` and 404'd.

7. **Spinner has hysteresis at the data layer.** `capture-pane` runs
   mid-redraw drop the spinner line; without smoothing, the "thinking"
   indicator flickers. Done in `app.js`, not the server.

## Status-line parsing

Claude Code renders a two-line block at the very bottom of its pane:

```
  fdy | master | clean | github.com/fdy/repo/pull/1234 ✓
  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)
```

`STATUS_RE` matches the bottom line (context %, model). `TITLE_RE` matches
the line above (project, branch, git state, PR URL). `PR_RE` pulls the PR
number and CI glyph (⟳ ✓ ✗) out of the URL field. If Claude changes its
status format, these regexes break and `is_claude` returns false for every
window — fix the regexes first when triaging "everything looks like a
shell."

## Conventions

- Single-file server; resist the urge to split it until it actually hurts.
- No frameworks on the frontend; vanilla JS is part of the value prop.
- Comments explain *why*, not what. The existing comments around
  pipe-pane, the cursor sync, and the bracketed-paste delay are the
  template — terse, points at the failure that motivated the code.
- `uv run server.py` must keep working — keep dependencies declared in the
  PEP-723 header at the top of `server.py`.
- The `.env` file is for local Anthropic API key only; never commit.
