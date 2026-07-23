# Spec — forkless pane open

## Problem

Opening a terminal forks tmux twice on the hot path (~20 ms each; pane-switch
is the most frequent interaction in a terminal-default workflow):

1. `setw ; resize-window ; display-message` — geometry + size + session/window
   name + mouse flag (`routes/ws.py`, one fork after the resize/read merge).
2. `capture-pane -e -S -10000` — scrollback for the initial paint (`ws.py`).

Fork #1 is **redundant with the mirror**: `_SessionMirror.subscribe` already
fires a connect-time reconcile (`tmux_mirror.py:304`) that runs `capture-pane` +
`display-message` and reads the same `cursor_x/cursor_y/alternate_on` fork #1
just read. The handshake and the mirror read the pane's geometry twice, back to
back, on every open.

## Goal

Eliminate fork #1. Source everything it provides without forking:

| Field | New source |
|---|---|
| `cursor_x/y`, `alternate_on`, `pane_height` | the mirror's connect-time reconcile — it already reads these |
| `session_name`, `window_index` | periscope's poll-state pane→session cache (already maintained for the rail) |
| `pane_width` / `pane_height` (size) | a cached fit-size, invalidated by the Tauri `onResized` hook (viewport is otherwise constant in the fullscreen Tauri app) |
| `mouse_any_flag` | fold into the mirror's `DISPLAY_FMT` (currently omits it) |

Fork #2 (scrollback) **stays** — the mirror only captures the visible screen.
So a deduped open is **1 fork, not 2**. Going to zero would mean dropping
scrollback-on-open (a live-terminal-only view), which is a product regression,
not part of this spec.

## Shape

- Extend `DISPLAY_FMT` (`tmux_mirror.py`) with `pane_width` + `mouse_any_flag`,
  and have `subscribe` return the first reconcile's `GridSnapshot` (or expose it
  via the `Subscription`) so the WS handshake can consume it instead of forking.
- The WS handler resolves `session_name` from a cache before subscribing (it
  already needs it to pick which session's mirror to attach). Cache miss on a
  brand-new pane → fall back to today's single `display-message` fork. The fork
  path stays as the correctness floor; the dedup is the fast path.
- Client caches its fit-size; a Tauri `onResized` listener (reachable via the
  `__TAURI_INTERNALS__` wiring in `tauri.js`) invalidates it. Non-Tauri browsers
  keep the existing `FitAddon` measurement — the cache is an optimization, not a
  requirement.

## Risks

- **Reply-ordering (invariant #3).** The mirror correlates replies by command
  order; the connect reconcile's snapshot must be delivered to the handshake
  without perturbing that queue. Reuse the existing reconcile, don't inject a new
  command. Covered by `tests/test_tmux_mirror.py`'s convergence oracle — extend it.
- **Stale session cache.** A pane whose session periscope hasn't polled yet must
  fall back to the fork, never render against a wrong session. Mirror the
  narrator's no-cwd-fallback stance (`narrator.py:345`).

## Not doing

- Removing the resize as a correctness measure. It's a no-op when the size is
  unchanged and free-rides fork #1 today; once fork #1 is gone, the size comes
  from the cache and the resize only issues on an actual Tauri resize event.
