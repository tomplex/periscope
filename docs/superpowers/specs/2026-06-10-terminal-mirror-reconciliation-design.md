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
   clients. `%output` notifications are line-framed, octal-escaped, and
   flow-controlled (the mechanism iTerm2's tmux integration is built
   on): no attach gap, no FIFO backpressure loss. Decoded bytes relay to
   xterm exactly as today → instant echo, natural scrollback
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
- Spawns/reuses a control client per session on first subscription;
  kills it when the session's last subscription ends.
- Protocol parsing: `%output %<pane> <octal>` → decoded raw bytes →
  subscriber queues. Octal escapes decode at the byte level (UTF-8
  multibyte arrives as escaped bytes and may split across
  notifications — decode to bytes, never to str). `%begin/%end` blocks
  are command replies, matched FIFO-order to pending command futures
  (tmux replies in command order — same guarantee `tmux_input` relies
  on, except now we read the replies instead of discarding them).
- Reconciliation frames are pushed into the same per-pane queue as
  relayed output, so ordering versus the relay is preserved by
  construction (same socket, same queue).

### Reconciliation

Per subscribed pane, the mirror tracks the last `%output` timestamp. A
reconcile fires:

- when output **quiesces** (~150ms with no bytes after activity),
- at a **max interval** (~1s) during sustained streaming so long bursts
  still converge,
- once after every resize,
- never while fully idle.

Frame construction, all through the control client (no forks):
`display-message` for cursor x/y, `alternate_on`, cursor visibility;
`capture-pane -e` for the visible grid with colors. Frame bytes:

- **alt-screen pane:** `\x1b[?1049h` (re-entry clears the alt buffer —
  fine, the frame repaints all of it), then per row
  `\x1b[<r>;1H<content>\x1b[0m\x1b[K`, then park cursor (1-indexed; tmux
  reports 0-indexed) and set cursor visibility.
- **normal-screen pane:** same per-row repaint, **no** clear/`2J`
  (scrollback must not be touched), cursor park.

Each frame is one atomic write/WS message, so xterm parses it as a
single frame: no flicker.

Accepted cosmetic costs:

- A capture taken mid-redraw paints a torn frame for ≤150ms; the
  continuing relay corrects it.
- The frame resets the app's SGR/pending-wrap state to clean; TUIs
  re-assert attributes constantly, so this is invisible in practice.
- If the relay genuinely drops bytes on a normal-screen pane, scrollback
  has a gap (the visible grid still heals). Rare gap in shell scrollback
  beats garbled screen.

### `periscope/routes/ws.py` (rewritten internals)

Same endpoint, same client wire protocol. Connect sequence:

1. resize-to-hint (unchanged `set_pane_size`),
2. resolve pane id, subscribe to the mirror,
3. send `{"type":"size"}`,
4. initial paint — today's `-S -10000` capture-with-scrollback blob, now
   issued through the control client,
5. drain the subscription to the WS.

The attach-vs-capture race disappears twice over: same-socket
serialization orders `%output` against the capture reply, and even a
missed byte heals at the next reconcile.

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
  reconnect FSM).
- **Control-client spawn failure:** the WS fails. No fork-path fallback
  and no `_disabled` latch — byte relay without reconciliation *is* the
  bug this design removes, and a tmux that can't `-C attach` already
  broke `tmux_input`.

## Verify at implementation start

- `attach -f ignore-size,read-only` on this tmux version (keeps the
  mirror client from influencing window sizing — viewed panes are
  already `window-size manual`; this covers the session's other
  windows). Fallback: `refresh-client -C <big>x<big>` after attach.
- Exact format variable for cursor visibility (`#{cursor_flag}` or
  equivalent).

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
3. **Manual:** dev periscope on 8766, open a pane running an
   AskUserQuestion-heavy Claude session, watch for ghost rows.

## Out of scope

- Replacing the 3s `/api/state` poll with event-driven updates (the
  control-mode infrastructure built here makes that a clean follow-up).
- Scrollback dedup for true upstream Ink content dupes (only relevant to
  non-alt panes now; Tom is waiting on the upstream fix).
- Any client-side changes.
