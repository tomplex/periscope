# Omnibox commander — design

## Summary

Turn the omnibox into a free-text command surface. The user types a
plaintext instruction ("create a workspace for the attribute config refactor
and add two claudes: one to X and another to Y", "create a worktree for X")
and a single persistent, hidden Claude Code pane — the **commander** — acts on
it, calling periscope's tools to create workspaces, open worktrees, and spawn
worker panes. The commander's work streams back into the omnibox, which becomes
a live console for the duration of the command.

The commander is the **repurposed first mate**: the existing first-mate pane +
marker + spawn plumbing stay; the proactive heartbeat/divergence machinery that
"didn't pan out" is stripped. First mate was a fleet *observer* that periscope
pushed digests to; the commander is a fleet *actuator* that the user sends
commands to. Send-only — no autonomous tick.

## Goals

- Type a natural-language command in the omnibox and have it executed.
- Cover the three command shapes the user named: create-workspace-and-spawn-N,
  create-a-worktree, and (free) the existing open/spawn surface.
- Reuse periscope's existing machinery (pane spawn, input delivery, transcript
  rendering, channel MCP tools) rather than building a parallel agent stack.
- Run free under the user's Claude subscription (the commander is a normal
  Claude pane in the user's tmux), not metered API billing.
- Leave room to grow past periscope-actions into "interpret niche instructions
  across my codebases" — the commander has read access to the user's repos.

## Non-goals (v1)

- No plan-preview/confirm step. Execution is immediate (the console + per-spawn
  latency are the visibility/abort affordance).
- No ask-back disambiguation. The commander makes a best guess; the console
  makes a wrong guess visible.
- No concurrent commands. Commands serialize on the one persistent session; the
  omnibox disables send while a turn is in flight.
- The commander does **not** edit files or run shell commands itself (see
  Tooling). It orchestrates; worker panes it spawns do the editing.
- No new captain's-log UI. The table is retained as durable scratch; nothing
  renders it.

## Decisions (locked in brainstorming)

| Decision | Choice |
|---|---|
| Engine | A managed persistent Claude Code **tmux pane**, not an in-process Agent SDK call. |
| Execution | Immediate — no confirm step. |
| Routing | An always-present "⚡ run: <text>" row in the omnibox; selecting it fires the agent. The deterministic `classify()` palette is unchanged above it. |
| Progress UI | The omnibox becomes a **console** that tails the commander's transcript until it goes idle. |
| Session model | **One persistent** session (remembers context across commands). |
| Commander scope | **Orchestrator**: periscope MCP tools + read-only code (`Read`/`Grep`/`Glob`); **no** `Bash`/`Edit`/`Write`. |
| Visibility | Hidden from the rail. The console is the window into it. |
| Model | Sonnet, pinned via the spawn command. |

## Why a managed pane (not the Agent SDK)

The Agent SDK was the first candidate. The managed-pane approach won on three
counts:

1. **Reuses what periscope already is.** Periscope already spawns Claude panes
   (`first_mate._spawn_first_mate`, `worktree_spawn._layout_two_window`),
   delivers input to panes (`/api/send` → `tmux.deliver_input`, paste-buffer +
   Enter), and renders a pane's conversation as a structured transcript
   (`turns.get_turns_for_pane`, `GET /api/pane/turns`). A commander pane is just
   a pane.
2. **The pane-scoping "problem" inverts into a feature.** The channel MCP tools
   key off the caller's `$TMUX_PANE`. The commander, being a real pane with a
   pane id, uses them with correct context — no refactor to make them
   pane-independent.
3. **Auth is simpler.** A pane in the user's interactive tmux inherits the GUI
   login session, so it is subscription-authed exactly like every Claude pane
   the user already runs — no `CLAUDE_CODE_OAUTH_TOKEN` plist token, no
   env-scrubbing. (Dependency: this rides the user's tmux server, the same
   default socket periscope-under-launchd attaches to. To verify in planning.)

The cost is that streaming is transcript-tailing rather than structured SDK
events, and that the commander has the full blast radius of a Claude Code
instance — which the Tooling section bounds.

## Architecture

```
omnibox "⚡ run: <text>"
   │  POST /api/command {text}
   ▼
routes/command.py
   │  1. ensure_commander()      ← boot-spawn + lazy heal (respawn if dead)
   │  2. deliver text as a turn  ← tmux.deliver_input (paste-buffer + Enter)
   │  3. return {session, index} ← the commander pane's address
   ▼
commander pane  (bridge:commander — a real Claude Code, Sonnet)
   │  reads code to resolve refs (Grep/Glob/Read)
   │  calls channel MCP tools:
   │    create_workspace · open · spawn_claude · list_claudes · peek · send_to …
   ▼
periscope state mutates → rail updates on next /api/state poll
   │
   └─ omnibox console polls GET /api/pane/turns?session=&index= until idle
```

### Data flow — the worked example

*"create a workspace for the attribute config refactor and add two claudes: one
to X and another to Y"*

1. Omnibox `POST /api/command {text}`.
2. `ensure_commander()` confirms the marker's pane is live (respawns if not),
   then `deliver_input(commander_target, text)`.
3. The commander Greps/Reads to resolve "attribute config refactor" → a repo,
   calls `create_workspace(name="attribute config refactor", base_repo=<repo>)`
   → `workspace_id`, then `spawn_claude(prompt=X, workspace_id, cwd=<repo>)` and
   `spawn_claude(prompt=Y, workspace_id, cwd=<repo>)`.
4. Each tool mutates periscope; the workspace and two panes materialize and
   appear in the rail on the next 3s `/api/state` poll.
5. The omnibox console polls `/api/pane/turns` for the commander pane, rendering
   each tool call and narration line until the commander is idle, then settles.

## Components

### `periscope/first_mate.py` → commander core

Rename the module and its identifiers to `commander` (via refactor-mcp, LSP-backed)
for clarity; the marker table, session/window constants, and spawn function come
along. (If churn is a concern, the names can stay; the behavior change is what
matters. Default: rename.)

**Strip** (the proactive "tick with ongoing work"):
- `fleet_diverged`, `heartbeat_decide`, `_render_delta`, `Push`, `_LAST_SENT`.
- `supervisor_pass` as a *tick* entry point (the ensure logic survives as
  `ensure_commander`, called at boot + on send rather than every 30s).
- `first_mate_disabled()` sentinel kill-switch (a manual feature with no
  autonomous respawn to suppress; a stop is "kill the pane").

**Keep / repurpose:**
- `_spawn_first_mate` → `_spawn_commander`: same sequence (ensure session, open
  window, `claude_exec()` + `--append-system-prompt "$(cat file)"`,
  `dismiss_dev_channels_consent_bg`, `stamp_new_window`, read pane id, set
  marker). Add `--model sonnet` and the read-only tool lockdown to the launch
  command (see Tooling). Still `is_prod()`-gated.
- `build_fleet_digest` + `assemble_pane_views` + `_curate_pane`: kept to back an
  **on-demand** `fleet_digest` tool (built fresh per call, since the push that
  populated `_LAST_SENT` is gone).
- `ROLE_PROMPT`: rewritten (see Role prompt).
- Session/window constants (`bridge` / `commander`), marker accessors.

`register_bridge_project` is **removed from the rail-visibility path** — the
commander is hidden (see Visibility). The function may be deleted or repurposed
to *exclude* rather than register.

### `periscope/activity.py` — strip the tick, keep the marker

- Remove the first-mate branches from `_worker_tick` (the `supervisor_pass`
  call, the `assemble_pane_views`/`build_fleet_digest`/`heartbeat_decide`
  block, and the stashed `_fm_push`) and `_emit_pending_first_mate`.
- Keep the `first_mate` singleton marker table and `set/get/clear_first_mate`
  (renamed with the module). Keep `captain_log` + `append_captain_log` /
  `recent_captain_log` (durable scratch, unused by UI).

### `periscope/channels.py` — re-gate, drop the interrupt, add actuators

- **Drop** the `need_human` → `_schedule_first_mate_emit` interrupt hook in
  `_do_notify_tool` (the commander is not an autonomous listener).
- **Re-gate**: `_require_first_mate` → `_require_commander` (same singleton-marker
  check). Continues to guard `captains_log_read/append` and `fleet_digest`.
- **Rewire** `_do_fleet_digest_tool` to build a fresh digest on call
  (`assemble_pane_views` + `build_fleet_digest`) instead of returning the now-
  unpopulated `_LAST_SENT`.
- **Add two MCP tools** (today these capabilities are HTTP-only):
  - `create_workspace(name, base_repo?)` → `workspaces.create_workspace`,
    returns the workspace id.
  - `open(path? | repo+branch? | repo+pr?)` → `open_ops.open_target` over the
    existing `PathTarget`/`BranchTarget`/`PRTarget` descriptors (the same dispatch
    `routes/open.py` does). `repo+branch` already creates the worktree if absent,
    so "create a worktree for X" is `open(repo, branch)` — no separate tool.

  These are general channel tools (any pane could use them); they are not
  commander-gated. The commander reaches them like any other pane.

The commander **inherits the entire existing channel toolset** by virtue of
being a pane: `spawn_claude`, `send_to`, `list_claudes`, `peek`, `report`,
`terminate`, `resume_session`, `search_history`, `get_history_session`,
`notify`, `link_pr`, `link_linear`, `open_document`. No new read-state tool is
needed — `list_claudes` + `peek` + the on-demand `fleet_digest` cover it.

### `periscope/routes/command.py` — new route

- `POST /api/command {text}`: `ensure_commander()`, then
  `deliver_input(commander_target, text)`; returns the commander pane's
  `{session, index}` so the client knows which transcript to tail. `409`/`503`
  if the commander can't be ensured (e.g. not prod — see Constraints).
- The console feed reuses the existing `GET /api/pane/turns?session=&index=` —
  no new transcript endpoint.

### `periscope/app.py` — boot-spawn instead of supervise

- Replace the worker-tick supervisor with a prod-gated **boot-spawn** of the
  commander in the lifespan (`ensure_commander()` once at startup). Lazy heal on
  `/api/command` covers a mid-session death.

### `periscope/routes/state.py` — hide the commander

Unregistered sessions fold into the bottom-pinned "dev" `MAIN_KEY` group, so
they are **still visible** by default. To honor "manages but doesn't display,"
filter the commander session out of `windows` right after `list_windows()` in
the `/api/state` handler (a single exclusion on `FIRST_MATE_SESSION`/its window).
This is a real task, not free — note it for the reviewer.

### Frontend — `static/src/open/classify.js` + `overlays/OpenOmnibox.jsx`

- `classify.js`: always append a synthetic `{ kind: "command", label: "⚡ run:
  <query>", text: query }` card (pinned last) whenever the query is non-empty.
  Add `command` to `KIND_META`.
- `OpenOmnibox.jsx`: picking the `command` card switches the card into **console
  mode** — `POST /api/command {text}`, then poll `GET /api/pane/turns` for the
  returned pane every ~1s, rendering new turns, until the transcript stops
  growing (idle). Esc dismisses the console (the command keeps running
  server-side). Disable the input/send while a turn is in flight ("commander
  busy").

## Role prompt

Rewrite `ROLE_PROMPT` from observer to orchestrator. Shape (not final wording):

- You are periscope's commander. The user sends you commands from the omnibox;
  act on them immediately with your tools.
- You orchestrate, you don't edit. To do work in a repo, **spawn a worker**
  (`spawn_claude`) with a clear first-message prompt and the right `cwd` /
  `workspace_id`; the worker has full tools. You have read-only code access
  (`Read`/`Grep`/`Glob`) to understand and route — resolve fuzzy references
  ("the attribute config refactor" → which repo/dir) before acting.
- Tools: `create_workspace`, `open` (path/branch/pr — `open(repo, branch)`
  creates a worktree), `spawn_claude`, `list_claudes`, `peek`, `send_to`,
  `fleet_digest`, the captain's log.
- Best-guess and proceed; narrate what you did concisely so the console reads
  cleanly. Keep the absolute prohibitions (never merge an fdy PR, never
  force-push, never prod-touching actions).

Delivered via the existing file + `--append-system-prompt "$(cat …)"` path
(send-keys strips newlines — CLAUDE.md note 5).

## Tooling lockdown

Two independent tool layers:

- **Claude built-in tools** — locked at launch to `Read`, `Grep`, `Glob`
  (allow-list); `Bash`, `Edit`, `Write` disallowed. The mechanism is the
  `claude` launch flags (e.g. `--allowedTools` / `--disallowedTools`) appended
  to `claude_exec()` for the commander spawn only. Exact flag spelling to confirm
  in planning.
- **Periscope MCP tools** — all channel tools are available to the commander
  (it's a pane). This is the actuator surface; it is intentionally broad
  (including `spawn_claude`, `send_to`, `terminate`). The lockdown above is about
  *the commander not doing codebase work itself*, not about restricting periscope
  actions.

## Error handling

- **Dead commander on send** → `ensure_commander()` respawns before delivering.
- **Commander can't start** (e.g. not authed) → its failure surfaces as the
  pane's terminal output; the console shows the transcript (or its absence) and
  the route returns a clear error if no marker can be set.
- **Tool failure inside the commander** → surfaces in its transcript; the
  commander decides whether to continue or stop. No half-spawn is hidden.
- **A turn already in flight** → the omnibox disables send and shows "commander
  busy"; the user waits or Escs. (Delivering input mid-turn would interleave.)

## Constraints

- **Prod-only.** Channels bind the MCP socket only in prod (dev periscope on
  :8766 doesn't), and `_spawn_commander` is `is_prod()`-gated. So the end-to-end
  send→act loop only works against prod (:8765). The omnibox *UI* (the run row,
  console rendering) is dev-iterable against stubbed/poll data; full verification
  is in the browser against prod. `POST /api/command` returns a clear error in
  dev.
- **One tmux server dependency.** Subscription auth holds only if the commander
  pane lives in the user's interactive tmux server (the default socket). Confirm
  empirically in planning.

## Testing

- **Pure functions kept** (`build_fleet_digest`, `_curate_pane`, and
  `assemble_pane_views`'s pure parts) keep their existing unit tests.
- **New MCP tools** `create_workspace` / `open` get handler tests with stubbed
  `workspaces.create_workspace` / `open_ops.open_target` (mirrors how the open
  route + open_ops are tested; the real-tmux integration tests in
  `tests/test_open_ops.py` already cover `open_target`).
- **Commander spawn** adapts the existing real-tmux test
  (`tests/test_first_mate_spawn.py`) to the renamed `_spawn_commander` (asserts
  spawn → marker; the respawn-loop assertions for the supervisor are dropped).
- **`/api/command` route** test: `ensure_commander` + `deliver_input` mocked;
  assert it delivers the text to the marked pane and returns its address;
  asserts the dev/no-marker error path.
- **Rail exclusion**: a `tests/routes/test_state.py`-level assertion that the
  commander session is absent from `/api/state` windows.
- **Send + transcript-tail end-to-end**: verified in the browser (per the
  project's UI-testing norm — a real Claude is a poor unit-test oracle).
- **Delete** the heartbeat/divergence/supervisor-respawn/interrupt tests in
  `tests/test_first_mate.py`; keep the marker tests in `tests/test_activity.py`
  (renamed).

## Open questions for review

1. **Rail exclusion point.** Is filtering `FIRST_MATE_SESSION` out of
   `/api/state` `windows` the right seam, or should the exclusion live in
   `list_windows()` / `build_window_view` so other consumers (focus tracking,
   pane→session mapping) also skip it? Filtering only in the state route leaves
   the commander visible to the narrator and other workers — is that desired
   (so the commander could still be `peek`'d) or should it be globally hidden?
2. **`fleet_digest` retention.** Worth keeping at all, given `list_claudes` +
   `peek` exist? Dropping it removes `build_fleet_digest`/`assemble_pane_views`
   entirely (more deletion). Keeping it gives the commander the budget snapshot
   in one call.
3. **Console idle detection.** "Transcript stopped growing" is heuristic. Is a
   fixed quiet-period (e.g. no new turn for N seconds) acceptable for v1, or do
   we need a firmer "commander finished" signal?
4. **Naming.** `first_mate` → `commander` rename (refactor-mcp) vs. keep the
   internal name and only repurpose behavior. Default is rename.
