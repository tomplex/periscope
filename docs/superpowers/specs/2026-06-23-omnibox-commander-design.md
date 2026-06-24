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
   delivers input to panes (`/api/send` → `send._send_to_target`, paste-buffer +
   Enter with the bracketed-paste delay), and renders a pane's conversation as a
   structured transcript (`turns.get_turns_for_pane`, `GET /api/pane/turns`). A
   commander pane is just a pane.
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
   │  1. ensure_commander()         ← boot-spawn + lazy heal (respawn if dead), single-flight locked
   │  2. _send_to_target(paste=text, keys=["Enter"])  ← send.py paste-buffer + 100ms + Enter
   │  3. return {session, index}     ← the commander pane's address
   ▼
commander pane  (bridge:commander — a real Claude Code, Sonnet)
   │  reads code to resolve refs (Grep/Glob/Read)
   │  calls channel MCP tools:
   │    create_workspace · open · spawn_claude(workspace="new") · list_claudes · peek · send_to …
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
   then `send._send_to_target(commander_target, paste=text, keys=["Enter"])`
   delivers the command as a submitted turn.
3. The commander Greps/Reads to resolve "attribute config refactor" → a repo,
   calls `create_workspace(name="attribute config refactor", base_repo=<repo>)`
   → `workspace_id`, then `spawn_claude(prompt=X, workspace="new", workspace_id,
   cwd=<repo>)` and the same for Y.
4. Each tool mutates periscope; the workspace and two panes materialize and
   appear in the rail on the next 3s `/api/state` poll.
5. The omnibox console polls `/api/pane/turns` for the commander pane, rendering
   each tool call and narration line until the commander is idle, then settles.

## Components

### `periscope/first_mate.py` → commander core

Rename the module and its identifiers to `commander` (via refactor-mcp,
LSP-backed) for clarity; the marker table, session/window constants, and spawn
function come along. **The rename must update `narrator.py`** (see below), which
imports `FIRST_MATE_SESSION`/`FIRST_MATE_WINDOW` and calls `get_first_mate`. (If
churn is a concern the names can stay; behavior change is what matters. Default:
rename.)

**Strip** (the proactive "tick with ongoing work"):
- `fleet_diverged`, `heartbeat_decide`, `_render_delta`, `Push`, `_LAST_SENT`.
- `build_fleet_digest`, `assemble_pane_views`, `_curate_pane` — these existed
  only to feed digests/the `fleet_digest` tool, which is dropped (see channels).
- `supervisor_pass` as a *tick* entry point (the ensure logic survives as
  `ensure_commander`, called at boot + on send rather than every 30s).
- `first_mate_disabled()` sentinel kill-switch (a manual feature with no
  autonomous respawn to suppress; a stop is "kill the pane").
- `register_bridge_project` — **deleted**, not repurposed (there is no
  "exclude project" concept in `projects.py`; hiding is "don't register +
  filter", see Visibility). Its test is deleted too.

**Keep / repurpose:**
- `_spawn_first_mate` → `_spawn_commander`: same sequence (ensure session, open
  window, `claude_exec()` + `--append-system-prompt "$(cat file)"`,
  `dismiss_dev_channels_consent_bg`, `stamp_new_window`, read pane id, set
  marker). Add `--model sonnet` and the read-only tool lockdown flags to the
  launch command (see Tooling). Still `is_prod()`-gated.
- `ROLE_PROMPT`: rewritten (see Role prompt).
- Session/window constants (`bridge` / `commander`), marker accessors.

New: `ensure_commander()` — `supervisor_pass`'s "marker live? else spawn" logic,
wrapped in a **single-flight lock** (an `asyncio.Lock` or a threading lock) so a
lifespan boot-spawn and a racing first `/api/command` can't both spawn during the
multi-hundred-ms window before the marker is set.

### `periscope/narrator.py` — stop narrating the commander

`narrator._is_first_mate` (and its import of the first-mate constants) must be
updated for the rename. The commander is hidden, so the narrator should **skip
it entirely** in its per-pane tick rather than only suppressing its rename —
otherwise periscope pays a Haiku call every 30s to generate a status line for a
pane no one sees. (Consequence: `peek` on the commander shows no narrator
status line — acceptable for a hidden orchestrator.)

### `periscope/activity.py` — strip the tick, keep the marker

- Remove the first-mate branches from `_worker_tick` (the `supervisor_pass`
  call, the `assemble_pane_views`/`build_fleet_digest`/`heartbeat_decide`
  block, and the stashed `_fm_push`) and delete `_emit_pending_first_mate`.
- Keep the `first_mate` singleton marker table and `set/get/clear_first_mate`
  (renamed with the module). Keep `captain_log` + `append_captain_log` /
  `recent_captain_log` (durable scratch, unused by UI).

### `periscope/channels.py` — re-gate, drop the interrupt, add actuators

- **Drop** the `need_human` → `_schedule_first_mate_emit` interrupt hook in
  `_do_notify_tool`, and delete `_schedule_first_mate_emit` (its only caller is
  that hook).
- **Drop** `fleet_digest` (`_do_fleet_digest_tool`) and its registration — it
  read `_LAST_SENT`, which is gone, and rebuilding it on-demand would mean
  running the worker's blocking capture loop inside the async tool handler.
  `list_claudes` + `peek` already give the commander read-state.
- **Re-gate**: `_require_first_mate` → `_require_commander` (same singleton-marker
  check). Continues to guard `captains_log_read/append`.
- **Add three MCP tools** (today these capabilities are HTTP-only):
  - `create_workspace(name, base_repo?)` → `workspaces.create_workspace(name=…,
    base_repo=…)`, returns the new workspace's `id`.
  - `open(path? | repo+branch? | repo+pr?)` → `open_ops.open_target` over the
    existing `PathTarget`/`BranchTarget`/`PRTarget` descriptors (the same dispatch
    `routes/open.py` does). `open_target` is HTTP-free (its docstring says so) and
    raises plain `ValueError`; the tool catches it into `{"ok": False, "error": …}`
    and ignores `OpenResult.ui`. `repo+branch` already creates the worktree if
    absent, so "create a worktree for X" is `open(repo, branch)` — no separate
    tool.
  - `catalog()` → `open_ops.build_catalog()` (HTTP-free), returning
    `{repos:[{repo,label,default_branch,branches}], worktrees:[{path,repo,branch,is_main}]}`.
    This is the commander's view of *dormant* repos + worktrees — `list_claudes`
    only shows live sessions. It grounds placement guesses (see Placement).

  These are general channel tools (any pane could use them); not commander-gated.

- **`spawn_claude` is cwd-anchored for the commander.** `_do_spawn_claude_tool`
  defaults `workspace="same"`, deriving the session from the caller — which for
  the commander is its *own hidden session*, so a forgotten arg would misfile the
  worker there (invisible, nested under the commander). Change the handler so that
  **when the caller pane is the commander, placement derives from `cwd` (or an
  explicit `session`), never the caller's session**: run `cwd` through
  `resolve_worktree_session` (the `workspace="new"` path) so the worker lands in
  the cwd's project/worktree session — a window in the main checkout when
  `cwd=<repo root>`, the worktree's session when `cwd=<worktree path>`. The
  commander's own session is never a spawn target. (This subsumes the cruder
  "always new" guard: "new" is cwd-anchored, and which cwd the commander picks
  *is* the placement decision — see Placement.)

The commander **inherits the rest of the existing channel toolset** by virtue of
being a pane: `spawn_claude`, `send_to`, `list_claudes`, `peek`, `report`,
`terminate`, `resume_session`, `search_history`, `get_history_session`,
`notify`, `link_pr`, `link_linear`, `open_document`.

### `periscope/routes/command.py` — new route

- `POST /api/command {text}`: `await ensure_commander()`, then
  `send._send_to_target(commander_target, paste=text, keys=["Enter"])`; returns
  the commander pane's `{session, index}` so the client knows which transcript to
  tail. Returns a clear error (`503`) when the commander can't be ensured —
  notably in dev, where it can't spawn (see Constraints).
- The console feed reuses the existing `GET /api/pane/turns?session=&index=` —
  no new transcript endpoint.

### `periscope/app.py` — boot-spawn instead of supervise

- Replace the worker-tick supervisor with a prod-gated **boot-spawn** of the
  commander in the lifespan (`await ensure_commander()` once at startup). Remove
  the `register_bridge_project()` call. Lazy heal on `/api/command` covers a
  mid-session death.

### Visibility — hide the commander from the rail

Three coupled changes (they must land together):

1. **Stop registering** the bridge session as a project (delete the
   `register_bridge_project` call + function). Today that registration is what
   makes the commander a visible rail group.
2. **Clean the stale project row.** A prior prod run persisted a `bridge`
   project in `state.json`; a one-shot migration removes it (otherwise it stays a
   visible group regardless of window filtering).
3. **Filter the window from the `/api/state` payload at the end.** Do NOT filter
   right after `list_windows()` — that raw list feeds `update_focus_from_windows`,
   `_attach_git_then_resolve_pids` (the commander still needs a stamped pid for
   tool resolution), and `_channel_gc` (which would garbage-collect the
   commander's own channel/alert state every 3s poll). Exclude the commander row
   from the final `result` list immediately before `return`.

`list_claudes`/`peek` read tmux directly, so the commander stays reachable to
those tools — only the dashboard rail hides it.

### Frontend — `static/src/open/classify.js` + `overlays/OpenOmnibox.jsx`

- `classify.js`: append a synthetic `{ kind: "command", label: "⚡ run: <query>",
  text: query }` card (pinned last) whenever the query is non-empty. **Add
  `command` to `KIND_META`** — `OpenOmnibox` indexes `KIND_META[c.kind]`
  unconditionally, so a missing key crashes the render. (A card with no
  `descriptor` is not novel — `pr`/`worktree`/`workspace` cards already lack one.)
- `OpenOmnibox.jsx`: this adds a **fourth render branch** alongside `Palette` and
  the two `drill` branches, and `pick()` must get an explicit
  `if (card.kind === "command")` arm — otherwise it falls through to
  `setDrill({card})` and renders nothing. Console mode = `POST /api/command
  {text}`, then poll `GET /api/pane/turns` for the returned pane every ~1s,
  rendering new turns until the transcript stops growing (idle). The Esc handling
  needs a tweak: Esc dismisses the console but leaves the command running
  server-side (today `useEscape` closes the whole omnibox). Disable input/send
  while a turn is in flight ("commander busy").

## Placement

Where a spawned worker lands — the **main project checkout**, a **fresh
worktree**, or an **existing project/worktree** — is a per-command judgment the
commander makes from the request + current context. The user specifies when they
can; otherwise the commander best-guesses. Each placement is just a choice of
`cwd` (and whether to create a worktree first):

| Placement | How |
|---|---|
| Main project checkout | `spawn_claude(cwd=<repo root>)` → a window in the project's main session. |
| Fresh worktree | `open(repo, branch=<new>)` (creates the worktree + its own rail item), then spawn into it — or `spawn_claude(cwd=<worktree path>)`. |
| Existing project / worktree | `spawn_claude(cwd=<that dir>)`, or `open(path=<that dir>)` to focus it. |

**Inference heuristics** (encoded in the role prompt):
- Signals → worktree: a PR, a refactor, "try/experiment", risky or branch-y work,
  anything the user wouldn't want touching the main checkout.
- Signals → main checkout: a quick edit, a question, "look at", read-mostly work.
- "in <project/repo>" → that project, in its main checkout unless the task says
  otherwise.
- **Tiebreak when genuinely ambiguous → fresh worktree.** A stray worktree is
  cheap to discard; polluting the main checkout isn't. No ask-back — best-guess
  and proceed (the console shows the choice; the user corrects if wrong).

The commander uses `catalog()` (dormant repos + worktrees) and `list_claudes`
(live sessions) + `Grep`/`Read` to resolve which repo and whether a suitable
worktree already exists before choosing.

## Role prompt

Rewrite `ROLE_PROMPT` from observer to orchestrator. Shape (not final wording):

- You are periscope's commander. The user sends you commands from the omnibox;
  act on them immediately with your tools.
- You orchestrate, you don't edit. To do work in a repo, **spawn a worker**
  (`spawn_claude` with an explicit `cwd`) with a clear first-message prompt and
  the right `workspace_id`; the worker has full tools. You have read-only code
  access (`Read`/`Grep`/`Glob`) to understand and route — resolve fuzzy references
  ("the attribute config refactor" → which repo/dir) before acting.
- **Choose placement** (main checkout / worktree / existing project) per the
  Placement heuristics: pick the worker's `cwd` accordingly, creating a worktree
  first with `open(repo, branch)` when the task wants isolation; tiebreak to a
  fresh worktree when ambiguous. The user specifies when they can — honor it.
- Tools: `catalog` (repos + worktrees), `create_workspace`, `open` (path/branch/pr
  — `open(repo, branch)` creates a worktree), `spawn_claude`, `list_claudes`,
  `peek`, `send_to`, the captain's log.
- Best-guess and proceed; narrate what you did concisely so the console reads
  cleanly. Keep the absolute prohibitions (never merge an fdy PR, never
  force-push, never prod-touching actions).

Delivered via the existing file + `--append-system-prompt "$(cat …)"` path
(send-keys strips newlines — CLAUDE.md note 5).

## Tooling lockdown

Two independent tool layers:

- **Claude built-in tools** — locked at launch to `Read`, `Grep`, `Glob`
  (allow-list); `Bash`, `Edit`, `Write` disallowed. The mechanism is `claude`
  launch flags (e.g. `--allowedTools` / `--disallowedTools`) appended to
  `claude_exec()` for the commander spawn only. Exact flag spelling to confirm in
  planning.
- **Periscope MCP tools** — all channel tools are available to the commander
  (it's a pane). This is the actuator surface; it is intentionally broad
  (including `spawn_claude`, `send_to`, `terminate`). The lockdown above is about
  *the commander not doing codebase work itself*, not about restricting periscope
  actions.

## Error handling

- **Dead commander on send** → `ensure_commander()` respawns before delivering.
- **Concurrent ensure** → the single-flight lock serializes boot-spawn and a
  racing first command so they can't double-spawn.
- **Commander can't start** (e.g. not authed, or not prod) → the route returns a
  clear `503`; any partial pane failure surfaces as the pane's terminal output in
  the console.
- **Tool failure inside the commander** → surfaces in its transcript; the
  commander decides whether to continue or stop. No half-spawn is hidden.
- **A turn already in flight** → the omnibox disables send and shows "commander
  busy"; the user waits or Escs. (Delivering input mid-turn would interleave.)

## Constraints

- **Prod-only.** Channels bind the MCP socket only in prod (`is_prod()` =
  `PORT==8765 and not PERISCOPE_DEV`); dev periscope on :8766 doesn't bind it, and
  `_spawn_commander` is `is_prod()`-gated. So the end-to-end send→act loop only
  works against prod (:8765); `POST /api/command` returns `503` in dev. The
  console feed (`/api/pane/turns`) reads real session JSONL — in dev there is no
  commander pane, so the console renders empty; verifying console *rendering* in
  dev requires pointing it at another live Claude pane's turns or a fixture.
- **One tmux server dependency.** Subscription auth holds only if the commander
  pane lives in the user's interactive tmux server (the default socket). Confirm
  empirically in planning.

## Testing

- **New MCP tools** `create_workspace` / `open` / `catalog` get handler tests with
  stubbed `workspaces.create_workspace` / `open_ops.open_target` /
  `open_ops.build_catalog` (the real-tmux integration tests in
  `tests/test_open_ops.py` already cover `open_target`).
- **`spawn_claude` commander cwd-anchoring**: a test that a spawn whose caller is
  the marked commander pane derives its session from `cwd` (via
  `resolve_worktree_session`), never the commander's own caller session — for
  `cwd=<repo root>` and `cwd=<worktree path>`.
- **Commander spawn** adapts the existing real-tmux test
  (`tests/test_first_mate_spawn.py`) to the renamed `_spawn_commander` (asserts
  spawn → marker; the supervisor respawn-loop assertions are dropped).
- **`ensure_commander` single-flight**: a test that two concurrent calls spawn
  once (lock holds).
- **`/api/command` route** test: `ensure_commander` + `_send_to_target` mocked;
  assert it delivers `paste=text, keys=["Enter"]` to the marked pane and returns
  its address; assert the dev/no-marker `503` path.
- **Rail exclusion**: a `tests/routes/test_state.py` assertion that the commander
  session is absent from the final `/api/state` windows, *and* that its channel
  state is not GC'd (the exclusion is post-`_channel_gc`).
- **Send + transcript-tail end-to-end**: verified in the browser (per the
  project's UI-testing norm — a real Claude is a poor unit-test oracle).
- **Delete** the heartbeat/divergence/supervisor-respawn/interrupt tests and the
  `register_bridge_project` test in `tests/test_first_mate.py`; keep the marker
  tests in `tests/test_activity.py` (renamed). Update `narrator` tests for the
  skip-commander behavior.

## Resolved review questions

1. **Rail exclusion point** → exclude from the final `/api/state` `result` (after
   `_channel_gc` + pid attach), delete `register_bridge_project`, migrate away the
   stale `bridge` project row, and skip the commander in the narrator tick. The
   commander stays reachable to `list_claudes`/`peek` (tmux-direct), hidden only
   from the rail and the narrator.
2. **`fleet_digest` retention** → dropped. `assemble_pane_views` is worker-thread
   shaped (blocking captures, `%N` handles to dodge non-thread-safe pid
   resolution) and unsafe in the async tool handler; `list_claudes`/`peek` cover
   read-state.
3. **Console idle detection** → v1 uses a fixed quiet-period heuristic (no new
   turn for N seconds after the transcript last grew). Good enough; revisit if it
   misfires.
4. **Naming** → rename `first_mate` → `commander` (refactor-mcp), and the rename
   must update `narrator.py`'s import + `_is_first_mate`.

## Open dependency to verify in planning

- The commander pane inherits the user's GUI-login subscription auth via the
  default tmux socket (Architecture #3 / Constraints). This is a runtime/env fact,
  not a code fact — confirm empirically before building (spawn a pane the way
  `_spawn_commander` will and check it's subscription-authed, not API-keyed).
