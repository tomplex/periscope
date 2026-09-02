# Channels (in-process MCP)

`# --- Channels ---` block in `server.py` plus `channel_shim.py`. The
shim is the documented stdio MCP entry point; the actual server logic
(tool implementations, notification emission, session registry) is in
`server.py` so it has access to the same state the dashboard does.

Tools exposed to Claude:
- `notify(message, kind=done|need_human|info)` — surfaces an alert on
  the pane card and in the dashboard's alert feed without opening the modal.
- `link_pr(number)` — bind a GitHub PR to the pane, even if Claude's
  status-line URL isn't visible.
- `link_linear(id, title?, status?)` — same for Linear tickets (no
  auto-detection path). Optional `title`/`status` metadata renders on
  the card and in the modal; each call fully describes the link.
- `set_name(name)` — rename the caller's own window and pin the name
  against the narrator (see the narrator section's pin invariant). The
  only self-naming path: `spawn_claude(name=…)` could name a CHILD, so a
  pane holding a standing role had no way to assert its own label.
- `spawn_claude(prompt, workspace?, session?, cwd?, name?)` — fork a
  fresh Claude pane in a new tmux window with the given first message.
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

`list_claudes` rows carry `track` (the rail group each pane sits in), the
response carries `you` (the caller's own handle + track), and
`track: "mine"` filters to the caller's roster.

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
  they self-correct on their next message (Claude loads new hooks live; no
  plugin reload needed).

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
