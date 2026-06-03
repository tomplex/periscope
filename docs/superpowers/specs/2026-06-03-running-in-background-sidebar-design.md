# Running-in-background sidebar — design

## Problem

Periscope's split-view Detail sidebar (`src/sidebar/Sidebar.jsx`) shows Linked /
Notes / Files / Activity for the selected pane. It has no view of **what Claude
is currently running in the background** — `run_in_background` shells (dev
servers, watchers, long tests) and dispatched subagents are invisible unless you
read the transcript. The supervisory dashboard should make that legible.

Two secondary problems addressed here:

1. **The Detail sidebar is empty in transcript mode.** `.detail-pane-body` is a
   `grid-template-columns: 1fr 280px`. In terminal mode the term-host is
   `display:contents` (Terminal → col 1, sidebar → col 2). In transcript mode the
   term-host is `display:none`, which removes it from the grid, so the sidebar
   collapses into col 1 (`1fr`) where the absolutely-positioned transcript
   overlay (`.detail-transcript-host`, `right:280px`) covers it — leaving the
   280px slot empty. The sidebar (and the new section) must be visible in **both**
   modes.

2. There's no way to see a subagent's own work — the parent transcript shows only
   the `⚇ Agent` block and its final report.

## Scope (v1)

- **A new "Running" sidebar section** listing:
  - **Background shells** — the authoritatively-running `run_in_background` tasks
    (command + runtime).
  - **Subagents** — split into a **Running** group and a collapsible
    **Complete (N)** group (type + description; running drilling into the
    transcript). Satisfies `docs/transcript-view-todos.md` item 3.
- **All sidebar sections collapsible** (Linked/Notes/Files/Activity/Running) with
  per-section collapsed state persisted in prefs; the sidebar scrolls.
- Fix the transcript-mode-empty sidebar.

### Non-goals

- **Shell output content.** We use the harness tasks-dir to detect *which* shells
  are running and their command/runtime, but do NOT surface the captured stdout
  (a later pass could add an output drill-in like the subagent transcript).
- Background **jobs** (`kind=bg` sessions / `~/.claude/jobs/`) — a separate
  concept, out of scope.

## Data sources (measured)

- **Background shells → the harness tasks-dir + `lsof` (authoritative).** Each
  `run_in_background` task writes to
  `/private/tmp/claude-<uid>/<enc-cwd>/<conv-id>/tasks/<task-id>.output` (regular
  file; subagents are symlinks in the same dir). The task's process holds that
  file open **only while running**, so `lsof` on it is an exact running signal —
  no infra noise (only real backgrounded tasks are here), no
  foreground/background guessing. Measured: `lsof -t bso70m181.output` → live pid
  (running dev server); a finished task's `.output` → no writer. The writer pid
  gives the real command (`ps -o command`) and runtime (`etime`). The only thing
  needing the session JSONL is locating the `tasks/` dir: the `<conv-id>` is
  embedded in every bg-task result path in the JSONL (scan once, cache per
  session). (The earlier process-tree approach was abandoned — it surfaced too
  much plumbing: `ty`, MCP servers, uv-venv pythons.)
- **Subagents → the `subagents/` dir.** `projects/<enc-cwd>/<session-id>/subagents/agent-<id>.meta.json`
  = `{agentType, description}`; `agent-<id>.jsonl` = the subagent's transcript.
  Recent `.jsonl` mtime + parent session `working` ⇒ running (see the running-gate
  note below). **Note:** every subagent event carries `isSidechain: true`, which
  `messages_from_jsonl` currently skips (`history/search.py:242`) — the drill-in
  needs a sidechain-aware parse (see §New route).

## Server design

### New module `periscope/running.py`

Stdlib + `periscope.*` leaf (no `from server import`). Two functions:

```
background_shells(session_id: str) -> list[dict]
    # tasks-dir .output files held open by a live process (lsof) = running.
    # -> [{"pid": int, "cmd": str, "runtime_s": int}]

subagents(session_id: str) -> list[dict]
    # glob projects/<enc>/<sid>/subagents/agent-*.meta.json + jsonl mtime.
    # agent_id is the BARE hex id (filename stem minus the "agent-" prefix).
    # -> [{"agent_id": str, "agent_type": str, "description": str, "running": bool}]
```

`background_shells`:
- **Locate the tasks-dir:** resolve `session_id` → its JSONL (`turns._jsonl_for_session`),
  scan for the first `/private/tmp/claude-…/…/<conv-id>/tasks/` path (regex), and
  cache `session_id → tasks_dir` (the `<conv-id>` is stable per conversation, so
  this is a one-time scan; re-scan only if not yet cached). No bg task ever ⇒ no
  path ⇒ `[]` (nothing to show anyway).
- **List candidates:** regular `*.output` files in the tasks-dir (skip symlinks —
  those are subagents).
- **Running check (one `lsof`):** `lsof -F pn <all .output files>` in a single
  call → parse the `p<pid>`/`n<name>` records into `{output_path: writer_pid}`.
  A file with a live writer = running.
- **For each running task:** `pid` = writer; `cmd` = `ps -o command= -p <pid>`
  (the real process command, trimmed/capped ~80 chars); `runtime_s` = `_etime_to_s`
  of `ps -o etime= -p <pid>`. Build the `{pid, cmd}` and etime lookups from one
  `ps` snapshot. Only running tasks are returned (finished ones have no writer).
- Degrade to `[]` on any error (no tasks-dir, lsof/ps failure).
- Helpers worth keeping/injecting for tests: `_etime_to_s`, and an injectable
  `lsof`/`ps` boundary (pass the parsed `{output_path: pid}` + `{pid: (cmd,etime)}`
  as test params, like `subagents` takes a seeded dir).

`subagents`:
- The subagents dir is `projects/<enc-cwd>/<session-id>/subagents/` — a subdir
  parallel to the session's own `<session-id>.jsonl` file. Resolve by globbing
  `activity._PROJECTS_DIR.glob(f"*/{session_id}/subagents/agent-*.meta.json")`
  (mirrors `turns._jsonl_for_session`'s glob-by-id, robust to cwd encoding). Pair
  each `agent-<id>.meta.json` with its sibling `agent-<id>.jsonl` for the mtime
  check.
- `running`: `.jsonl` mtime within `RUNNING_WINDOW_S` (≈10s) **and**
  `(session_status.session_state_for(session_id) or {}).get("state") == "working"`
  (guard the `None` return; `session_state_for` returns `{"state","status",
  "waiting_for"} | None`). **Coarseness (accepted v1):** the parent-`working` gate
  is per-session, not per-agent — when several agents run in one turn, a
  just-finished one can read "running" until its mtime ages out of the window
  (≤10s, self-corrects); parallel agents all correctly read running.
- Degrade to `[]` on any missing dir / parse error (LGTM-integration philosophy).

Unit-tested in `tests/test_running.py`: `background_shells` against a synthesized
ps-style `pid→ppid` map (inject the `ps` reader or pass the table); `subagents`
against a seeded `projects/<enc>/<sid>/subagents/` fixture dir.

### `/api/pane` additions

`routes/pane.py::pane(session, index, lines)` already resolves `pane_id`. Add:
- resolve `pane_id` → `session_id` via existing `turns.session_id_for_pane`
- return two new keys: `background_shells(session_id)` and `subagents(session_id)`
  (each `[]` when none). Both are session-id-based now — no `pane_pid` lookup.

### New route: subagent transcript

`GET /api/pane/subagent?session=&index=&agent=<agent_id>` in `routes/pane.py`:
- `agent_id` is the **bare hex id**; validate `^[0-9a-f]+$` (reject otherwise — it
  composes into a filename) → 400 on mismatch.
- resolve pane → `session_id`; locate `subagents/agent-<agent_id>.jsonl` under the
  session's project dir via a `turns.subagent_jsonl(session_id, agent_id)` helper
  (globs `*/{session_id}/subagents/agent-{agent_id}.jsonl`).
- return `{messages: messages_from_jsonl(path, include_sidechain=True)}`, or
  `{messages: null}` if absent.
- **`messages_from_jsonl` change:** add an `include_sidechain: bool = False`
  parameter. Subagent events all carry `isSidechain: true`, which the current
  parser drops (`history/search.py:242` skips `isMeta` OR `isSidechain`). With
  `include_sidechain=True`, skip only `isMeta`. Default `False` preserves every
  existing caller's behavior.

## Frontend design

### `RunningSection({ data })` in `Sidebar.jsx`

Modeled on `FilesSection`. Reads `data.background_shells` + `data.subagents`.

- **Returns `null` when both are empty** (section absent when nothing runs).
- Rendered **first** in `Sidebar` (above Linked) — it's the live "now".
- **Background shells** group: shell row `▶ {cmd}` + right-aligned `{runtime}`
  (formatted s/m/h). All entries are running (lsof-confirmed), no dot needed.
- **Subagents** split into two groups:
  - **Running** — `⚇ {agent_type}` + `{description}` + pulsing run-dot, clickable
    (opens the transcript).
  - **Complete (N)** — same rows minus the dot, inside a collapsible group that is
    **collapsed by default** (keeps the long finished-agent list out of the way).
    `subagents()` returns all agents with their `running` flag; the component
    partitions on it.
- **Sub-group labels** ("Background shells" / "Subagents") render **only when
  more than one group is shown**; with a single group the `▶`/`⚇` glyphs
  self-identify.
- New CSS block `.run-*` at the end of `static/styles.css`; pulsing dot reuses
  `transcript-status-pulse`.

### Collapsible sidebar sections (all of them)

Make every `Sidebar` section header (`<h4>`) a collapse toggle, with per-section
state persisted in prefs (mirror the rail's `rail_collapsed` pattern in
`prefs.js`). Approach:
- A `<SidebarSection title key children>` wrapper (or extend the existing
  `<section>`s) that renders a clickable `<h4>` with a chevron (▸/▾) and hides its
  body when collapsed.
- Collapsed state keyed by a stable section id ("linked"/"notes"/"files"/
  "activity"/"running") in a new prefs blob (e.g. `sidebar_collapsed: {id: bool}`),
  read/written via `prefs.js` helpers like the rail's `getRailCollapsed`/
  `setRailCollapsedKey`. Shared by modal + detail sidebars (same component).
- The Complete-subagents group has its own independent collapse (collapsed by
  default), separate from the section-level Running collapse.
- `.detail-side` (and the modal side) get `overflow-y: auto` so a long sidebar
  scrolls instead of overflowing.

### Subagent transcript drill-in

- New signal `subagentView` in `store.js`: `{ session, index, agentId } | null`
  (mirrors `previewPath`).
- A subagent row sets `subagentView.value = {session, index, agentId}`.
- New `SubagentOverlay` (sibling to `PreviewOverlay`, floats over
  `.detail-pane-body`): on a non-null `subagentView`, fetches
  `/api/pane/subagent`, renders the messages, Esc-dismiss via the shared
  `useEscape` LIFO. Re-opening on a new `agentId` re-fetches in place.
- **Stacking:** the transcript host is `z-index:1`, composer `z-index:2`.
  `SubagentOverlay` sits above both (`z-index:5`); `PreviewOverlay` is already
  `z-index:20`, above *it*. A file chip clicked *inside* a subagent
  transcript sets `previewPath` (the shared `TranscriptBody` chips do this), so
  `PreviewOverlay` floats over the open subagent transcript — supported on
  purpose; the `useEscape` LIFO dismisses the preview first, then the subagent
  overlay.
- **Refactor:** extract a presentational `<TranscriptBody messages currentUuid />`
  from `TranscriptView` (the `messages.map → Turn/ToolCall` core, the
  current-turn highlight, the compact divider). `TranscriptView` keeps the poll /
  autoscroll / composer / status-banner and renders `<TranscriptBody>` inside;
  `SubagentOverlay` renders `<TranscriptBody>` with the fetched messages, no
  composer, and `currentUuid={null}` (static history → no live-turn highlight).
  No behavior change to the live view. (A truncated subagent's last in-flight
  tool would show `running…`; harmless — correct when genuinely live.)

### Transcript-mode sidebar fix

`static/styles.css`: pin the sidebar to the second grid column so it keeps the
280px slot even when the term-host is `display:none`:

```
.detail-side { grid-column: 2; }
```

The transcript overlay already stops at `right:280px`, so the sidebar then sits
in the slot left for it — visible in both terminal and transcript mode. Verify
in the browser that the Files/Activity/Running sections render in transcript mode.

## Refresh / performance

No new poll loop — the sidebar's existing 1.5s `/api/pane` poll (`SidePanel`)
carries the new fields. Per poll, for the *selected* pane only: a cached JSONL
scan for the tasks-dir (one-time per session), one `lsof` over the tasks-dir
`.output` files, and one `ps` snapshot for the running writers' cmd/etime. Bounded
and fine. `/api/pane` is only polled for the selected pane, not all panes.

## Caveats

- **`lsof` cost / liveness exactness.** One `lsof` call per selected-pane poll
  (~tens of ms). The writer-open-file signal is exact for running-vs-finished — no
  foreground/background guessing. A task that closes its own stdout while still
  alive (rare) would read as finished; acceptable.
- **Conv-id discovery needs a prior bg task.** The tasks-dir is located by scanning
  the JSONL for a `/tasks/<conv-id>/` path, which only exists once the session has
  launched ≥1 background task. Before that there are no bg shells to show anyway,
  so `[]` is correct.
- **Undocumented internals.** `subagents/*.meta.json` shape and the session-status
  file are Claude Code internals (version-tagged). All reads degrade to empty on a
  shape change; the section simply doesn't render.
- **Subagent dir location.** New subagents are assumed to land under the *current*
  session's `subagents/`. To verify while building: dispatch one and confirm the
  dir (pre-`/clear` subagents live under the old session id; for "running now"
  this is fine).

## Testing

- `tests/test_running.py` — `background_shells` (denylist + descendant walk +
  persistence filter against an injected ps table) and `subagents` (seeded dir,
  running-gate logic).
- `tests/test_search.py` — `messages_from_jsonl(..., include_sidechain=True)`
  returns sidechain events; default still drops them (regression guard for the
  existing callers).
- `tests/routes/test_pane.py` — `/api/pane` carries the new keys; `/api/pane/subagent`
  returns messages for a seeded subagent jsonl (sidechain events present), `null`
  for missing, and 400 for an `agent_id` failing `^[0-9a-f]+$`.
- Frontend per project convention: verify in the browser on the dev instance
  (running shells appear, subagent drill-in opens the transcript, sidebar shows in
  both terminal and transcript mode); rebuild + commit `static/dist/app.js`.
