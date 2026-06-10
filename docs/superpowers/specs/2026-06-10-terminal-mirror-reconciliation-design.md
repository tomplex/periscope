# Terminal mirror: lossless transport + self-healing reconciliation

**Date:** 2026-06-10
**Status:** Approved (design review with Tom)

## Problem

The `/ws/pane` terminal mirror garbles regularly. The architecture is
open-loop: raw pane bytes replay into xterm.js and nothing ever verifies
that xterm's grid matches tmux's. One lost or misapplied escape sequence
produces permanent ghost rows — there is no self-healing mechanism.

Observed failure classes (June 3 investigation + June 10 live specimen):

1. **Ghost text / wrong cursor on open** — the connect sequence runs
   `capture-pane` *before* `pipe-pane`, so bytes emitted in the gap are
   lost.
2. **Garbled live streaming mid-session** — TUI-heavy redraws (e.g.
   Claude's AskUserQuestion, ~30–50KB/frame at 159x55) overwhelm the
   pipe-pane→FIFO→WS path; a dropped erase sequence leaves stale rows
   overlapping the new frame.
3. **"Duplicated blocks"** — in alt-screen mode (Claude Code's current
   TUI), stale ghost rows from class 1/2 *look like* duplicated content.
   (True content dupes from the upstream Ink redraw regression are a
   separate, upstream problem — out of scope; no mirror fix can remove
   bytes Claude actually emitted.)
4. **Resize reflow artifacts** — self-inflicted: occasional dev-browser
   mounts re-fit at a different width and re-pin. Mitigations exist
   (width pin, hold-pane-size); residual artifacts currently persist
   forever because nothing re-converges the mirror.

Key facts constraining the design:

- tmux already maintains the authoritative grid — adding a second
  server-side emulator (pyte) would just be a third emulation layer with
  its own divergence modes.
- Claude panes run in alt-screen (`alternate_on=1`), so for the primary
  use case there is no scrollback dimension: the whole game is the live
  grid.
- Verified empirically: a control-mode client receives `%output` only
  for panes of its **attached session** — mirroring requires one control
  client per session being viewed.
- `tmux_input.py` already proves the persistent control-mode-client
  pattern in this codebase (input direction only).
- `pipe-pane` is one-per-pane: a second viewer of the same pane silently
  steals the first's stream (latent bug, fixed as a side effect here).

## Design

Two layers, each doing what it's good at:

1. **Transport** — replace pipe-pane/FIFO with per-session control-mode
   clients. `%output` notifications are line-framed and octal-escaped
   (the mechanism iTerm2's tmux integration is built on), delivered over
   a socket whose backpressure lands in tmux's buffer rather than
   dropping bytes: no attach gap, no FIFO loss. (True `%pause` flow
   control requires the `pause-after` client flag, which we don't set —
   plain socket backpressure is the actual guarantee.) Decoded bytes
   relay to xterm exactly as today → instant echo, natural scrollback
   accumulation, client untouched.
2. **Convergence** — the mirror periodically ships an unconditional
   **reconciliation repaint** built from tmux's own grid
   (`capture-pane`). Blindly idempotent: no diffing, no shadow grid, no
   assumption about client state. Any desync from any cause heals at the
   next frame.

```
browser (xterm.js — unchanged)
   ▲ bytes + {"type":"size"} JSON           ▼ keystrokes (unchanged → tmux_input)
   │
ws.py  /ws/pane  (rewritten internals, same endpoint & wire protocol)
   ▲ per-pane byte queue
   │
tmux_mirror.py  (new — sibling of tmux_input.py)
   ├─ one `tmux -C attach -t <session> -f ignore-size,read-only`
   │    per session with ≥1 active viewer (in practice: 1, occasionally 2)
   ├─ %output parser: octal-decode → route bytes to per-pane subscribers
   ├─ command channel: capture-pane / display-message through the same
   │    client stdin; replies correlated by in-order %begin/%end framing
   └─ reconciler: per-pane quiesce timer → authoritative repaint frames
```

### `periscope/tmux_mirror.py` (new)

One responsibility: "give me a correct byte stream for pane X."

- API: `subscribe(pane_id) → async-iterable of bytes` + unsubscribe
  (context-manager or explicit). Multiple subscribers to one pane
  multiplex off the same client.
- Spawns/reuses a control client per session on first subscription.
  When the session's last subscription ends, the client lingers ~20s
  before being killed (timer cancelled on resubscribe) — rail
  navigation unmounts the old terminal before mounting the new one, so
  without a linger every pane switch within a session would thrash
  attach/detach.
- Protocol parsing: `%output %<pane> <octal>` → decoded raw bytes →
  subscriber queues. Route by pane-id prefix *before* octal-decoding so
  unviewed panes (a log-spewing dev server in the same session) cost
  one string comparison, not a decode. Octal escapes decode at the byte
  level (UTF-8 multibyte arrives as escaped bytes and may split across
  notifications — decode to bytes, never to str). `%begin/%end` blocks
  are command replies, matched FIFO-order in command order; match the
  `<time> <number>` tokens echoed from `%begin`, not a bare `%end`
  prefix — a capture-pane *body* line can legitimately begin with
  `%end`.
- **Ordering rule (load-bearing):** tmux guarantees notifications never
  occur inside a reply block and replies come in command order — but
  that wire ordering must not be laundered through a future.
  `set_result` at `%end` only *schedules* the awaiting task; the reader
  keeps parsing, and `%output` arriving after the reply could reach the
  subscriber queue before the woken task enqueues its frame, making the
  frame revert newer output. Reconcile frames are therefore built and
  pushed **synchronously inside the reader task's processing of the
  final `%end` line** (a registered reply callback), never in a task
  woken by a future. Futures are fine for callers that only need reply
  *data* and do their own sequencing.

### Reconciliation

Per subscribed pane, the mirror tracks the last `%output` timestamp. A
reconcile fires:

- when output **quiesces** (~150ms with no bytes after activity),
- at a **max interval** (~1s) during sustained streaming so long bursts
  still converge,
- once after every resize and on `%layout-change` (arrives on the same
  client for free; catches tmux-side grid changes that produce no pty
  output, e.g. `clear-history` or an external resize),
- once at connect (heals the initial-blob gap, see ws.py below),
- never while fully idle.

Frame construction, all through the control client (no forks):
`capture-pane -e` for the visible grid with colors, then
`display-message` for cursor x/y, `alternate_on`, cursor visibility —
capture first so the cursor is the fresher of the two samples.

**Coverage requirement:** the frame iterates rows 1..pane_height —
padding with empty rows if capture ever returns short — each written as
`\x1b[<r>;1H<content>\x1b[0m\x1b[K`. The row loop is the mechanism that
guarantees every cell of the grid is overwritten; nothing else clears.
(Verified: the vendored xterm.js gates `activateAltBuffer` /
`activateNormalBuffer` on a buffer *change* — DECSET 1049 re-entry is a
no-op and clears nothing, so the frame cannot rely on it.)

Frame bytes:

- **alt-screen pane:** `\x1b[?1049h` (no-op if already in alt; switches
  if the client missed the app's transition), then the row loop, then
  park cursor (1-indexed; tmux reports 0-indexed) and set cursor
  visibility.
- **normal-screen pane:** `\x1b[?1049l` first (no-op if already normal;
  heals the stuck-in-alt class, where a missed `1049l` would otherwise
  break scrollback accumulation *forever*), then the row loop, **no**
  clear/`2J` (scrollback must not be touched), cursor park.

Each frame is one atomic write/WS message, so xterm parses it as a
single frame: no flicker.

Accepted cosmetic costs:

- A capture taken mid-redraw paints a torn frame for ≤150ms; the
  continuing relay corrects it.
- The frame resets the app's SGR/pending-wrap state to clean; TUIs
  re-assert attributes constantly, so this is invisible in practice.
- `1049h` re-entry clobbers xterm's saved-cursor slot; the app's next
  real `1049l` restores a reconcile-time cursor. Heals next cycle.
- If the relay genuinely drops bytes on a normal-screen pane, scrollback
  has a gap (the visible grid still heals). Rare gap in shell scrollback
  beats garbled screen.

### `periscope/routes/ws.py` (rewritten internals)

Same endpoint, same client wire protocol. Connect sequence:

1. resize-to-hint (unchanged `set_pane_size`),
2. resolve pane id, subscribe to the mirror,
3. send `{"type":"size"}`,
4. initial paint — today's `-S -10000` capture-with-scrollback blob,
   still issued via the **fork path** (run_in_executor, as today),
5. drain the subscription to the WS; the mirror's connect-time reconcile
   follows within ~150ms.

The initial blob deliberately does *not* go through the control client:
with a 50k history-limit, a `-e` capture body can run multi-MB, and
tmux holds **all** `%output` for **every** pane in the session until a
reply block completes — head-of-line blocking the whole mirror on every
connect (and reconnects fire constantly during dev reloads). The old
capture-vs-pipe gap this reintroduces is now harmless: the connect-time
reconcile (visible grid only, ≤ pane_height rows, cheap) heals the
screen moments later. Scrollback may miss the few bytes from the
connect window — the same loss the old design had, minus the permanent
garbling.

Reply bodies on the control client are therefore always small (visible
grid), but the reader still sets an explicit asyncio StreamReader limit
well above 64KB — a heavily-SGR'd wide row or large `%output` burst
line can exceed the default.

Deleted: mkfifo, FIFO reader, `pipe-pane` start/stop, FIFO cleanup.
Input path (keystroke queue → `tmux_input`) and resize handling are
unchanged, except a resize now also triggers a reconcile.

CLAUDE.md's pipe-pane invariants (#3/#4 in "Key invariants" and the
ws.py description) are rewritten to describe the mirror, as part of
implementation.

### `static/src/terminal/terminalCore.js`

Untouched. Reconcile frames are ordinary ANSI bytes; the closed loop is
invisible to the client. Deliberate: the client keeps xterm's native
scrollback, selection, search, link handling, and the reconnect FSM.

## Lifecycle & error handling

- **Mirror client death** (tmux restart, `%exit`, session killed):
  subscribers get EOF → ws closes → client's existing reconnect FSM
  retries; a fresh control client spawns on the next subscribe.
- **Pane killed while viewed:** capture fails → close the WS (today the
  FIFO just goes silent; a close is strictly more honest and feeds the
  reconnect FSM). Note the client FSM never gives up — steady 4s
  retries against a dead pane until the user navigates away. Bounded
  and invisible; accepted.
- **Control-client spawn failure:** the WS fails. No fork-path fallback
  and no `_disabled` latch. (`tmux_input` *does* degrade to forks on
  spawn failure — input has a working degraded mode. Mirroring
  deliberately does not: byte relay without reconciliation is exactly
  the bug this design removes, so a fallback would silently reintroduce
  it. This makes the mirror the first hard dependency on `-C attach`,
  which is fine on tmux 3.6a for a single-user tool.)
- **Lifespan shutdown:** `tmux_mirror.shutdown()` is registered in the
  lifespan next to `tmux_input.shutdown()` (app.py) — without it,
  control clients leak across dev reloads.

## Verify at implementation start

- Exact format variable for cursor visibility (`#{cursor_flag}` or
  equivalent).
- (`attach -f ignore-size,read-only` is confirmed available on the
  installed tmux 3.6a — spec review verified against the man page;
  `ignore-size` keeps the mirror client from influencing window sizing
  for the session's non-viewed windows, viewed panes being
  `window-size manual` already.)

## Testing (`tests/test_tmux_mirror.py`)

1. **Unit, no tmux:** `%output` parsing + octal decode (multibyte UTF-8
   split across notifications, `\\` escapes), `%begin/%end` reply
   correlation, quiesce/max-interval timing with a fake clock,
   frame-builder snapshot tests (grid+cursor+alt in → exact bytes out).
2. **Integration, real tmux (the closed-loop oracle):** spawn
   `tmux -L periscope-test`, run a script emitting TUI-ish output
   (bursts, alt-screen toggles, redraws), feed the subscriber stream
   into **pyte** (test-only dependency — the emulator deliberately kept
   out of production becomes the verifier), assert pyte's final grid ==
   `capture-pane`'s grid. Then the thesis-as-assertion test: inject
   random byte drops into the relay path and assert the grid still
   converges after reconciliation.
3. **Existing tests:** `tests/routes/test_ws.py` mocks
   mkfifo/pipe-pane/add_reader throughout and is rewritten against the
   mirror API; the resize-before-initial-paint ordering assertion in it
   protects behavior this design keeps and must be re-expressed, not
   dropped.
4. **Manual:** dev periscope on 8766, open a pane running an
   AskUserQuestion-heavy Claude session, watch for ghost rows.

## Out of scope

- Replacing the 3s `/api/state` poll with event-driven updates (the
  control-mode infrastructure built here makes that a clean follow-up).
- Scrollback dedup for true upstream Ink content dupes (only relevant to
  non-alt panes now; Tom is waiting on the upstream fix).
- Any client-side changes.
