# Structure proposal: terminal mirror reconciliation

**Spec:** `docs/superpowers/specs/2026-06-10-terminal-mirror-reconciliation-design.md`
**Date:** 2026-06-10

## Assumptions

- **Pane-id resolution lives in ws.py**, by appending `#{pane_id}` to the
  existing connect-time `display-message` format string (the one that already
  fetches size/cursor/alt). No new tmux round-trip.
- **pyte** goes in the test dependency group in `pyproject.toml` (test-only, per
  spec; never imported from `periscope/`).
- The control client subprocess is created with an explicit asyncio stream
  `limit=1 << 20` (1 MiB) — "well above 64KB" per spec; exact number is not
  load-bearing.
- Timing constants are module constants in `tmux_mirror.py`:
  `QUIESCE_S = 0.15`, `MAX_INTERVAL_S = 1.0`, `LINGER_S = 20.0`.
- `%error` reply blocks (pane killed mid-capture) terminate the pane's
  subscriptions with EOF, per the spec's "capture fails → close the WS".

## File layout

```
periscope/
  tmux_mirror.py        NEW  — control-mode mirror: parser, frame builder,
                              per-session clients, reconcile timing, module API
  routes/ws.py          MOD  — pipe-pane/FIFO plumbing deleted; consumes
                              tmux_mirror.subscribe(); resize triggers reconcile
  app.py                MOD  — lifespan registers tmux_mirror.shutdown() next to
                              tmux_input.shutdown() (line ~91)
tests/
  test_tmux_mirror.py   NEW  — unit (parser/decoder/frames/timer) + real-tmux
                              integration with pyte oracle
  routes/test_ws.py     MOD  — rewritten against a fake Subscription; keeps the
                              resize-before-initial-paint ordering assertion
pyproject.toml          MOD  — pyte in the test dependency group
CLAUDE.md               MOD  — invariants #3/#4 + ws.py table row rewritten
```

One new module, matching the spec's "sibling of `tmux_input.py`" and the
codebase's one-file-per-subsystem convention. Estimated ~450 LOC — above the
typical 400 ceiling but it is genuinely one concern (the mirror); the internal
units below keep it navigable. Split flagged in Decisions.

## Per-module structure

### `periscope/tmux_mirror.py`

Four internal units, ordered lowest-rung-first in the file. `tmux_input.py`'s
module-global style does **not** transfer here: tmux_input has exactly one
client and zero per-consumer state; the mirror has N session clients each
owning a subprocess, a reader task, a FIFO reply queue, per-pane subscriber
sets, and per-pane timers. That is coupled mutable state + behavior — rung 3.
The module-global pattern is kept only for the registry.

**1. Protocol parsing — frozen events + a small stateful parser.**

```python
@dataclass(frozen=True)
class Output:        pane_id: str; raw: bytes   # still octal-escaped
@dataclass(frozen=True)
class Reply:         body: tuple[bytes, ...]
@dataclass(frozen=True)
class ReplyError:    body: tuple[bytes, ...]
@dataclass(frozen=True)
class LayoutChange:  window_id: str
@dataclass(frozen=True)
class Exit:          pass

class ControlParser:
    def feed_line(self, line: bytes) -> Output | Reply | ReplyError | LayoutChange | Exit | None: ...

def decode_octal(raw: bytes) -> bytes: ...
```

- `ControlParser` is a concrete class (rung 3) because reply-block accumulation
  is inherently stateful: it stashes the `<time> <number>` tokens from `%begin`
  and matches the *full-token* `%end`/`%error` line (a capture body line can
  legitimately start with `%end` — spec). Outside a reply block, lines parse
  statelessly into events. One method, byte-in/event-out: heavy unit testing
  with zero tmux.
- `Output` carries the **raw escaped** payload — routing by pane-id happens
  before decoding (spec's cheap-skip for unviewed panes), so decode is the
  caller's choice. `decode_octal` is a pure function, bytes→bytes only (UTF-8
  multibyte may split across notifications; never decode to str).
- Unknown `%notification` lines parse to `None` (ignored) — forward-compatible
  with tmux adding notifications.

**2. Frame building — frozen data + pure functions (rung 2).**

```python
@dataclass(frozen=True, kw_only=True)
class GridSnapshot:
    rows: tuple[bytes, ...]   # capture-pane -e body lines, raw bytes
    width: int
    height: int
    cursor_x: int             # 0-indexed, as tmux reports
    cursor_y: int
    alt_on: bool
    cursor_visible: bool

def build_reconcile_frame(snap: GridSnapshot) -> bytes: ...
def snapshot_from_replies(capture: Reply, display: Reply) -> GridSnapshot: ...
```

Pure transforms over immutable input → exact-bytes snapshot tests. All the
load-bearing spec rules live here and nowhere else: the 1..height row loop with
`\x1b[<r>;1H…\x1b[0m\x1b[K`, padding short captures, `?1049h` vs `?1049l`
leading byte, no `2J` on normal-screen, 0→1-index cursor park, cursor
visibility. One frame = one `bytes` return = one atomic WS message.

**3. Reconcile timing — one small concrete class (rung 3).**

```python
class ReconcileTimer:
    def __init__(self, fire: Callable[[], None], *, now: Callable[[], float] | None = None) -> None: ...
    def note_output(self) -> None      # (re)arm quiesce; arm max-interval if streaming
    def note_reconciled(self) -> None
    def request(self) -> None          # resize / connect / %layout-change: fire soon
    def cancel(self) -> None
```

Mutable timer state (last-output ts, last-reconcile ts, armed `call_later`
handle) coupled to the behavior that interprets it. Separating it from
`_SessionMirror` exists for exactly one reason: the spec's "quiesce/max-interval
timing with a fake clock" unit tests — `now` is injected keyword-only,
`call_later` goes through the running loop and is monkeypatchable. `fire` is a
sync callback (it only *initiates* the capture commands; see ordering below).
Inlining this into per-pane dicts in the mirror would force those tests through
a live control client — the structural smell the test-strategy rules call out.

**4. Session client + module API — concrete class behind a functional registry.**

```python
class Subscription:
    # async context manager + async iterator of decoded bytes; queue-backed,
    # None sentinel = EOF (pane died, client died, shutdown)
    def request_reconcile(self) -> None: ...   # ws.py calls this on resize

class _SessionMirror:
    def __init__(self, session: str) -> None: ...
    async def start(self) -> None              # tmux -C attach -t <session> -f ignore-size,read-only
    def subscribe(self, pane_id: str) -> Subscription
    async def _read_loop(self) -> None         # readline → ControlParser → dispatch
    def _send_command(self, cmd: str, on_reply: Callable[[Reply | ReplyError], None]) -> None
    def _fire_reconcile(self, pane_id: str) -> None

# module API (mirrors tmux_input's surface style)
async def subscribe(session: str, pane_id: str) -> Subscription: ...
async def shutdown() -> None: ...
_MIRRORS: dict[str, _SessionMirror] = {}
```

- `_SessionMirror` is the rung-3 case in textbook form: subprocess lifecycle +
  reader task + reply queue + subscriber routing acting on shared state.
- **Reply correlation is callback-based, not future-based** — this is the
  spec's load-bearing ordering rule encoded structurally. `_send_command`
  appends `on_reply` to a FIFO `deque`; the reader pops and invokes it
  *synchronously* when `feed_line` returns `Reply`. `_fire_reconcile` writes
  `capture-pane -e -t %N` then `display-message -t %N -p '…'` and registers two
  callbacks; the second builds the frame (`snapshot_from_replies` +
  `build_reconcile_frame`) and pushes to subscriber queues **inside the reader
  task**, so no `%output` can leapfrog it. A thin
  `async def _command(cmd) -> Reply` wraps a future over the same mechanism for
  data-only callers (none needed yet; kept trivial).
- `%output` dispatch: pane-id string compare against subscribed panes first,
  `decode_octal` only on a hit, fan out to every subscriber queue, then
  `timer.note_output()`.
- Linger: when a pane's last subscriber leaves and no other pane in the session
  is subscribed, `call_later(LINGER_S, kill)`; cancelled on resubscribe.
  Rail-navigation thrash protection per spec.
- Death paths: `%exit`/EOF/reader exception → sentinel to every queue, pop self
  from `_MIRRORS`. `%error` on a reconcile capture → sentinel to that pane's
  queues. No fallback, no `_disabled` latch — spec is explicit that degrading
  to relay-without-reconcile reintroduces the bug.
- Registry is module functions + a dict (`subscribe` is the `getOrCreateX`),
  not a manager class — taste rule, and consistent with how `tmux_input`
  presents its surface.

### `periscope/routes/ws.py` (rewritten internals)

Stays a single route function — orchestration, rung 1. Same endpoint, same
wire protocol. Deleted: mkfifo, FIFO fd, `add_reader`, `pipe-pane` start/stop,
FIFO cleanup (the whole bottom `finally` block shrinks to nothing mirror-
related — `Subscription.__aexit__` is the cleanup). Kept verbatim: `note_action`,
`set_pane_size` + the hold-pane-size comment, keystroke queue → `tmux_input`,
resize JSON handling, initial `-S -10000` blob via the fork path.

```
1. set_pane_size(cols, rows)                          (unchanged, executor)
2. display-message → size/cursor/alt + #{pane_id}     (fork path, one call)
3. sub = await tmux_mirror.subscribe(session_name, pane_id)   # async with
4. send {"type":"size"}; send initial blob            (fork-path capture, executor)
5. forward task: async for chunk in sub → send_bytes; on EOF close the WS
6. recv loop: keystrokes → queue; resize → set_pane_size + sub.request_reconcile()
```

Two deliberate deltas from today: the forward task **closes the websocket** when
the subscription EOFs (spec: "a close is strictly more honest", feeds the client
reconnect FSM), and the initial `capture-pane` moves into `run_in_executor`
(today it blocks the loop inline; spec describes it as executor — cheap to fix
in passing). The connect-time reconcile is the mirror's job (`Subscription`
creation calls `timer.request()`), not ws.py's.

### `periscope/app.py`

One-line lifespan addition: `await tmux_mirror.shutdown()` beside
`tmux_input.shutdown()`. Shutdown terminates every `_SessionMirror` process and
sentinels all queues — without it control clients leak across dev reloads.

## Patterns

**Used:**
- Frozen value-objects + pure functions for events and frames (`Output`,
  `GridSnapshot`, `build_reconcile_frame`) — the testing-without-tmux surface.
- Discriminated-by-class event union from `ControlParser.feed_line` — closed
  variant set.
- Functional registry (`subscribe()` + `_MIRRORS` dict) for per-session client
  reuse.
- Callback injection (`ReconcileTimer(fire=...)`, `_send_command(on_reply=...)`)
  — the ordering rule and fake-clock testing both fall out of it.

**Considered and rejected:**
- Module globals throughout (the `tmux_input.py` style) — N clients × per-pane
  state turns globals into parallel dicts keyed three ways; the class is the
  honest shape.
- A `MirrorManager` class wrapping the registry — a dict and two module
  functions suffice.
- Future-based reply correlation as the primary mechanism — explicitly
  forbidden by the spec's ordering rule for reconcile frames; futures exist
  only as a wrapper for data-only callers.
- An abstract transport interface (mirror vs hypothetical fallback) — the spec
  deliberately has no fallback; one implementation, no second foreseeable.
- Splitting parser/frames into separate modules now — see Decisions.
- Async-generator `subscribe()` instead of a `Subscription` class — a bare
  generator can't carry `request_reconcile()`.

## Test strategy

- **`tests/test_tmux_mirror.py` — unit, no tmux** (the bulk):
  - `ControlParser`: `%output` lines; `%begin/%end` token matching including a
    body line that starts with `%end`; `%error`; interleaved notifications
    around reply blocks; unknown notifications ignored.
  - `decode_octal`: `\134`-style escapes, `\\`, UTF-8 multibyte split across
    two notifications (decode bytes from each, concatenate, then UTF-8 — the
    split must round-trip).
  - `build_reconcile_frame`: exact-bytes snapshots for alt vs normal, short
    capture padded to height, cursor 0→1 indexing, visibility on/off, no `2J`
    anywhere in the normal-screen frame.
  - `ReconcileTimer` with injected `now` + patched `call_later`: quiesce fires
    at 150ms, max-interval fires during sustained output, idle never fires,
    `request()` immediacy.
- **`tests/test_tmux_mirror.py` — integration, real tmux** (skipif no tmux,
  following `tests/test_tmux_input.py::test_roundtrip_into_real_pane`'s
  pattern, but on a dedicated `tmux -L periscope-test` server per spec):
  - The closed-loop oracle: scripted TUI-ish output (bursts, alt-screen
    toggles) → subscriber stream → **pyte** → assert pyte's grid ==
    `capture-pane`'s grid.
  - Thesis-as-assertion: wrap the subscription with random byte drops, assert
    convergence after reconcile. This test only passes *because* of
    reconciliation — it pins the design's reason to exist.
  - Subscribe/unsubscribe/linger lifecycle and two-subscriber multiplexing.
- **`tests/routes/test_ws.py` — rewritten**: replace the mkfifo/add_reader
  mocks with a monkeypatched `tmux_mirror.subscribe` returning a fake
  `Subscription` (a queue the test feeds). Keep `fake_tmux` for the fork-path
  display-message/capture. The resize-before-initial-paint ordering assertion
  is re-expressed, not dropped (spec). New assertions: subscription EOF closes
  the WS; resize calls `request_reconcile`. The mocking here stays thin
  precisely because everything behavioral (parsing, frames, timing) is
  unit-testable below the route.
- **Mock-heaviness check:** nothing forces a mock — parser, decoder, frame
  builder, and timer are all directly constructible; the only mocked seam in
  route tests is the subscribe boundary itself.

## Decisions to sanity-check

1. **One file (`tmux_mirror.py`, ~450 LOC) vs splitting frames/parser into a
   sibling module.** Chose one file: it is one subsystem, matches the spec's
   stated layout and the package's one-file-per-subsystem table, and the units
   are import-path-stable for tests either way. Close because it lands above
   the 400-LOC comfort line; if implementation runs fat, `GridSnapshot` +
   `build_reconcile_frame` are the natural extraction (zero imports back into
   the mirror).
2. **`ReconcileTimer` as a separate class vs timing inline in `_SessionMirror`
   per-pane state.** Chose separate for fake-clock unit tests; close because it
   adds a name for ~50 lines of logic and the mirror is its only consumer.
3. **`Subscription` class vs plain async generator + module-level
   `request_reconcile(pane_id)`.** Chose the class: resize-reconcile belongs to
   the thing ws.py already holds. Close because the generator version is less
   code and the module function would work.
4. **Forward task closes the WS on subscription EOF** (behavior change from
   today's silent-FIFO). Spec-endorsed, but it is the one place the rewrite
   intentionally changes observable behavior — worth a conscious nod.
