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

- **At-a-glance list** of running background shells + subagents in a new "Running"
  sidebar section.
- **Subagent transcript drill-in** (clicking a subagent opens its transcript).
  This also satisfies `docs/transcript-view-todos.md` item 3.
- **Running-only.** The process tree only contains live shells; subagents are
  filtered to recently-active. Finished items are not listed.
- Fix the transcript-mode-empty sidebar.

### Non-goals

- **Shell output drill-in.** A background shell's captured stdout lives in the
  harness tasks-dir (`/private/tmp/claude-<uid>/<enc-cwd>/<conversation-id>/tasks/<id>.output`),
  keyed by a conversation-id that isn't cleanly linked from the current
  (post-`/clear`) session. Deferred — the list shows command + runtime only.
- Background **jobs** (`kind=bg` sessions / `~/.claude/jobs/`) — a separate
  concept, out of scope.
- Perfect foreground/background discrimination for shells (see Caveats).

## Data sources (measured)

- **Background shells → the process tree.** A `run_in_background` task is a
  detached, persistent process under the pane's Claude. `ps` descendants of the
  pane's pid give command (args), runtime (etime), and alive=running directly.
  No fragile conversation-id mapping. Confirmed: walking the tree from a live
  Claude pid surfaces tool shells, dev servers, MCP servers, LSP, `uv`/`python`
  workers.
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
background_shells(pane_pid: int) -> list[dict]
    # ps descendants of pane_pid, infra-filtered.
    # -> [{"pid": int, "cmd": str, "runtime_s": int}]

subagents(session_id: str) -> list[dict]
    # glob projects/<enc>/<sid>/subagents/agent-*.meta.json + jsonl mtime.
    # agent_id is the BARE hex id (filename stem minus the "agent-" prefix).
    # -> [{"agent_id": str, "agent_type": str, "description": str, "running": bool}]
```

`background_shells`:
- Build a `pid → (ppid, comm, args, etime)` map from one
  `ps -axo pid=,ppid=,comm=,etime=,args=` call; walk descendants of `pane_pid`.
- **Infra denylist** (drop these `comm`/arg patterns): the `claude` launcher and
  versioned binary (`.../share/claude/versions/...`), `*-mcp` servers, `ty` (and
  other LSPs), `node` running an MCP server, the periscope channel shim,
  `caffeinate`, bare `zsh`/`-zsh` login shells with no command.
- **Persistence filter:** only emit pids seen on the *previous* poll too (a tiny
  `_last_seen_pids` cache in the module, keyed by `target` = `session:index`), so
  a transient foreground tool-bash caught mid-run is dropped. Backgrounded/
  long-running processes persist across polls and survive. **Consequence:** the
  first `/api/pane` poll after selecting a pane (SidePanel remounts per `target`)
  has no prior entry, so shells are empty for one poll (~1.5s warmup) then
  populate. Accepted.
- `cmd` is the trimmed `args` (capped, e.g. 80 chars); `runtime_s` parsed from
  `etime`.

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

`routes/pane.py::pane(session, index, lines)` already resolves `target`. Add:
- resolve `pane_pid` via `tmux display-message -t <target> -p '#{pane_pid}'`
- resolve `pane_id` → `session_id` via existing `turns.session_id_for_pane`
- return two new keys: `background_shells` and `subagents` (each `[]` when none).

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
- Shell row: `▶ {cmd}` + right-aligned `{runtime}` (formatted via `relTime`-style
  s/m/h).
- Subagent row: `⚇ {agent_type}` + `{description}` + a pulsing run-dot; the row is
  clickable (opens the transcript, below).
- **Sub-labels** ("Background shells" / "Subagents") render **only when both**
  kinds are non-empty; with one kind the `▶`/`⚇` glyphs self-identify.
- New CSS block `.run-*` at the end of `static/styles.css`; the pulsing dot reuses
  the `transcript-status-pulse` keyframes.

### Subagent transcript drill-in

- New signal `subagentView` in `store.js`: `{ session, index, agentId } | null`
  (mirrors `previewPath`).
- A subagent row sets `subagentView.value = {session, index, agentId}`.
- New `SubagentOverlay` (sibling to `PreviewOverlay`, floats over
  `.detail-pane-body`): on a non-null `subagentView`, fetches
  `/api/pane/subagent`, renders the messages, Esc-dismiss via the shared
  `useEscape` LIFO. Re-opening on a new `agentId` re-fetches in place.
- **Stacking:** the transcript host is `z-index:1`, composer `z-index:2`.
  `SubagentOverlay` sits above both (e.g. `z-index:5`); `PreviewOverlay` sits
  above *it* (e.g. `z-index:10`). A file chip clicked *inside* a subagent
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
carries the new fields. One `ps -axo` call per `/api/pane` request; the
persistence filter needs the per-pane `_last_seen_pids` cache (bounded by pane
count, harmless staleness for closed panes). Subagent globbing is a few small
files. `/api/pane` is only polled for the *selected* pane, so this is one pane's
cost per 1.5s, not all panes.

## Caveats

- **Foreground leak.** `ps` can't perfectly distinguish a backgrounded shell from
  a long-running foreground tool command. The persistence filter (alive across
  two polls) drops transient ones; a foreground command that runs >1 poll will
  appear — which is arguably useful ("Claude is running a slow command") and is an
  accepted v1 tradeoff (confirmed with Tom).
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
