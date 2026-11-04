# Channels (in-process MCP)

`periscope/channels.py` plus `channel_shim.py`. The shim is the documented
stdio MCP entry point; the server logic (tool implementations, notification
emission, session registry) runs in-process so it shares the dashboard's
state.

Tools exposed to Claude — 18, all registered in `_CHANNEL_TOOLS`
(`periscope/channels.py`), which holds each one's schema and the description
Claude reads. That registry is the source of truth; this list says what each
is for and records the invariants behind the ones that have them.

The pane's card:
- `notify(message, kind=done|need_human|info, severity?)` — surfaces an
  alert on the pane card and in the dashboard's alert feed.
- `link_pr(number)` — bind a GitHub PR to the pane, even if Claude's
  status-line URL isn't visible.
- `link_linear(id, title?, status?)` — same for Linear tickets. Without
  a link, periscope shows the ticket a `KEY-123:` PR title or a
  Linear-shaped branch names, as the id alone (`git_pr.linear_ticket_for`;
  `settings.linear_team_keys` narrows it to known keys); a link replaces
  that. Optional `title`/`status` metadata renders on the card and in the
  Inspector; each call fully describes the link. The Reviews queue shows the
  same ticket per row: the owning pane's link, else what the row's PR title
  or branch names. One builder, `git_pr.linear_ticket_fields`, makes all
  five fields for the pane payloads and the queue rows alike.
- `set_name(name)` — rename the caller's own window and pin the name
  against the narrator (see the narrator section's pin invariant). The
  only self-naming path: `spawn_claude(name=…)` could name a CHILD, so a
  pane holding a standing role had no way to assert its own label.
- `open_document(path, line?)` — open a file as a preview tab on the
  caller's pane, quietly (no focus steal); `line` forces source view.

Other Claudes:
- `spawn_claude(prompt, workspace?, session?, cwd?, repo?, branch?, name?,
  account?, model?, workspace_id?)` — fork a fresh Claude pane in a new tmux
  window with the given first message. `repo` + `branch` spawn into that
  worktree (created if absent); `account` / `model` override launch routing
  (`docs/account-routing.md`).
  `workspace="same"` (default) adds a window to the caller's session, so
  the spawn nests under the caller's rail item (fan-out / related work —
  the rail is session-anchored, so a different `cwd` shows only as a
  chip). `workspace="new"` anchors the spawn to its `cwd`'s worktree as
  its own rail item: `open_ops.resolve_worktree_session` registers the
  project + dedupes a foreign-name clash, the spawn creates the session
  (or new-tabs into an existing worktree session), then `place_in_rail`
  records the ordering. Non-git `cwd` with `workspace="new"` falls back
  to `"same"` (no worktree to anchor a rail item to). Its result carries
  the spawned pane's `track` (the rail group it landed in, after the
  precedence above plays out).

- `list_claudes(track?)` — every live Claude pane with its handle (pid),
  status, channel attachment, spawner and git head. Rows carry `track` (the
  rail group each pane sits in), the response carries `you` (the caller's
  own handle + track), and `track: "mine"` filters to the caller's roster.
- `peek(handle, limit?, detail?)` — read another pane's recent transcript
  without sending it anything; the source of truth for what a pane is doing
  (its status line is only a summary).
- `send_to(handle, message)` — wake another attached pane with a message.
  Success means queued, not read — `peek` is the delivery check.
- `report(message)` — send a result back to the pane that spawned the
  caller. Always lands: with no live, attached spawner it becomes a user
  alert on the caller's pane, and `delivered_to` says which happened.
- `terminate(handle)` — kill another Claude's window; refuses the caller's
  own.

History (the `/history` index, so live panes aren't in it):
- `search_history(query, project?, since?, limit?)` — full-text search over
  past sessions; returns summaries + session ids.
- `get_history_session(session_id, offset?, limit?)` — a slice of one
  session's conversation, the last 30 messages by default.
- `resume_session(session_id, tmux_session?, workspace_id?, account?)` —
  `claude --resume` in a new window, in the session's original directory;
  refuses a session that looks live.

Repos and rail groups:
- `open(path? | repo+branch? | repo+pr?)` — materialize a directory, a
  worktree (created if absent) or a PR checkout into the rail, through the
  same create-or-focus core as ⌘K (`docs/unified-open.md`).
- `catalog()` — repos and their worktrees, dormant and live. Git-heavy:
  once per command, never polled.
- `create_workspace(name, base_repo?)` / `list_workspaces()` — goal-scoped
  rail groups that `spawn_claude(workspace_id=…)` tags into. Legacy:
  `tracks.migrate_workspaces_to_tracks()` folds every workspace into a goal
  track at boot.

Notifications go the other way as `notifications/claude/channel`
messages, surfacing in Claude's prompt as `<channel source="periscope">`
blocks. The pinned `mcp==1.27.*` is checked at startup and exercised by
`tests/test_channel_shim.py`; bump both together.

## Shim survives periscope restarts

`channel_shim.py` is not a dumb bytes proxy. When the unix socket drops
mid-session (periscope restart, dev cycle, lifespan teardown), the shim:

- Synthesizes JSON-RPC error responses for any tool calls in flight so
  Claude doesn't hang.
- Reconnects at `PERISCOPE_MCP_RECONNECT_BACKOFF_S` (default 1s) until
  the socket comes back or stdin EOFs (Claude exited).
- On the fresh socket, re-sends the hello frame, replays the captured
  `initialize` request, replays `notifications/initialized`, and synths
  a `tools/list` so periscope's `_list_tools` handler re-registers
  `_MCP_SESSIONS[pane]` — required for push notifications and tool
  routing.
- Swallows the duplicate `initialize` response from the new periscope
  and the synthetic `tools/list` response; Claude only sees them once.

Net effect: Claude's MCP connection survives `bin/periscope restart`
and most lifespan-cycle blips without needing `/clear`. The non-zero-
exit invariant (item 10 below) still holds — the shim only exits 0,
just rarely now.

## Pane → session mapping (the transcript view)

`periscope/turns.py` renders a pane's Claude conversation as a structured
transcript (the split-view "Transcript" mode + `GET /api/pane/turns`). It must
map a tmux pane to its *specific* session JSONL — **cwd alone collides** when
several Claude panes run in one directory (newest-mtime returns the same file
for all of them). The mapping lives in the `pane_sessions` table in
`~/.config/periscope/periscope.db` (`pane_id → session_id`, where `session_id`
is the JSONL stem / `CLAUDE_CODE_SESSION_ID`); `turns.py` reads it via
`activity.get_pane_session` and globs for `<id>.jsonl` (glob, not cwd-encode —
a pane that `cd`'d into a worktree has its JSONL under the *start* dir's
encoding). Lifespan runs a one-shot import from the legacy
`~/.config/periscope/pane_sessions/` directory layout (`migrate_legacy_pane_sessions`)
and prunes rows for tmux pane ids that no longer exist.

**The recorded row is the FALLBACK, not the authority.** Claude mints a new
session id when a conversation is resumed or compacted, and the hook does not
always fire for the successor — a pane then points at a superseded transcript
(cost a real conversation: `move-account` resumed the pre-rotation id and landed
~18h back). `turns.session_id_for_pane` therefore asks
`session_status.live_session_id_for_pane` first: the pane's process subtree is
walked to its claude pid (`session_status.pane_claude_pids`, the one
implementation — `session_status.pane_config_dirs` builds its scan on it, and
resurrect and window_view consume that) and
`~/.claude/sessions/<pid>.json` is read for the sessionId that process reports
*now*. `pane_sessions` answers only when there is no live claude.

The producer is **`pane_session_hook.py`**, registered on Claude's
`SessionStart` *and* `UserPromptSubmit` events by `bin/periscope install-hook`
(run from `install`; removed by `uninstall-hook`). It reads `session_id` from the
hook **payload** (current, so it survives `/clear` — which mints a new session
id) and `TMUX_PANE` from a direct child of the pane's Claude (the real pane id).
A deep `ps`/env scan is deliberately NOT used: inherited
`CLAUDE_CODE_SESSION_ID`/`TMUX_PANE` from tool/subagent subprocesses
cross-contaminate, and a `/clear` leaves a spawn-time env stale — the payload is
the only authoritative, current source.

- **SessionStart** (fires at startup + `/clear`) records the pane's session
  *immediately*, before its first prompt — so a fresh pane shows its OWN
  transcript at once instead of cwd-falling-back to whatever was most recently
  active.
- **UserPromptSubmit** (every prompt) migrates panes that predate the hook —
  they self-correct on their next message, once their session has loaded the
  hook. Assume only sessions started after `install-hook` have; PR 2's hand
  check (2b plan, Task 9) settles whether Claude loads new hooks live.

`install-hook` also invokes the provider-specific Codex installer. It merges
dedicated Periscope groups for `SessionStart`, `UserPromptSubmit`, `Stop`, and
`SessionEnd` into `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`) with
an atomic, mode-preserving write. It never edits other groups or bypasses
Codex's hook trust; use `/hooks` to review and trust the command. The standalone
`codex_pane_session_hook.py` is stdlib-only, silent, and records sanitized
lifecycle metadata in `agent_sessions`/`agent_session_events`. Until Stage-0
live capture proves `TMUX_PANE` and root-vs-subagent behavior, it accepts only a
matching `codex-tui` rollout under `CODEX_HOME/sessions`, marks evidence
`codex-hook-unverified`, and must not be treated as authoritative for status.
`GET /api/healthz` exposes this as `codex_hook.verification: "unresolved"` and
reports installation/observation without claiming trust.

Resolution falls back to newest-mtime-in-cwd when a pane has no recorded session
yet. The earlier `channel_shim.py` recorder was removed — the hook's payload is
strictly better (current vs spawn-frozen).

The same mapping gives a done banner its words (THI-416). For a `done` Claude
pane, `window_view` publishes `last_reply`: `turns.last_reply_for_session`
reads the session's transcript backward from the end to the newest assistant
text, skipping meta and sidechain events and Claude's bookkeeping rows
(`turn_duration`, `artifact-autoreact-ledger`, `mode`, …). It returns None once
a prompt follows that text (the new turn has said nothing yet), and flattens the
markdown so a link's URL doesn't fill the banner. It's cached per session on the
file's size and mtime, so a done pane costs a `stat` per poll. There is no
newest-mtime-in-cwd fallback here: a wrong transcript would put another pane's
words in the banner, so without the session the banner falls back to
`last_line`, the screen's guess.

## Permission requests (a banner's Allow / Deny)

**`pane_permission_hook.py`** is `pane_session_hook.py`'s sibling:
- **Registration.** The same `bin/periscope install-hook` registers it on Claude's `PermissionRequest` event, with no matcher, so it runs for every tool. `uninstall-hook` removes it.
- **When it runs.** Claude runs it just before a permission dialog renders.
- **How it reads and writes.** Like `pane_session_hook.py`, it reads `TMUX_PANE` and the hook payload, never an inherited env, and opens its own short-lived connection to prod's `periscope.db`.

**What it records.** One `pane_permissions` row per dialog, and it prunes rows older than a day:
- the pane id;
- the session and agent ids. A subagent's call asks in its parent's pane;
- the tool;
- a display summary of at most 80 characters, in clear text and published on `/api/state`: Bash's command (a long one clipped to its head … tail), a file tool's path, WebFetch's URL, or else the first string argument;
- a `match_key`;
- the time.

The payload carries no `tool_use_id`, so `request_id` is a hash of the session, tool, input and time.

**The match key** is what Allow must find on screen before it sends `1` (`panes.dialog_matches`):

| Request | Key |
|---|---|
| Bash | `sha256:` of the command + description with all whitespace and hidden controls removed |
| Edit / Write | the absolute path. The dialog's question path must be a `/`-boundary suffix of it |
| anything else, or Bash with `dangerouslyDisableSandbox` | NULL, so the banner offers Open only |

- **Bash's key is a digest, so the WHOLE command never sits in the key.** Commands carry tokens, and `periscope.db` is 0644. The summary still shows the command's head and tail in clear text, by design: it's what the banner displays. The screen side hashes its own squashed block and compares the two.
- **Two definitions, pinned together.** The hook runs on bare `python3` and can't import `periscope`, so it and `panes.py` each define `VERIFIABLE_TOOLS` and the hidden-control class, and tests pin the copies together.
- **The column heals itself.** A table created before `match_key` gets it by `ALTER`, from whichever side opens the DB first (the hook, or `activity._conn`). Otherwise every INSERT would fail, silently.

**Why it prints nothing and fails open.** A `PermissionRequest` hook's JSON stdout is read as its decision on the dialog. So this observer never prints, swallows every failure and exits 0: a broken recorder must never answer, or block, a prompt. Every failure costs the banner its Allow / Deny, never the dialog. Two layers back this up:
- **The registration**, `python3 -I -S <path> || true`:
  - `python3` can be the Mac's 3.9 with no venv, so the file stays 3.9-safe;
  - `-I -S` keeps a site or venv out;
  - `|| true` makes even a missing file exit 0.
- **The busy wait.** SQLite's is 0.5 s, well inside Claude's 5 s hook timeout, since a busy wait overruns its budget about 3×.

**What `install-hook` does to existing entries:**
- **It joins only a matcher-less group** (`matcher` absent, `""` or `"*"`), so a user's matcher-scoped group can't narrow ours.
- **It repoints an existing entry only when that entry's target is gone, or when it already points at this checkout.** Never another live checkout's: an install from a worktree would otherwise take over prod's entry, and removing the worktree would leave a dangling path.
- **`pane_session_hook.py`'s entry ends in `|| true` too.** A nonzero `UserPromptSubmit` blocks the prompt.

**Who reads it.**
- **`window_view.pending_permission_for`** publishes `w.pending_permission = {request_id, tool, summary}` when exactly one row was stamped no earlier than 5 s before the wait's `needs_since` (no upper bound) and that row has a key.
- **`POST /api/pane/permission`** (`routes/pane_actions.py`) recomputes the row, key included, and checks it against a fresh capture before sending `1` or Escape (`docs/invariants.md`, invariant 20).

**Gaps:**
- **Prod's DB only.** The hook can't know which instance is looking, so a dev server never publishes `pending_permission`.
- **Auto mode.** A pane in auto mode (2.1.280's default) asks only when auto mode defers to you.
- **Old sessions.** Only sessions started after `install-hook` are assumed to have loaded the hook; one that hasn't gets Open-only banners.
