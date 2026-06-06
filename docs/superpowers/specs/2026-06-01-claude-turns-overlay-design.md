# Claude Turns Overlay — Design Spec

**Date:** 2026-06-01
**Status:** draft, reviewed once, pending Tom approval
**Author:** Tom + Claude (brainstorm + spec-reviewer pass)

---

## Summary

Add a structured "turns" view to the periscope pane modal for Claude
sessions, **integrated alongside the live xterm mirror, not as a
separate tab.** The xterm view stays the primary, authoritative
rendering — full terminal fidelity, live updates, unchanged. JSONL turn
data decorates it in two places:

- **Gutter overlay** on top of the xterm viewport: a narrow column of
  DOM markers at anchored turn boundaries, repositioned on every
  scroll/render via the xterm buffer's `viewportY`/`length` to map
  absolute buffer lines to screen-y. We do not use
  `term.registerMarker` — it's cursor-offset-relative and can't pin
  arbitrary scrollback lines after the fact.
- **Tabbed side panel** sharing the existing `#modal-side` 280px column
  with the activity stream. A new "Turns" tab lists structured turns
  with expandable tool-call breakdowns. Default tab is "Turns" when
  the pane has a resolvable Claude JSONL; otherwise "Activity" (the
  current default).

Data source: the existing `history/` indexing pipeline. Pane → cwd →
JSONL via `live_transcript_for(cwd)` (`periscope/activity.py:211`). A new
shared helper `messages_from_jsonl(path, since_ts=None)` lives in
`history/search.py`, used by both `/api/history/session/:id` (existing)
and `/api/pane/turns` (new). No duplicated parsers.

Anchoring uses content matching: when a new user-turn arrives, scan
xterm's full buffer (`buffer.getLine(i)` from `0..buffer.length`) for
the user-message text and store the absolute line number on the turn
record. The gutter overlay renders dots at those lines whenever they
fall within the visible viewport. Turns that can't be anchored
(scrolled off the buffer entirely, message too short, no match) still
render in the side panel — the modal naturally splits into "live region
annotated, history structured."

## Goals

- **One integrated modal view.** Terminal mirror, gutter markers, and
  structured turn list all live in the same modal pane.
- **Read at a glance.** A long Claude session in the modal becomes
  navigable: scrub between turns, jump to a specific tool call, see
  current-turn boundary highlighted in the terminal.
- **Structured tool-call expansion.** Tool args + outputs (especially
  Bash command + stdout, Read paths, file diffs) render cleanly in the
  side panel, not collapsed behind the terminal's `Ctrl+R` UI.
- **No terminal regressions.** xterm rendering is untouched. Gutter
  markers are a separate overlay layer; xterm doesn't know they exist.
- **No xterm width regression.** Turns share `#modal-side`'s existing
  280px column with the activity stream; the xterm pane width is
  unchanged from today.
- **Approximate anchoring is fine.** Perfect line-to-turn alignment is
  impossible (cursor moves, alt-screen, redraws). The spec commits to
  "best-effort anchor on user-message text; show in side panel
  regardless."
- **Reuse `history/`.** No new JSONL parser. The single new piece of
  parsing logic is a `tool_use_id` pairing pass that walks the existing
  `Event` stream to back-patch tool results onto their corresponding
  tool uses.

## Non-goals

- **Shell panes.** Shell command/output chunking via OSC 133 is a
  separate, opt-in feature. Out of scope.
- **Rendering structured turns in place of the terminal.** xterm is
  still the live truth; the side panel is supplementary.
- **Editing past turns or branching from them.** Read-only view.
- **Cross-session navigation in the modal.** `/history` already does
  cross-session search.
- **Persisting custom annotations.** No "star this turn"; the existing
  notes section is pane-level.

## Key invariants this spec must preserve

1. **xterm rendering fidelity.** No surgery on the byte stream, no
   intercepting escape sequences, no replacing scrollback chunks. The
   gutter is a separate absolutely-positioned DOM overlay on top of
   the xterm container.
2. **`terminal.js` encapsulation.** `term` stays private. Expose a
   thin facade — `findBufferLineMatching(text, minLen)`,
   `getTurnOverlayCoords()`, `scrollToBufferLine(absoluteY)` — that
   `turns.js` consumes.
3. **No second 1.5s poll.** Turn data piggybacks on the existing
   `/api/pane` response that `modal.js` already polls every 1.5s. No
   duplicated request rate.
4. **Lifespan / cleanup boundary.** Nothing server-side lives past
   modal close. Per-pane parse cache (see Performance) is a
   module-level dict keyed by jsonl_path, evicted on lifespan
   shutdown via the existing `_bg`/`_task` cleanup pattern.
5. **Graceful degradation.** If `history/` isn't initialized, or
   `live_transcript_for(cwd)` returns None, the side-panel tab is
   hidden and the modal renders as today. No log spam.

## Data flow

```
tmux pane ─── pipe-pane ───► /ws/pane ───► xterm.js (live, unchanged)
                                              ▲
                                              │ DOM overlay layer
                                              │ (absolute-positioned dots,
                                              │  repositioned on scroll/render)
                                              │
periscope/activity.py:                        │
  live_transcript_for(cwd) → Path           static/turns.js
                  │
                  ▼
~/.claude/projects/<enc-cwd>/<sid>.jsonl
                  │
                  ▼ history/search.py:
                  │   messages_from_jsonl(path, since_ts=None)
                  │   — shared with /api/history/session/:id
                  │
                  ▼
            existing /api/pane response, extended:
            {
              ...existing fields,
              "turns": {
                "session_id": "...",
                "jsonl_path": "...",
                "messages": [ ... ],
                "next_since_ts": 1717250045000
              }
            }
                  │
                  ▼ modal.js's existing 1.5s poll
                  │
                  ▼
              static/turns.js renders panel + overlay
```

Pane → JSONL resolution runs server-side on each `/api/pane` request;
no caching of the resolution itself (Claude sessions rotate on
`--resume` / new sessions). The parse cache is keyed by jsonl_path so
it survives across polls for the same session.

## Server side

### Shared helper: `messages_from_jsonl`

New function in `history/search.py`, factored out of the existing
`get_session` body:

```python
def messages_from_jsonl(
    jsonl_path: str,
    since_ts: int | None = None,
) -> list[dict]:
    """Stream-parse a Claude JSONL into a list of structured messages.

    Returns user / assistant turns in JSONL order, with tool_use blocks
    on assistant turns back-patched with their paired tool_result
    content (matched by tool_use_id from later user-role events).

    Filters:
      - Skips events where raw.get("isMeta") is True.
      - Skips events where raw.get("isSidechain") is True.
      - Skips events with no user_text, no assistant_text, no tool_uses,
        and no tool_results.
      - Emits system events with subtype=="compact_boundary" as
        {"role": "system", "kind": "compact", "ts_ms": ...} — used as
        a divider in the UI; resets the anchor pass.

    If since_ts is set, only events with ts_ms > since_ts are returned.
    The pairing pass still walks the full file to resolve tool_use_ids
    that may have been emitted before since_ts.
    """
```

Implementation:

1. First pass: collect all `tool_results` by `tool_use_id` into a dict.
   This requires a full file walk; cannot be incremental.
2. Second pass: walk events, filter per rules above, attach
   `tool_uses[i]["result"]` from the dict where available. Tool uses
   with no matching result yet (in-flight during live polling) get
   `"result": null`.
3. Apply `since_ts` filter at message-emit time, not event-walk time.
4. Return the list.

`get_session` in `history/search.py:208` is rewritten to call this
helper for its `messages` field. Its public schema is unchanged.

### New endpoint behavior: `/api/pane` extended

Rather than a separate `/api/pane/turns` endpoint with its own poll,
extend the existing `/api/pane` response with a `turns` block:

```json
{
  "...existing pane fields": "...",
  "turns": {
    "session_id": "uuid",
    "jsonl_path": "/Users/.../<sid>.jsonl",
    "messages": [ ... ],
    "next_since_ts": 1717250045000
  } | null
}
```

`turns` is `null` when:
- `live_transcript_for(cwd)` returns None
- the resolved JSONL doesn't exist
- `history/` is not initialized (shouldn't happen post-install but
  guard for it)

The client passes `?turns_since_ts=N` to ask for incremental updates;
the server returns only messages with `ts_ms > N`. On first poll the
client omits the parameter and gets the full session.

### Pane → cwd resolution

`tmux display-message -t <target> -p '#{pane_current_path}'`. Same
subprocess pattern used elsewhere (`periscope/panes.py`,
`periscope/activity.py`). No new tmux call shape introduced.

### Filter rules (concrete)

Skip every JSONL event where any of:
- `raw.get("isMeta") is True`
- `raw.get("isSidechain") is True`
- `raw.get("type") == "system"` AND `subtype != "compact_boundary"`
  (file-history-snapshot, queue-operation, last-prompt, etc.)
- `parse_jsonl` returned an `Event` with no `user_text`, no
  `assistant_text`, empty `tool_uses`, AND empty `tool_results`

Emit `type == "system"` AND `subtype == "compact_boundary"` as a
divider message (`{"role": "system", "kind": "compact", "ts_ms": ...}`).
The frontend renders these as horizontal rules and resets the anchor
search range for subsequent turns.

### Performance: incremental parse cache, ship from day one

The spec-reviewer flagged that long Claude transcripts grow to tens of
MB during active days (per existing comments in
`periscope/activity.py:251,413`). At line-by-line `json.loads` rates,
a 30 MB JSONL costs ~50-150ms to fully parse — per 1.5s poll, per open
modal. That's not catastrophic, but it's avoidable.

Ship the cache in Phase 1, not as a follow-up:

```python
# periscope/routes/pane.py (or a sibling module)
_PARSE_CACHE: dict[str, dict] = {}
# {
#   jsonl_path: {
#     "size": int,           # last-seen file size
#     "messages": list[dict], # parsed-so-far
#     "tool_result_ids": dict[str, str],  # tool_use_id -> result content
#     "last_ts_ms": int,
#   }
# }

def get_turns_for_pane(cwd: str, since_ts: int | None) -> dict | None:
    jsonl = live_transcript_for(cwd)
    if jsonl is None:
        return None
    path = str(jsonl)
    cur_size = os.path.getsize(path)
    entry = _PARSE_CACHE.get(path)
    if entry is None or cur_size < entry["size"]:
        # First visit or file truncated/rotated — full parse.
        entry = _full_parse(path)
        _PARSE_CACHE[path] = entry
    elif cur_size > entry["size"]:
        # Append-only growth — parse the tail.
        _parse_tail(path, entry)
    # entry["messages"] holds the full known message list
    messages = entry["messages"]
    if since_ts is not None:
        messages = [m for m in messages if m["ts_ms"] > since_ts]
    return {
        "session_id": entry["session_id"],
        "jsonl_path": path,
        "messages": messages,
        "next_since_ts": entry["last_ts_ms"],
    }
```

`_full_parse` calls `messages_from_jsonl` and records `size` after.
`_parse_tail` opens the file at offset `entry["size"]`, reads new
bytes, parses new lines, extends `messages`, updates `tool_result_ids`
and back-patches any newly-arrived results onto prior messages still
in the cache. Truncation/rotation detection by `cur_size < entry["size"]`
forces a full reparse.

Cache is bounded by the number of currently-resolved JSONLs (one per
open Claude session) and evicted on lifespan shutdown.

## Frontend side

### New module: `static/turns.js`

Single responsibility: own the turn-panel DOM (within `#modal-side`'s
tab system) and the gutter-overlay DOM (positioned on top of
`#modal-xterm`).

Public API:
- `startTurns(target)` — called from `modal.js#openModal` after
  `startLiveTerminal`. Registers a callback with the modal-poll loop;
  no separate timer.
- `stopTurns()` — called from `closeModal()`. Removes overlay DOM,
  clears panel.
- `onPanePoll(paneData)` — called by `modal.js` with each `/api/pane`
  response. Reads `paneData.turns` and updates state.

`turns.js` does NOT import `term` directly. It calls a small facade
exposed by `terminal.js`:

```js
// static/terminal.js — additions to the existing exports
export function findBufferLineForText(needle, minLen) { ... }
export function getXtermGeometry() { ... }  // { cellHeight, cols, viewportY, length }
export function onXtermRender(cb) { ... }    // returns disposer
export function onXtermScroll(cb) { ... }   // returns disposer
export function scrollToBufferLine(absoluteY) { ... }
```

This keeps `term` private (consistent with `terminal.js`'s existing
"all terminal state stays private" comment at the top of the file) and
makes the facade independently testable.

### DOM layout

The xterm host is `<div id="modal-xterm">`. The existing `<aside
id="modal-side">` to its right holds activity + notes + link
buttons. **No new sibling panels.** Instead, `#modal-side` gains a tab
strip at the top:

```html
<aside id="modal-side" class="modal-side">
  <nav class="modal-side-tabs" role="tablist">
    <button class="modal-side-tab" data-tab="turns" hidden>Turns</button>
    <button class="modal-side-tab is-active" data-tab="activity">Activity</button>
  </nav>
  <div class="modal-side-tab-content" data-tab-content="turns" hidden>
    <!-- populated by turns.js -->
  </div>
  <div class="modal-side-tab-content" data-tab-content="activity">
    <!-- existing activity / notes / link-button content -->
  </div>
</aside>
```

The "Turns" tab is hidden when `paneData.turns` is null. When it
becomes non-null, the tab appears and — on first appearance for this
modal open — becomes the active tab. Subsequent tab switches are
sticky (user-driven only).

The gutter overlay is a separate DOM layer placed inside
`#modal-terminal-pane`, absolutely positioned over the left edge of
`#modal-xterm`:

```html
<div id="modal-terminal-pane" class="modal-pane">
  <div id="modal-xterm" class="modal-xterm"></div>
  <div id="modal-turn-gutter" class="modal-turn-gutter"></div>  <!-- NEW -->
  <aside id="modal-side" class="modal-side">...</aside>
</div>
```

`#modal-turn-gutter` is `position: absolute; left: 0; top: 0; bottom:
0; width: 8px; pointer-events: none;`. xterm renders normally; the
gutter sits on top with `pointer-events: none` so it doesn't capture
clicks. Individual turn dots inside the gutter get `pointer-events:
auto` for click-to-scroll-to-side-panel-row.

### Gutter overlay rendering

For each anchored turn:

1. On first arrival in `turns.js`, call
   `findBufferLineForText(anchor_hint, minLen=30)`. If found, store
   `absoluteY` on the turn record. If not, the turn renders in the
   side panel only.
2. On each `term.onRender` and `term.onScroll`:
   - Get geometry: `cellHeight`, `viewportY`, `length`.
   - For each turn with `absoluteY != null`:
     - If `absoluteY < viewportY` (scrolled above) or
       `absoluteY >= viewportY + visibleRows` (scrolled below):
       hide its dot.
     - Else: position dot at `top: (absoluteY - viewportY) *
       cellHeight px`. Show it.
3. When the buffer trims (`buffer.length` shrinks past `absoluteY`),
   the turn is permanently un-anchored. Mark its side-panel row as
   "scrolled off"; drop its dot from the gutter.

Dot styling: 6px circle, role-colored (user = blue, assistant =
purple, system/compact = orange divider). Current-turn dot pulses via
CSS animation.

Hover a dot → tooltip with turn role + timestamp + first line.
Click a dot → expand and scroll to the corresponding row in the side
panel.

### Side-panel rendering

Inside the "Turns" tab:

- Header: session-id short-hash + summary first line (from `history/`
  if available; from `first_user_msg` otherwise).
- Filter chip row: "All" / "User" / "Assistant" / "Tools" — multi-toggle.
- Turn list: one row per non-system turn + horizontal-rule rows for
  compact dividers. Each row: role tag, relative timestamp, 1-line
  preview.
- Click a row → expand inline: full text, tool calls.
- Tool calls expand individually: command/args at top, output below.
- A "scroll-to" link on each row, enabled only when the turn is
  currently anchored (has a live gutter dot).

### Anchoring failure modes

- **User message too short.** `MIN_ANCHOR_LEN = 30` characters AND at
  least two whitespace-separated tokens. Slash commands and one-word
  replies skip the anchor; the side-panel row still renders.
- **Message never appears in terminal** (e.g. assistant turn after a
  tool result with no visible user text). Side panel only; no dot.
- **Multiple matches in buffer.** Take the most recent (highest line
  index) — Claude rarely emits identical user messages back-to-back.
- **Scroll-out of buffer.** xterm's scrollback is 20000 lines. When
  `buffer.length < absoluteY + 1`, the line is gone. Dot removed,
  side-panel row marked "scrolled off."
- **/compact boundary.** Compact divider in the side panel. The
  anchor search range for the NEXT user turn restarts at the first
  buffer line written after the compact marker — tracked by
  remembering `buffer.baseY` at compact arrival time and only
  searching lines >= that snapshot for new turns.

### Current-turn highlight

The most-recent non-system turn (highest `ts_ms`) gets a pulse
treatment in both places: side-panel row glow, gutter dot pulse via
CSS `@keyframes`. When a new turn arrives, the prior one fades to
normal. Pure CSS, no JS timer.

### Coexistence with the existing modal-side

Existing activity / notes / link buttons stay in the Activity tab,
unchanged. The existing media query at `static/styles.css:1603` that
hides `.modal-side` below 1100px works for both tabs without
modification.

The current default tab is decided per-modal-open:

- If `paneData.turns != null` on the first poll: default tab = Turns.
- Otherwise: default tab = Activity.

After the user clicks a tab, that choice sticks for the rest of the
modal lifetime — no auto-switching.

## Failure modes and open questions

The spec-reviewer pass closed most of the original open questions.
Remaining items:

1. **In-flight tool calls.** The pairing pass leaves
   `tool_uses[i].result = null` for tool calls whose result hasn't
   landed yet. The side panel should render these as "running…" with
   a spinner. Already implied by the schema; calling it out so it
   doesn't get missed.
2. **`_resuming` interaction.** During `claude --resume`, periscope's
   `_resuming` dict tracks the in-flight resume. There's a brief
   window where the new pane's cwd is set but `live_transcript_for`
   may return either the new or the old JSONL depending on mtime
   ordering. Out-of-scope to address here, but flag for Phase 1
   testing: if a resumed session's first poll returns the wrong
   transcript briefly, does the UI recover cleanly on the next poll?
3. **Anchor search cost on initial poll.** `findBufferLineForText`
   walks up to `buffer.length` (20000) lines on each unanchored turn.
   For a session with 200 turns at first modal-open, that's up to
   4M string-includes. Mitigation: cache scanned-already line index
   in `turns.js` and resume from there on subsequent polls. Probably
   fine in practice; flag for measurement.

## Implementation phases

Commit-as-you-go on `main` per project convention. Phase boundaries
exist for review/dogfood checkpoints, not feature-branch gates.

### Phase 1 — Shared parser + `/api/pane` extension (1 day)

- Add `messages_from_jsonl(path, since_ts)` to `history/search.py`,
  with explicit filter rules and the tool-use/result pairing pass.
  Refactor `get_session` to call it. Unit tests for: pairing,
  isMeta/sidechain filtering, compact_boundary emission, since_ts
  filter, in-flight tool_use (no result yet).
- Add `_PARSE_CACHE` and `get_turns_for_pane(cwd, since_ts)` to a new
  `periscope/turns.py` module.
- Extend the existing `/api/pane` route to include the `turns` block.
- Verifiable via `curl 'http://127.0.0.1:8766/api/pane?session=foo&index=0'`.

### Phase 2 — Side-panel tab only (1 day)

- DOM: add tab strip to `#modal-side`; new `static/turns.js` renders
  the Turns tab.
- Render turns + compact dividers + tool-call expansion.
- Default-tab logic per the spec.
- **No gutter overlay yet.** Side panel is purely additive.
  Dogfoodable floor: if anchoring later proves too fragile, Phase 2
  alone is shippable.

### Phase 3 — Gutter overlay on xterm (1 day)

- Facade additions to `terminal.js`:
  `findBufferLineForText`, `getXtermGeometry`, `onXtermRender`,
  `onXtermScroll`, `scrollToBufferLine`.
- `#modal-turn-gutter` DOM layer in `index.html`.
- Gutter rendering loop in `turns.js`: position dots on scroll/render.
- Anchor search per spec, with `MIN_ANCHOR_LEN = 30` and two-token
  rule. Stress test on a real long session.

### Phase 4 — Polish (½ day)

- Compact-boundary anchor reset.
- Filter chip row in side panel.
- CSS pulse animation for current turn.
- Width-priority verification: ensure xterm pane is unchanged width.
- Edge cases from dogfooding.

## Out-of-scope follow-ups

- Multi-pane "all my Claude sessions" view.
- Branching / forking from a past turn.
- Diff-aware tool-call rendering for the Edit tool.
- Live turn count badge on the dashboard card.
- Search-within-panel.
- Decoupling turns polling from the 1.5s `/api/pane` cadence if the
  combined response gets uncomfortably large for long sessions.

## What changed from the first draft

For reviewers comparing against the v1 draft:

- **Anchoring mechanism.** `term.registerMarker` does not work for
  arbitrary scrollback lines (it's a cursor offset). Replaced with a
  DOM-overlay layer repositioned on every `onRender` / `onScroll`.
- **Parsing.** `/api/pane/turns` collapsed into an extension of the
  existing `/api/pane` response. Shared `messages_from_jsonl` helper
  lives in `history/search.py` and is also used by `get_session`.
- **Tool result pairing.** Acknowledged as new logic with explicit
  unit-test coverage in Phase 1, not a hand-wave.
- **Event filtering.** Concrete skip list (isMeta, isSidechain,
  non-compact system events, empty events).
- **Performance.** Incremental parse cache ships in Phase 1, not as a
  "maybe later." Transcripts grow to tens of MB, not the "<2 MB" the
  v1 draft claimed.
- **DOM layout.** No new sibling panel that squeezes xterm. Turns
  share `#modal-side`'s 280px column with activity as a tab. Gutter is
  an 8px overlay on top of xterm with `pointer-events: none`.
- **`terminal.js` encapsulation.** `term` stays private; new facade
  methods replace any direct access.
- **Anchor length threshold.** Bumped from 12 to 30 chars + two-token
  requirement based on real Claude user-message patterns (short slash
  commands, one-word replies).
- **Default tab.** Decided: Turns when a JSONL resolves, Activity
  otherwise.
