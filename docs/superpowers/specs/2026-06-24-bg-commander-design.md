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
     --allowedTools "Read,Grep,Glob,mcp__periscope__*" \
     "<text>"
   ```
   run with `cwd` = a sensible repo root (so the commander has a place to read
   from); the commander spawns workers elsewhere via its tools.
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

- The channel server registers `_MCP_SESSIONS[handle]` exactly as today — the key
  is now an opaque handle, not necessarily a pane.
- `is_commander_pane(handle)` → `is_commander(handle)`: true iff the handle
  starts with `cmdr:`. (No singleton marker needed — being a commander is
  intrinsic to the handle, set at dispatch.)
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
status      TEXT                 -- 'running' | 'done' | 'failed' (derived/synced)
started_at  INTEGER
```

Status is **synced from `claude agents --json --all`** (match by session id;
`busy`→running, `idle`/completed→done, absent→done/reaped) on a light cadence
(the activity worker tick, prod-only) and on demand when the omnibox opens.

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

**Replaced / removed:**
- `commander.py`'s singleton engine: `ensure_commander`, `_spawn_commander`, the
  tmux-pane spawn, the `commander` marker table, the boot-spawn + rail-hide +
  narrator-skip (a `--bg` commander isn't a tmux pane, so it never appears in the
  rail or the narrator — those become unnecessary).
- `/api/command` rewritten to dispatch; `/api/command/status` (singleton busy
  state) replaced by per-job status.
- The omnibox live console → job list.
- `is_commander_pane` marker check → `cmdr:` handle check.

## Open questions for review

1. **Commander identity scheme.** `cmdr:<session_id>` via `PERISCOPE_CALLER_ID`
   in the shim env — good enough, or do we want the server to validate the id
   against the dispatched-jobs table (so a random process can't claim `cmdr:`)?
2. **Dispatch mechanics.** `claude --bg` is invoked how from the FastAPI process
   — `subprocess` from the prod server? It needs the user's login cred context
   (the supervisor uses stored credentials); confirm a launchd-spawned
   `claude --bg` authes on subscription (the spike ran from an interactive shell;
   launchd is the real target — verify, like the original auth dependency).
3. **`claude agents` visibility.** A `--bg` commander shows up in the user's
   `claude agents` view (alongside their own dispatches). Is that fine (it's
   honest — periscope dispatched it), or do we want them visually distinguished /
   hidden there? (We don't control that view.)
4. **Cleanup.** Finished commanders linger ~1h then the supervisor reaps them.
   Do we proactively `claude stop <id>` a commander once its job is marked done,
   or let the supervisor handle it?
5. **MCP socket dependency.** `--bg` commanders connect to
   `/tmp/periscope-mcp.sock`, which only exists in prod. So commands remain
   prod-only — same constraint as today; fine.

## Build order

1. Caller-identity rework in `channel_shim.py` + the channel server (handle
   generalization, `cmdr:` kind, `is_commander`). Tested against the spike-style
   synthetic dispatch.
2. `/api/command` dispatch + the `commands` table + status sync.
3. `GET /api/command/jobs` + `/jobs/{id}/turns`.
4. Omnibox job list.
5. Rip out the singleton (`ensure_commander`/marker/boot-spawn/rail-hide/
   narrator-skip/`/api/command/status`).
6. Verify in prod: dispatch 2–3 concurrent commands, confirm subscription auth,
   concurrency, close-and-come-back, and that each commander delegates correctly.
