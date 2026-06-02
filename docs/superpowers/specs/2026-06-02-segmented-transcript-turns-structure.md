# Segmented Transcript — Claude Turns — Structure Proposal

**Date:** 2026-06-02
**Spec:** `2026-06-02-segmented-transcript-turns-design.md` (reviewed)
**Consumes:** this is the structural blueprint the implementation plan follows.

The spec locked the hard decisions (stateless full-resend, endpoint location,
`messages_from_jsonl` home, `get_turns_for_pane` in a new `turns.py`, reconcile
by `uuid`, persist-don't-remount the terminal). This doc only resolves what the
spec left open: file/module split, function-vs-component decomposition, signal
placement, toggle wiring, and per-module test strategy. No structural
disagreement with the spec — see the one flag at the end.

---

## 1. Assumptions

Filling spec gaps; each is the most-likely reading, called out so review can
veto cheaply.

- **Default mode is transcript-*existence*-driven, not `is_claude`-driven**
  (resolved §9). `is_claude` (`panes.py:580`, read by `RailRows.jsx:105`) means
  "status line detected," which is true for a freshly-launched pane sitting at
  the trust / channel-accept prompt with *zero* turns. Defaulting such a pane to
  Transcript would hide the terminal exactly when the user needs it to accept
  channels and type the first prompt. So: **default Terminal; auto-promote to
  Transcript once the pane's poll first returns non-empty `messages`, once per
  pane, unless the user has manually toggled.** Manual choice always wins.
- **The selected pane polls regardless of displayed sub-mode.** The poll is
  gated by "is this the current rail selection," NOT by "is Transcript visible"
  — otherwise a Terminal-mode pane never polls, never discovers its transcript,
  never auto-promotes (circular). One selected pane → one poll; trivial cost.
- **Poll cadence: 2s** (spec's lean), constant `TURNS_POLL_MS = 2000` mirroring
  `DETAIL_POLL_MS` (`Detail.jsx:29`) — **but fire the first poll immediately on
  selection**, not after the first interval, so an established pane's
  Terminal→Transcript promotion happens in tens of ms (no perceptible flash).
- **Toggle affordance: a small segmented control rendered by `<PaneHeader>`**
  (the existing header), receiving mode + setter as props. Cosmetic per the
  spec's open question; keeping it in the existing header avoids a third
  header element.
- **`messages_from_jsonl` returns `list[dict]`** (plain dicts, not a dataclass)
  — it crosses the HTTP boundary as JSON and `get_session` already shapes
  `messages` as dicts. No value-object type earns its keep here.
- **Tool-call rendering is data-driven by `name`**, not a component-per-tool
  class hierarchy. Bash/Read/Edit differ only in *which input keys* they
  surface; one `<ToolCall>` with a small per-name field-picker covers them and
  every other tool falls back to a generic input dump. (See §5 — this is the
  over-abstraction the per-tool-component split would have been.)
- **`compact` dividers** are emitted inline in the `messages` list as
  `{role:"system", kind:"compact", uuid, ts_ms}`; the renderer switches on
  `m.role === "system" && m.kind === "compact"`. No separate list.

---

## 2. File layout

```
history/
  search.py                         CHANGED  + messages_from_jsonl(); get_session refactored onto it
  tests/test_search.py              CHANGED  + messages_from_jsonl unit tests
  tests/fixtures/turns_session.jsonl NEW     fixture w/ isMeta, isSidechain, compact_boundary, in-flight tool_use

periscope/
  turns.py                          NEW      get_turns_for_pane(cwd) — stateless resolver
  routes/pane.py                    CHANGED  + GET /api/pane/turns
tests/
  test_turns.py                     NEW      get_turns_for_pane unit tests (mirror convention)
  routes/test_pane.py               CHANGED  + /api/pane/turns route tests

static/src/
  store.js                          CHANGED  + transcriptMode, transcriptSeen signals (see §4)
  split/
    Detail.jsx                      CHANGED  + toggle, computed mode, keep-mounted transcripts (review-iframe pattern)
    Transcript.jsx                  NEW      <TranscriptView> + poll hook + TurnSegment/ToolCall sub-components
```

Rationale for the frontend split:

- **One new file, `Transcript.jsx`**, not a `transcript/` subdir. The whole
  renderer is one tightly-coupled concern (~250–350 LOC: poll hook + segment
  list + turn segment + tool-call render). Periscope's `split/` peers are
  single files of comparable size (`Detail.jsx` is 359 LOC). A subdir is
  premature until shell-blocks (the next milestone) adds a second renderer —
  *then* split. Flagging the future split, not pre-paying it.
- **The toggle lives in `Detail.jsx`** (in `PaneDetail`/`PaneHeader`), not in
  `Transcript.jsx` — it owns *which child shows*, and it already owns the
  keyed `<Terminal>` whose persist-don't-remount it must preserve.

Backend honors the one-file-per-subsystem + `tests/test_<module>.py` mirror:
`periscope/turns.py` ↔ `tests/test_turns.py`. `messages_from_jsonl` lives with
its existing consumer (`get_session`) in `search.py` per spec — not a new
module — so it's covered in `history/tests/test_search.py`, not a new file.

---

## 3. Per-module structure

### `history/search.py: messages_from_jsonl` — plain function (rung 1)

```python
def messages_from_jsonl(jsonl_path: str) -> list[dict]:
```

Stateless pure transform over `parse_jsonl(path)` Events → list of message
dicts. Two-pass (collect `tool_results` by `tool_use_id`; then walk + filter +
back-patch + stamp `uuid`), exactly as the spec's implementation sketch. No
state, no polymorphism → a function. `get_session` is rewritten to call it for
its `messages` field (dropping the inline parse at `search.py:232-245`).

Reuses: `history/jsonl.py:parse_jsonl` / `Event` (no new parser); reads
`ev.raw` for the `isMeta`/`isSidechain`/`subtype` filters (those keys are *not*
lifted onto `Event` by `_classify` — confirmed `jsonl.py:45-55`), and
`ev.tool_uses` / `ev.tool_results` / `ev.uuid` / `ev.ts_ms` for the rest.

### `periscope/turns.py: get_turns_for_pane` — plain function (rung 1)

```python
def get_turns_for_pane(cwd: str) -> dict | None:
```

Stateless resolver. `live_transcript_for(cwd)` (reuse `activity.py:211`) → None
returns `None` (caller emits `{turns: null}`); else
`{session_id, jsonl_path, messages: messages_from_jsonl(path)}`. No
module-level cache, nothing for lifespan to evict (invariant preserved). One
function, one file — earns the module by being a distinct subsystem with its
own test mirror, per convention. Imports only `periscope.activity` +
`history.search` (no `from server import`).

`session_id` derives from the JSONL stem (the file is `<sid>.jsonl`); use
`Path(path).stem` rather than re-reading the file.

### `periscope/routes/pane.py: GET /api/pane/turns` — route function

```python
@router.get("/api/pane/turns")
def pane_turns(session: str, index: int) -> dict:
```

Sibling to `/api/pane`. Resolves cwd via the same
`tmux("display-message", "-t", target, "-p", "#{pane_current_path}")` shape the
existing route uses (`pane.py:49-53`), then `get_turns_for_pane(cwd)`. Returns
the dict, or `{"turns": None}` when the resolver returns `None`. Session/index
as query params (invariant 6). Errors via `HTTPException` (e.g. a genuinely
broken `display-message`), never `{"ok": False}`.

### `static/src/split/Transcript.jsx` — function components + one hook

Component tree (all function components — Preact has no class-component need
here):

```
<TranscriptView target=... active=...>      // poll lifecycle + signal-backed list
  └─ <TurnSegment m=... expanded=...>        // one per message, keyed by m.uuid
       ├─ (role==="system",kind==="compact") → <CompactDivider/>   // <hr>
       └─ else: header (role tag · relTime(m.ts_ms/1000) · 1-line preview)
                + expanded body:
                  text
                  <ToolCall t=...>           // one per m.tool_uses[i]
```

- **Poll lifecycle: a `useTranscriptPoll(target, selected)` hook inside
  `Transcript.jsx`**, not a shared module like `grid/poll.js`. Rationale:
  `grid/poll.js` is the *single global* `/api/state` loop every surface shares;
  this poll is *per-pane, lifecycle-scoped* — it ticks while the pane is the
  current rail selection (in EITHER sub-mode, per §1's anti-circularity rule)
  and stops when another pane is selected. Same shape as `SidePanel`'s
  `useEffect` poll (`Detail.jsx:152-166`), parameterized by `(target,
  selected)`, **firing once immediately on becoming selected** then every
  `TURNS_POLL_MS`. On the first non-empty response it records the pane in
  `transcriptSeen` (§4) to drive auto-promotion. Don't promote to a store
  action; nothing else calls it.
- **`<TurnSegment>` is keyed by `m.uuid`** at the list map. Plain Preact
  reconciliation: an in-flight tool result filling in on a later poll updates
  that segment in place; a `jsonl_path` change (resume) replaces the whole
  list; expand state is **local to `<TurnSegment>`** (a `useState` bool) and
  survives because keyed instances persist across the per-poll list swap and
  because the whole `<TranscriptView>` is kept mounted across pane switches
  (§5). No global expanded signal needed — keep-mounted makes it free.
- **`<ToolCall>` is one component, data-driven by `t.name`** (§5). `result ===
  null` → "running…" spinner; else output below the input.
- Reuses `util.js:relTime` with `m.ts_ms / 1000` (the spec's epoch-seconds
  trap), `util.js:targetQuery` for the poll URL.

---

## 4. Signal placement

Two new signals in `store.js` (transient read model — nothing persisted,
matching the spec's "not a persisted pref initially"):

```js
export const transcriptMode = signal({});   // { [pid]: "transcript" | "terminal" }  — EXPLICIT user override only
export const transcriptSeen = signal({});   // { [pid]: true }  — set on first non-empty poll (drives auto-promote)
```

The displayed mode is **computed**, not stored, from these two plus pane type:

```js
const mode = !w.is_claude            ? "terminal"
           : transcriptMode.value[w.pid] ?? (transcriptSeen.value[w.pid] ? "transcript" : "terminal");
```

- **`transcriptMode`** holds only an *explicit* user toggle. Absent ⇒ fall
  through to the computed default. This is what makes manual choice sticky and
  auto-promotion non-destructive (auto-promotion never writes here).
- **`transcriptSeen`** is set once by the poll hook the first time a pane's
  response has non-empty `messages`. Flipping it changes the computed default
  from "terminal" to "transcript" — the auto-promote — without touching the
  user-override map, so a later manual toggle to Terminal still wins. Both are
  signals (not local state) because `<PaneDetail>` reads them every render to
  pick the child, and they must survive selecting away and back.

Why two signals instead of one "write transcript on promote": keeping
*user-said* (`transcriptMode`) separate from *system-observed* (`transcriptSeen`)
means the promote is a pure observation with no risk of clobbering a real user
choice — the alternative (auto-writing into `transcriptMode`) conflates them and
makes "did the user pick this?" unanswerable.

Everything else stays **local**: the polled `messages` list (large,
single-reader, pane-scoped — `useState` in `<TranscriptView>`, mirroring
`SidePanel`'s local `paneData` at `Detail.jsx:150`) and per-segment expand
state (local to `<TurnSegment>`, §3). Scroll position is preserved by the DOM
via keep-mounted (§5), needing no signal at all.

---

## 5. Toggle + persist-don't-remount wiring

Two persistence problems, one mechanism. (1) The live `<Terminal>` must not
tear down when you flip to Transcript. (2) A transcript's **scroll position and
expanded segments** must survive navigating to another pane and back (the user's
explicit requirement). Both are the persist-don't-remount discipline `<Detail>`
already uses for review iframes (`Detail.jsx:282-307`, `352`) — applied to two
widgets.

`<Detail>` is restructured so transcripts follow the **keep-every-opened-one-
mounted** pattern, exactly as it already does for review iframes:

```jsx
// In <Detail>: track every Claude pane whose transcript has been opened, prune
// to live panes — identical to the `opened` review-set at Detail.jsx:288-294.
const openedTranscripts = useRef(new Set());
if (isPane && paneW?.is_claude) openedTranscripts.current.add(paneW.pid);
// prune to pids still in `windows` (mirror the review prune loop)...

// Render the SELECTED pane's PaneDetail (header, side, terminal) as today,
// PLUS keep each opened transcript mounted & CSS-hidden unless it's the
// selected pane in Transcript mode:
{[...openedTranscripts.current].map((pid) => {
  const tw = lookupWindow(pid);
  const shown = isPane && paneW?.pid === pid && modeFor(pid) === "transcript";
  return (
    <div key={pid} style={shown ? "display:contents" : "display:none"}>
      <TranscriptView target={tw?.target} pid={pid}
                      selected={isPane && paneW?.pid === pid} />
    </div>
  );
})}
```

```jsx
function PaneDetail({ w }) {
  const mode = computeMode(w);                 // §4 formula
  // ...activeTarget effect unchanged...
  return (
    <div id="detail-pane" class="detail-pane">
      <PaneHeader w={w} mode={mode}
                  onMode={(next) => setTranscriptMode(w.pid, next)} />
      <div class="detail-pane-body">
        {/* Terminal: ALWAYS mounted, single instance keyed on pid
            (reconnect-not-remount, coupling #5); hidden, not unmounted, when
            Transcript is shown — xterm/WS never tears down. */}
        <div style={mode === "terminal" ? "display:contents" : "display:none"}>
          <Terminal key={w.pid} id="detail-xterm" class="detail-xterm"
                    target={w.target} onPaste={handleDetailPaste} />
        </div>
        {/* TranscriptView is NOT rendered here — it lives in <Detail> as a
            kept-mounted sibling (above) so its scroll/expand survive pane
            switches, the way review iframes do. This empty slot is where it
            visually lands via the shared #detail-pane-body grid. */}
        <SidePanel key={w.target} target={w.target} />
      </div>
    </div>
  );
}
```

Key points (each maps to a named existing pattern):

- **Terminal: single instance, keyed on `pid`, hidden-not-unmounted.** The
  reconnect-not-remount contract (`Detail.jsx:11-14`). Distinct mechanism from
  transcripts on purpose: the terminal *can* cheaply re-derive its state on
  reconnect (capture-pane snapshot), so single-instance-reconnect is fine; a
  transcript has no such restore, so it needs keep-mounted. Two widgets, two
  fit-for-purpose persistence strategies — noted so the asymmetry reads as
  deliberate.
- **Transcripts: keep-every-opened-one mounted, pruned to live panes** — the
  literal review-iframe pattern (`Detail.jsx:288-307`). Scroll position (DOM)
  and expand state (keyed local component state, §3) survive for free; no
  scroll signal, no global expanded signal.
- **`selected` prop gates the poll** (not mounting, not sub-mode). Only the
  currently-selected pane's `<TranscriptView>` ticks (§1 anti-circularity);
  the others stay mounted-but-idle. Flipping to Terminal does **not** stop the
  poll (auto-promote needs it); switching panes does.
- **Non-Claude pane: never added to `openedTranscripts`, no toggle.** `mode`
  forced "terminal"; `<PaneHeader>` renders the segmented control only when
  `w.is_claude`.
- **`SidePanel` untouched** — keeps its own `/api/pane` 1.5s poll regardless of
  mode (metadata sidebar is orthogonal to terminal vs transcript).

Note the layout coupling this introduces: `<TranscriptView>` is mounted as a
child of `<Detail>` (`#detail`) rather than inside `#detail-pane-body`, so the
CSS must place it in the same region the terminal occupies. The review iframes
already live at this level, so the grid precedent exists — but the transcript
needs to visually align with `#detail-pane-body`, a CSS task to verify in
Phase 2 (flagged so it isn't discovered late).

---

## 6. Test strategy

### Backend — real unit tests, no mocks on the parse path

**`history/tests/test_search.py` (+ `messages_from_jsonl` cases)** — unit, real
JSONL fixture, zero mocking. Add `tests/fixtures/turns_session.jsonl` (the
existing `normal_session.jsonl` has tool_use/tool_result/text but **no**
`isMeta`/`isSidechain`/`compact_boundary` — confirmed by inspection — so the
filter/divider paths need a purpose-built fixture). Assert:

- **Pairing**: an assistant `tool_use` gets its paired `tool_result.content`
  back-patched onto `tool_uses[i]["result"]` (key `tool_use.id` ↔
  `tool_result.tool_use_id`).
- **`ev.raw` filters**: a line with `isMeta:true` is dropped; `isSidechain:true`
  is dropped; a `type:"system"` line with `subtype != "compact_boundary"` is
  dropped — and these are read from `ev.raw`, so the fixture must carry the
  flags at the *raw* JSONL level (regression guard against someone "fixing" it
  to read `ev.is_meta`, which doesn't exist).
- **Compact emission**: a `type:"system", subtype:"compact_boundary"` line is
  emitted as `{role:"system", kind:"compact", uuid, ts_ms}`.
- **In-flight tool_use**: a `tool_use` with no later matching `tool_result`
  → `result` is `None` (the "running…" source).
- **`uuid` present + stable**: every emitted message carries a non-null
  `uuid`, and two parses of the same file produce the same uuids in the same
  order (reconciliation depends on this).
- **`get_session` refactor regression**: the existing
  `test_get_session_returns_full_row_and_messages` still passes, *plus*
  assistant turns now carry `tool_uses[i]["result"]` and `uuid` (the schema
  gain the spec calls inert for `/history`).

**`tests/test_turns.py` (NEW, mirror)** — unit. `live_transcript_for` reads the
real filesystem (`~/.claude/projects` encoded dir); test by `monkeypatch`-ing
`periscope.activity._PROJECTS_DIR` to a `tmp_path` seeded with an encoded dir +
a JSONL whose recorded `cwd` matches (the pattern `test_activity.py` already
uses) — **real files, not a mocked `live_transcript_for`.** Assert: cwd with a
matching transcript → `{session_id, jsonl_path, messages}` with messages from
the real parse; cwd with no match / no dir → `None`. This is the
testable-directly win: `get_turns_for_pane(cwd)` takes a plain string and hits
real files, so there's no hard-to-construct object forcing mocks.

**`tests/routes/test_pane.py` (+ `/api/pane/turns`)** — route test via the
FastAPI `client`, mocking only the `tmux` boundary (the `display-message` call)
exactly as the existing `test_pane_returns_parsed_payload` does. Two cases:
(1) tmux returns a cwd whose transcript resolves → 200 with
`session_id`/`messages`; mock `get_turns_for_pane` *or* point it at a tmp
transcript — prefer pointing it at a real tmp JSONL so the route↔resolver wiring
is exercised end-to-end (the Q1-2026 mocked-migration lesson: a mock that
passes while the real path is broken). (2) resolver returns `None` →
200 `{turns: null}`.

### Frontend — manual-verified, per project convention

No frontend test suite exists; do **not** invent one. Manual verification on
the `:8766` dev worktree, per the spec's Phase 2/3:

- Toggle flips Transcript ⇄ Terminal; terminal does **not** reload on
  toggle-back (watch for xterm reconnect, not a fresh capture-pane repaint).
- In-flight tool call shows "running…", then fills its result in place on a
  later poll without the segment remounting (expanded state persists).
- A `claude --resume` path-flip full-replaces the list (the spec's flagged
  manual test).
- Non-Claude pane shows Terminal only, no toggle.
- `relTime` renders sane deltas (guard against the `ts_ms`-raw 57-year bug).

The structure is *built* to make this verifiable without a harness: the only
logic that could hide a bug behind a mock (parsing, pairing, filters) lives in
the backend where it gets real-dependency unit tests; the frontend is thin
reconciliation over that data.

---

## 7. Patterns

Used:
- **Plain functions** for both backend units (parse transform, resolver) — no
  state, no polymorphism.
- **Function components + a scoped poll hook** (`useTranscriptPoll`),
  mirroring `SidePanel`'s local-`useEffect` poll rather than the global
  `grid/poll.js` loop.
- **Hide-don't-destroy via display toggling**, reusing the review-iframe and
  keyed-`<Terminal>` persistence patterns already in `Detail.jsx`.
- **Signals for survive-remount control state** (`transcriptMode` override +
  `transcriptSeen` promotion); local state for the large pane-scoped polled list
  and per-segment expand; DOM keep-mounted for scroll position.
- **Data-driven dispatch** for tool rendering (switch on `t.name`).

Considered and rejected:
- **A component-per-tool hierarchy** (`<BashCall>`/`<ReadCall>`/`<EditCall>`):
  over-abstraction. The tools differ only in which input keys to surface;
  there's no behavioral polymorphism and an unbounded tail of other tools.
  One `<ToolCall>` with a per-name field-picker + generic fallback. Revisit
  only if a tool needs genuinely custom *interaction* (none does in v1).
- **Promoting the transcript poll into `grid/poll.js` / a store action**: that
  module is the single global state loop; this is a per-pane lifecycle poll
  with exactly one caller. Keeping it local matches `SidePanel`.
- **Putting the polled `messages` list in `store.js`**: it's large,
  single-reader, pane-scoped — local state is correct; only control state that
  must survive remount goes global.
- **A new `transcript/` subdir**: premature for one renderer. Split when
  shell-blocks adds a second.
- **A server-side parse/`since_ts` cache**: explicitly deferred by the spec
  (the in-flight back-patch trap). Not structured for.
- **A `messages_from_jsonl` value-object type** (frozen dataclass per message):
  the data crosses an HTTP boundary as JSON and has one shape; plain dicts,
  consistent with the existing `get_session` `messages` field.

---

## 8. Decisions to sanity-check (close calls)

1. **Poll hook local to `Transcript.jsx` vs. a shared poll module.** Chose
   local (mirrors `SidePanel`). Alternative: a small shared
   `split/transcriptPoll.js`. Close because periscope *does* have a precedent
   for extracting the one global poll (`grid/poll.js`) — but that one is
   shared by many surfaces; this has one caller. Flagging in case Tom wants
   poll loops uniformly co-located.

2. **Resolved by Tom — expansions (and scroll) persist across pane switches.**
   Achieved structurally via keep-every-transcript-mounted (§5), so expand
   state can stay *local* to `<TurnSegment>` and scroll stays in the DOM — no
   global expanded signal. (Earlier draft proposed a global `pid:uuid`
   signal; keep-mounted makes it unnecessary.)

3. **Route returns a real transcript in the test vs. mocking the resolver.**
   Chose real-tmp-JSONL end-to-end in the route test (Q1-2026 mocked-migration
   lesson). Alternative: mock `get_turns_for_pane` for a faster, more isolated
   route test and rely on `test_turns.py` for the real path. Close because the
   route logic is genuinely thin (one tmux call + one resolver call) so a mock
   there is defensible; erring toward the real path because the wiring is
   exactly what mocks have burned us on before.

---

## 9. Resolved: `is_claude` ≠ "transcript exists"

**The tension (now resolved by Tom).** `is_claude` (`panes.py`, smoothed from
the status line) is true for a freshly-launched pane sitting at the trust /
channel-accept prompt with zero turns — Tom's launch ritual hits this on every
session start. Defaulting such a pane to Transcript would render an empty state
exactly when the terminal is the one thing needed (to accept channels, type the
first prompt). The spec's "default Transcript for a Claude pane" can't key off
`is_claude`, and "`turns:null` → no toggle" can't hold literally because the
mode is decided client-side before the first poll.

**Resolution (folded into §1/§4/§5):**

- **Default Terminal; auto-promote to Transcript** the first time the pane's
  poll returns non-empty `messages` (`transcriptSeen`), once per pane, unless
  manually toggled. The selected pane polls in *both* sub-modes so promotion
  can fire while showing Terminal. First poll fires immediately on selection,
  so an established pane promotes in tens of ms — no visible flash.
- **Toggle visibility:** shown for any `is_claude` pane (harmless optimism); if
  flipped to Transcript before any turns exist, it renders a "no transcript
  yet" empty state.

Net: anything worth reading lands in Transcript near-instantly (honoring the
"default Transcript" intent), while a fresh pane stays on Terminal through the
channel-accept moment.
