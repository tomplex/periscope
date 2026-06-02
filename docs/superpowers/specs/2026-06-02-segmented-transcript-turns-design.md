# Segmented Transcript — Claude Turns (consolidated, post-cutover) — Design Spec

**Date:** 2026-06-02
**Status:** reviewed (spec-reviewer pass applied) — ready for structure-proposer
**Author:** Tom + Claude
**Supersedes (for the turns half):**
- `2026-06-01-segmented-transcript-design.md` — keeps its first-principles
  framing and the *shell-blocks* milestone; this doc is the concrete,
  current plan for the **Claude-turns half**.
- `2026-06-01-claude-turns-overlay-design.md` — **fully superseded.** Its
  server half is carried forward here; its UI half (xterm gutter overlay +
  buffer anchoring + modal-side tab) is **deleted**, not ported.

---

## Summary

Give periscope's split-view **detail pane** (`static/src/split/Detail.jsx`) a
**transcript** content mode for Claude panes: the pane's conversation renders as
a list of structured, collapsible **turn segments** (user message → assistant
text → tool calls), sourced from the `history/` JSONL pipeline. For a Claude
pane the detail pane shows the **Transcript by default**, with a toggle to the
existing live `<Terminal>`.

This is the first, no-emulation half of the segmented-transcript model. Shell
command blocks (OSC 133, per-block terminal emulation) remain a separate later
milestone — see the framing spec; out of scope here.

## What changed since the prior specs (read this — it's why they're superseded)

1. **The Preact frontend cutover shipped.** The renderer is a Preact component
   in `<Detail>`, not vanilla DOM in the modal. Stream view is gone; the view
   switch is grid ⇄ split.

2. **The anchoring machinery is deleted, not ported.** The turns-overlay spec's
   center of mass — `findBufferLineForText`, gutter dots repositioned on every
   `onRender`/`onScroll`, content-matching with `MIN_ANCHOR_LEN`, scroll-off
   handling, compact-boundary anchor resets, the `terminal.js` facade — existed
   only to *decorate a live xterm*. In the segmented model **turns are the
   render**, so there is nothing to anchor. None of that code is written. This
   is the single biggest simplification: the turns half is strictly less code
   and lower risk than the spec it replaces.

3. **The transport changed.** The old plan ("extend `/api/pane`, piggyback its
   1.5s modal poll") is dead for this renderer's home. Verified against the
   code:
   - The **modal** (grid view) polls `/api/pane` every 1.5s.
   - The **split-view `<Detail>`** — where the renderer lives — reads the global
     `windows` signal (`/api/state`, 3s) and does **not** poll `/api/pane` at
     all.
   - `/api/pane` is a heavy aggregator (capture-pane + git + PR + activity +
     LGTM); pulling it per-cycle just for turns is wasteful.

   So turns get a **dedicated endpoint** (`GET /api/pane/turns`) that `<Detail>`
   polls only while a Claude pane is selected. See Transport.

## Scope

**In:** turn segments for Claude panes in `<Detail>` — scrub between turns,
expand a turn to its text + tool calls (Bash command + stdout, Read paths, Edit
input), in-flight tool calls show "running…"; the server parser + endpoint that
feeds it; Transcript-default-with-Terminal-toggle in the detail pane.

**Out:** shell command blocks / OSC 133 / per-block terminal emulation (later
milestone); the grid-view modal (this targets split-view `<Detail>` — the modal
keeps its current xterm-only view); editing/branching from a past turn;
cross-session navigation (`/history` owns that); persisted annotations.

## Server half (carried forward — clean and well-specified)

### Shared helper: `messages_from_jsonl` in `history/search.py`

Factor the JSONL→messages logic out of `get_session` (currently inline at
`search.py:232-245`, which drops tool results entirely) into:

```python
def messages_from_jsonl(jsonl_path: str) -> list[dict]:
    """Stream-parse a Claude JSONL into structured messages, full file.

    User / assistant turns in JSONL order. Assistant tool_use blocks are
    back-patched with their paired tool_result content (matched by
    tool_use.id == tool_result.tool_use_id from later user-role events).
    Unpaired tool_uses (in-flight) get result=None.

    Each emitted message carries `uuid` (from Event.uuid) as its stable
    identity — the client merges/reconciles on it (NOT on ts_ms, which
    collides: an assistant text + its tool_uses share a timestamp).

    Filters (skip the event if ANY holds) — note these read `ev.raw`, since
    `_classify` does NOT lift these onto the Event (jsonl.py:45-55):
      - ev.raw.get("isMeta") is True
      - ev.raw.get("isSidechain") is True
      - ev.type == "system" AND ev.raw.get("subtype") != "compact_boundary"
      - Event has no user_text, no assistant_text, no tool_uses, no tool_results

    Emits ev.type=="system" AND ev.raw.get("subtype")=="compact_boundary" as
      {"role": "system", "kind": "compact", "uuid": ..., "ts_ms": ...} — a
      UI divider. (activity.py:262 already reads this exact raw key pair.)
    """
```

Built on the existing `history/jsonl.py:Event` shape — no new JSONL parser:
- `Event.tool_uses` = `[{id, name, input}]`
- `Event.tool_results` = `[{tool_use_id, content}]`
- Pairing key: `tool_use.id` ↔ `tool_result.tool_use_id`.
- Stable id: `Event.uuid` (already populated by `_classify`, jsonl.py:54).

Implementation:
1. First pass: collect `tool_results` keyed by `tool_use_id` (full-file walk).
2. Second pass: walk events, apply filters (reading `ev.raw` per above),
   attach `tool_uses[i]["result"]` from the map (`None` when no result yet),
   stamp each message with `ev.uuid`. Return the list.

No `since_ts`/incremental path. **Why full-resend, not deltas:** a `since_ts`
delta protocol is incompatible with in-flight back-patch on a stateless server
— a tool_use is emitted once (`result=null`); its result lands on a *later*
event but pairs onto the *earlier* assistant turn, whose `ts_ms` is now below
`since_ts`, so the patched turn never re-sends and the client's "running…"
never resolves. Full-resend sidesteps this entirely (and makes a resume
path-flip a free full-replace). Cost is one full parse + parsed-list payload
per poll, for the *single selected* pane at 2s, on a worker thread — acceptable
for a single-user tool. See Deferred for when to revisit.

`get_session` is rewritten to call this for its `messages` field (dropping its
own `since_ts`-less inline parse, which today omits tool_results entirely —
search.py:239-245). Its public schema gains tool-result content on assistant
turns; `/history`'s `renderMsg` (history.js:361) reads only `t.name`/`t.input`,
so the extra `result`/`uuid` keys are inert there — no guard needed.

### New module: `periscope/turns.py` — stateless resolver

```python
def get_turns_for_pane(cwd: str) -> dict | None:
    # live_transcript_for(cwd) -> newest-mtime JSONL whose recorded cwd matches
    #   (activity.py:211). None -> caller returns {turns: null} (graceful:
    #   history not init / no JSONL / no cwd match).
    # else -> {session_id, jsonl_path, messages: messages_from_jsonl(path)}
```

- Stateless — no module-level cache, nothing to evict at lifespan shutdown.
- `_resuming` window: during `claude --resume`, `live_transcript_for` may
  return the old or new JSONL by mtime. With full-resend the client sees a new
  `jsonl_path` and full-replaces its list — correct by construction, no
  cross-file `since_ts` merge bug. Flag for one manual test; don't engineer
  around it.

## Transport: `GET /api/pane/turns`

New route in `periscope/routes/pane.py` (sibling to `/api/pane`):

```
GET /api/pane/turns?session=<s>&index=<i>
  -> 200 { session_id, jsonl_path, messages: [...] } | { turns: null }
```

- Session/index as **query params** (slash-bearing session names — invariant 6).
- Resolves cwd via `tmux display-message -t <target> -p '#{pane_current_path}'`
  (same shape `/api/pane` already uses), then `get_turns_for_pane(cwd)`.
- `<Detail>` polls this **only while a Claude pane is selected**; every poll
  returns the full message list (no `since_ts`). One in-flight selection →
  bounded load.
- Independent of the `/ws/pane` terminal bridge and of the global `/api/state`
  poll. Errors via `raise HTTPException` per project convention.

**Open:** poll cadence. The modal uses 1.5s for `/api/pane`; the transcript is
less latency-sensitive than a live terminal. Lean **2s**, decide during dogfood.

## Frontend: the segment renderer in `<Detail>`

### Detail-pane content modes

`PaneDetail` (`Detail.jsx:140`) currently renders `<PaneHeader>` + `<Terminal>` +
`<SidePanel>`. Add a **Transcript ⇄ Terminal toggle** in the header:

- **Default for a Claude pane: Transcript.** A non-Claude pane (shell) has no
  transcript → Terminal only, no toggle (until the shell-blocks milestone).
- The toggle is per-pane UI state in a signal (transient — `store.js`), not a
  persisted pref initially.

### Persist-don't-remount (reuse the migration patterns)

- The live `<Terminal>` is **kept mounted, CSS-hidden** when Transcript is
  active — same persist-don't-remount discipline `<Detail>` already uses for
  review iframes. Toggling back to Terminal must **reconnect, not remount**
  (it's keyed on `pid` today; preserve that). Switching to Transcript must not
  tear down the xterm/WS.
- The transcript poll runs only when Transcript is the active mode for a Claude
  pane, and stops on deselect/unmount.

### Segment list rendering (no anchoring, no emulation)

- `<TranscriptView>` reads the polled `messages` into a per-pane signal and
  renders one **segment per turn** + horizontal-rule dividers for
  `kind:"compact"`.
- Turn segment: role tag, relative timestamp, 1-line preview; click to expand
  body + tool calls. **`util.js:relTime` takes epoch *seconds*** (util.js:29,
  every caller passes seconds), but messages carry `ts_ms` — call
  `relTime(m.ts_ms / 1000)`, don't pass `ts_ms` raw (it renders as a ~57-year
  delta). (`/history` uses a separate ms-aware `fmtTime`; we reuse `relTime`.)
- Tool call: name + input at top (Bash `command`, Read `file_path`, Edit
  `old/new`), output below; `result === null` → "running…" spinner.
- Reconciliation: each poll returns the full list; render keyed on `uuid` so
  Preact diffs in place — an in-flight tool call's `result` filling in on a
  later poll updates that segment without remounting it, and expanded state
  (keyed by `uuid` in a signal) survives. A `jsonl_path` change (resume) just
  replaces the list. Merge by `uuid`, never by `ts_ms` (collides).
- Current (newest) turn gets a subtle highlight. Pure CSS.

This is plain Preact reconciliation over a signals-backed list — no imperative
host, no xterm coupling, no buffer math.

## Data flow

```
tmux pane (cwd) ──display-message──► /api/pane/turns ──► get_turns_for_pane(cwd)
                                                              │
                                          periscope/activity.py:live_transcript_for(cwd)
                                                              │  ~/.claude/projects/<enc>/<sid>.jsonl
                                                              ▼
                                          history/search.py: messages_from_jsonl (full file)
                                                              ▼
                                       { session_id, jsonl_path, messages[] }
                                                              ▼
                          <Detail> transcript poll (Claude pane selected, Transcript mode)
                                                              ▼
                            <TranscriptView> — segment list keyed by uuid (signals)
```

## Invariants preserved

- **No `from server import` in `periscope/`** — `turns.py` imports only from
  `periscope.*` / `history.*`.
- **Session/index are query params** (slash-bearing names — invariant 6).
- **No server-side state to leak** — the endpoint is stateless; nothing
  outlives the request, so no lifespan eviction needed.
- **Live terminal fidelity untouched** — the xterm/WS path is unchanged;
  Transcript is a sibling mode that hides (not destroys) the terminal.
- **Graceful degradation** — `turns: null` (history uninitialized, no JSONL,
  no cwd match) → no Transcript toggle, detail renders as today. No log spam.
- **Route errors via `HTTPException`** with real status codes.

## Non-goals

Shell blocks / OSC 133 / per-block emulation (next milestone). Grid-view modal
changes. Editing/branching. Cross-session nav. Persisted annotations. A
dashboard live turn-count badge.

## Deferred (revisit only if profiling says so)

A server-side parse cache (incremental tail-parse) was considered and cut from
v1. It's a real optimization but it reintroduces state and, to stay correct,
its delta protocol **must re-emit any message whose paired `result` changed
since the client's cursor** — not just messages with a newer original `ts_ms`
(the in-flight back-patch trap that motivated full-resend). Only build it if
dogfooding a genuinely large live session shows the full-reparse + full-payload
per 2s poll is a real cost. Measure first.

## Open questions (small — resolve in review / dogfood)

1. Transcript poll cadence (lean 2s).
2. Toggle affordance placement — header button vs. a small segmented control in
   `PaneHeader`. Cosmetic; decide in build.

## Phases (commit-as-you-go on a worktree, :8766)

1. **Server parser + endpoint.** `messages_from_jsonl(path)` (full-file pairing,
   `ev.raw` filters, compact dividers, `uuid` stamping) in `history/search.py`;
   refactor `get_session` to use it. Stateless `periscope/turns.py`
   (`get_turns_for_pane`). `GET /api/pane/turns`. Unit tests: pairing,
   isMeta/sidechain filtering (via `ev.raw`), compact_boundary emission,
   in-flight tool_use (result=None), `uuid` present + stable. Verify via
   `curl '127.0.0.1:8766/api/pane/turns?session=foo&index=0'`.
2. **Transcript view + toggle.** `<TranscriptView>` (segments keyed by `uuid`,
   `relTime(ts_ms/1000)`) + the Transcript⇄Terminal toggle in `PaneDetail`;
   Transcript default for Claude panes; persist-don't-remount the xterm;
   transcript poll lifecycle. Manual-verified in the browser (no frontend test
   suite — project convention).
3. **Polish.** In-flight `result` fill-in via uuid-keyed reconciliation,
   compact dividers, current-turn highlight, tool-call rendering per tool
   (Bash/Read/Edit), `_resuming` path-flip manual test, edge cases from dogfood.
```
