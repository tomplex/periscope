# `--bg` commander + job tracking — design

## Why this supersedes the singleton

The shipped commander is a **single persistent hidden tmux pane** the omnibox
sends commands to. Real-world testing exposed its core flaws:

- **Transcript pollution** — one conversation accumulates every command; the
  console shows old commands mixed with the new one.
- **Serialization** — commands queue in one conversation; no concurrency.
- **Context bloat / cross-command bleed** — prior commands confuse later ones.
- **Ambiguous "done"** — idle gaps between tool calls look like completion.

The fix is **per-command, ephemeral commanders**. A spike confirmed Claude
Code's background-agent feature (`claude --bg`) is the right engine — it gave us,
in one shot:

- **Subscription auth** (the spike ran on "Opus 4.8 · Claude Max", no API key).
- **Paneless MCP** — a `--bg` agent reached periscope's tools via `--mcp-config`
  (channel_shim) and successfully called `catalog`.
- **Warm start** — the per-user supervisor keeps a pre-warmed worker ready.
- **Enumeration** — `claude agents --json [--all]` returns `{sessionId, status,
  cwd}` per session.
- **Inspectable** — assigning `--session-id` means we know exactly which JSONL
  to read (`turns.py` already parses it).

So each command becomes its own `--bg` commander, and each `--bg` session *is* a
trackable job. This collapses the earlier (a) tmux-ephemeral + (b) daemon layers
into one, and makes (c) job-tracking nearly free.

## Goals

- Each omnibox command spawns a **fresh** `--bg` commander; many run concurrently.
- Each command is a **job** the omnibox lists (running / done), openable to its
  transcript, surviving close-and-reopen.
- Commanders stay **strict delegators** (orchestrate → spawn workers → report) on
  **subscription** billing.

## Non-goals (this phase)

- No two-way conversation with a commander (still fire-and-forget; the *worker*
  it spawns is where you converse). Revisit later.
- No job cancellation UI beyond `claude stop` plumbing (nice-to-have).
- No change to the worker model — `spawn_claude(repo, branch, prompt)` etc. stay.

## The dispatch

`POST /api/command {text}` no longer pokes a singleton. It:

1. Mints a `session_id` (uuid) and a job record.
2. Dispatches a background commander:
   ```
   claude --bg --session-id <session_id> \
     --append-system-prompt-file <ORCHESTRATOR_PROMPT_FILE> \
     --mcp-config <PERISCOPE_MCP_CONFIG> --strict-mcp-config \
     --model sonnet \
     --allowedTools "Read,Grep,Glob,mcp__periscope__catalog,mcp__periscope__open,mcp__periscope__create_workspace,mcp__periscope__spawn_claude" \
     "<text>"
   ```
   run with `cwd` = a sensible repo root (so the commander has a place to read
   from); the commander spawns workers elsewhere via its tools.

   **The allowlist is pinned to the four pane-independent tools, NOT
   `mcp__periscope__*`.** The wildcard would grant pane-dependent tools
   (`notify`, `link_pr`, `link_linear`, `open_document`, `report`, `peek`,
   `send_to`, `terminate`, `list_claudes`) that resolve the caller against a real
   `%N` window or feed the handle into `tmux`. A `cmdr:` handle matches no window,
   so those tools either error ("could not resolve pid") or — worse for `notify` —
   write alert state keyed on a bogus pane that the live-pane-driven `_channel_gc`
   never collects (a leak). The four allowed tools (`catalog`, `open`,
   `create_workspace`, `spawn_claude`) are exactly the delegator set the
   orchestrator prompt advertises and the only ones verified pane-independent.

   These CLI flags are all confirmed present in the installed `claude`
   (v2.1.190): `--bg`, `--append-system-prompt-file`, `--mcp-config`,
   `--strict-mcp-config`, `--session-id`, `--model`, `--allowedTools`.
3. Records the job and returns `{job_id: session_id}`.

The commander orchestrates and finishes; its `--bg` session goes `idle`/done.
Nothing to tear down — the supervisor reaps idle sessions after ~1h; the
transcript persists on disk regardless.

### `PERISCOPE_MCP_CONFIG`

A generated JSON that runs `channel_shim.py` with the **commander identity**
(see below) in its env, pointing at `/tmp/periscope-mcp.sock`. Written once at
lifespan boot (or per-dispatch) under the config dir.

## The hard part: caller identity (paneless)

periscope's MCP tools identify their caller by `$TMUX_PANE` (`%N`). The channel
server keys `_MCP_SESSIONS[pane]`, and `spawn_claude` reads the caller pane's
session/cwd; `is_commander_pane(pane)` checks the singleton marker. A `--bg`
commander has **no pane**. We replace the pane-as-identity assumption with an
explicit **commander handle**.

**Design:** the channel_shim's hello frame carries a handle that is either a tmux
pane id (`%N`, today's panes) **or** a commander id (`cmdr:<session_id>`). The
shim reads it from `PERISCOPE_CALLER_ID` (falling back to `$TMUX_PANE` for normal
panes). Server-side:

- **Two `startswith("%")` guards must be relaxed, not one.** Both
  `channel_shim.py` (`if not TMUX_PANE.startswith("%"): return`) and the server's
  connection handler in `channels.py` (`if not pane.startswith("%"): return`)
  currently drop any non-pane hello before the MCP session ever runs. The new
  accept predicate is "starts with `%` **or** `cmdr:`" in *both* places — the
  server-side guard alone would otherwise reject every `cmdr:` hello and close
  the socket.
- The channel server registers `_MCP_SESSIONS[handle]` exactly as today — the key
  is now an opaque handle, not necessarily a pane. The connection-handler
  `finally` does `_MCP_SESSIONS.pop(handle)`, which is fine for a `cmdr:` key. The
  separate `_channel_gc` (driven by live tmux pane ids) never sees `cmdr:` keys —
  which is *only* safe because the pinned allowlist (above) keeps a commander from
  ever creating `_CHANNEL_ALERTS`/`_CHANNEL_UNREAD` entries that would leak.
- `is_commander_pane(handle)` → `is_commander(handle)`: true iff the handle
  starts with `cmdr:`. (No singleton marker needed — being a commander is
  intrinsic to the handle, set at dispatch.) **Validate the id against the live
  dispatched-jobs set** (the `commands` table, status `running`): a hello whose
  `cmdr:<id>` is unknown is rejected. The socket is owner-only (`0o600`), so this
  is defense-in-depth, but it closes the "self-asserted prefix grants commander
  tools" gap (open question #1, resolved → yes) for a few lines.
- **Captain's-log gating moves to the handle check.** `captains_log_read` /
  `captains_log_append` are gated by `_require_commander` → `is_commander_pane`
  today; rewire to `is_commander(handle)`. Note these two tools are NOT in the
  pinned `--allowedTools`, so a `--bg` commander cannot reach them anyway — decide
  whether captain's-log survives the singleton's removal at all (default: drop it;
  it existed for the persistent singleton's cross-command memory, which the
  ephemeral model has no use for).
- `spawn_claude`'s caller-context derivation (`tmux display-message -t <pane>`
  for caller session/cwd) is **skipped for commander callers** — they always pass
  explicit `cwd`/`repo`/`branch` anyway (the strict-delegator prompt requires it),
  and the cwd-anchoring path already handles placement. A commander caller with
  no explicit cwd is an error (already the non-git guard's shape).
- Tools that resolve the caller against `list_windows()` (none needed by a
  commander's tool set — `catalog`/`open`/`create_workspace`/`spawn_claude` don't
  need the caller pane) are unaffected.

This is the central rework. It generalizes the existing pane-keyed registry to a
handle-keyed one, with `cmdr:` as a second handle kind.

## Job tracking (c)

A **`commands` table** (in the activity DB, alongside the dropped commander
marker):

```
id          TEXT PRIMARY KEY     -- the --bg session_id
text        TEXT                 -- the command
cwd         TEXT                 -- dispatch cwd
status      TEXT                 -- 'running' | 'done' (derived/synced)
started_at  INTEGER
```

Status is **synced from `claude agents --json --all`** (match by `sessionId`) on a
light cadence (the activity worker's 30s tick, prod-only) and on demand when the
omnibox opens.

**The output shape (verified against `claude` v2.1.190) is not what the original
draft assumed.** Each entry has a `kind` (`"background"` | `"interactive"`). A
`--bg` commander is `kind: "background"`, and background entries carry a **`state`**
field (observed values: `done`, `blocked`, plus running states), **not** the
`status` field that interactive sessions use (`idle`/`busy`/`waiting`). So the
mapping is:

- match `sessionId`; read `state`. `state == "done"` → `done`. Any other present
  `state` (running, blocked, …) → `running`.
- **absent from the list → disambiguate by age.** A just-dispatched session has a
  window before it registers in `claude agents`; "absent" must NOT mean "done"
  during that window or the on-open read flips a brand-new job to `done` before it
  starts. Rule: absent AND `started_at` older than a 60s grace → `done` (reaped);
  absent AND younger than the grace → keep `running`.

There is no `failed` status: `claude agents` reports no failure state we can
derive, so the table tracks only `running`/`done`. (A non-zero `subprocess`
dispatch exit could be recorded at dispatch time, but that's an immediate-failure
path, not a synced state — out of scope for this phase.)

Endpoints:
- `POST /api/command {text}` → `{job_id}` (dispatch, above).
- `GET /api/command/jobs` → `[{id, text, status, started_at}]` (newest first),
  joining the table with a fresh `claude agents --json` status read.
- `GET /api/command/jobs/{id}/turns` → that job's transcript via
  `turns.messages_from_jsonl` over the session's JSONL (located by session id).

## Omnibox: job list, not a console

The "⚡ run" card still dispatches. But picking it (or a new always-visible
"commands" affordance) opens a **job list**: each row a command with a status dot
(● running / ✓ done) and relative time. Selecting a row opens that job's
transcript (read-only, from `/api/command/jobs/{id}/turns`). Closing the omnibox
and reopening shows the same list — state lives server-side, so "close, do other
stuff, come back" just works. The single live-console mode is retired.

## Reused vs. replaced

**Reused:**
- The periscope MCP tools (`catalog`, `open`, `create_workspace`,
  `spawn_claude` incl. the `repo+branch` worktree-creation) — a commander calls
  them over `--mcp-config`.
- The **orchestrator role prompt** (now delivered via `--append-system-prompt-file`).
- `channel_shim.py` (generalized to a handle, not strictly `$TMUX_PANE`).
- `turns.py` for reading any session's JSONL.

**Replaced / removed** (the full caller list — grep `commander`, `COMMANDER_`,
`is_commander_pane`, `captain` across `periscope/`, `tests/`, `static/src/` and
account for every hit before declaring the singleton gone):
- `commander.py`'s singleton engine: `ensure_commander`, `_spawn_commander`,
  `COMMANDER_SESSION`/`COMMANDER_WINDOW`, the tmux-pane spawn. The module is
  deleted; everything that imports from it must be fixed in the same change.
- `periscope/app.py` — `_archive_stale_commander_project()` and the lifespan
  boot-spawn block (`commander.ensure_commander()`). Both reference the deleted
  module; remove them.
- `periscope/activity.py` — the `commander` marker table + `is_commander_pane`
  (marker read). `is_commander_pane(pane)` → `is_commander(handle)` (`cmdr:`
  prefix + jobs-table validation); the marker table is dropped.
- `periscope/routes/state.py` — the line that filters the commander pane out of
  the shipped rail via `is_commander_pane(...)`. A `--bg` commander is never a
  pane, so rail-hide is unnecessary, but this line still reads the going-away
  marker and must be deleted.
- `periscope/narrator.py` — `_is_commander()` (imports `COMMANDER_SESSION`/
  `COMMANDER_WINDOW`) and the two `is_commander_pane` skips. The skips become dead
  (commanders aren't panes/windows the narrator sees) but the import breaks on
  module deletion — excise all three.
- `channels.py` captain's-log gating (`_require_commander`) — rewire to
  `is_commander(handle)` or drop captain's-log entirely (see caller-identity
  section; default: drop).
- `/api/command` rewritten to dispatch; `/api/command/status` (singleton busy
  state) replaced by per-job status (`GET /api/command/jobs`).
- The omnibox live console (`useCommanderConsole`/`CommanderConsole` in
  `OpenOmnibox.jsx`) → job list. (`CommandsModal.jsx` is the launcher-exec editor,
  unrelated — leave it.)
- `is_commander_pane` marker check → `cmdr:` handle check.

## Open questions for review

1. **Commander identity scheme.** *Resolved → yes, validate.* `cmdr:<session_id>`
   via `PERISCOPE_CALLER_ID`, AND the server validates the id against the live
   `commands` table (status `running`) at hello time. A few lines; closes the
   self-asserted-prefix gap even though the owner-only socket already limits it.
2. **Dispatch mechanics / launchd auth.** *Partially resolved; one residual gate.*
   `claude --bg` is invoked via `subprocess` from the prod FastAPI process. The
   CLI surface is confirmed (flags above). `claude agents --json` already returns
   the user's real sessions when run non-interactively (non-TTY `command claude`),
   which is evidence subscription credentials are reachable outside an interactive
   shell. **Residual unknown:** prod runs under *launchd* (`com.tom.periscope`),
   whose env is more minimal than a login shell (PATH to `claude`, keychain ACL).
   This is a **gating verification (build-order step 0)** — confirm a
   launchd-context `claude --bg` authes on subscription *before* writing dispatch
   code; if it doesn't, the dispatch may need to go through a login shell or a
   small env shim, which changes step 2's shape (not the architecture).
3. **`claude agents` visibility.** A `--bg` commander shows up in the user's
   `claude agents` view (alongside their own dispatches). Is that fine (it's
   honest — periscope dispatched it), or do we want them visually distinguished /
   hidden there? (We don't control that view.)
4. **Cleanup.** *Resolved → proactive stop.* Once a job's `state` reads `done`,
   periscope `claude stop <id>`s the session. This frees the supervisor slot and
   makes the "absent → reaped" sync branch a real terminal signal instead of
   waiting out the ~1h reap window (during which `claude agents --all` keeps
   returning the session as `done` anyway — harmless, just untidy).
5. **MCP socket dependency.** `--bg` commanders connect to
   `/tmp/periscope-mcp.sock`, which only exists in prod. So commands remain
   prod-only — same constraint as today; fine.

## Build order

0. **Gate: launchd subscription auth.** Confirm a launchd-context `claude --bg`
   (the prod spawn env, not an interactive shell) authenticates on subscription.
   If it can't, decide the env fix (login shell / env shim) before writing
   dispatch code. (CLI flags + `claude agents --json` shape already verified.)
1. Caller-identity rework in `channel_shim.py` + the channel server (handle
   generalization, `cmdr:` kind, **both** `startswith("%")` guards, `is_commander`
   with jobs-table validation). Tested against a synthetic `cmdr:` dispatch.
2. `/api/command` dispatch + the `commands` table + status sync (read `state` for
   background; absent+grace; proactive `claude stop` on `done`).
3. `GET /api/command/jobs` + `/jobs/{id}/turns`.
4. Omnibox job list.
5. Rip out the singleton — enumerate every caller from "Replaced / removed"
   (`commander.py` delete, `app.py` boot-spawn + archival, `state.py` rail-hide,
   `narrator.py` skips, captain's-log gating, `/api/command/status`). **Grep gate:**
   `grep -rn "commander\|COMMANDER_\|is_commander_pane\|ensure_commander" periscope/ tests/ static/src/`
   must come back clean (only the new `is_commander`/`cmdr:` references).
6. Verify in prod: dispatch 2–3 concurrent commands, confirm subscription auth,
   concurrency, close-and-come-back, and that each commander delegates correctly.
