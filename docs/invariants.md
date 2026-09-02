# Key invariants (the things that broke and we fixed)

These are the non-obvious behaviors worth preserving:

1. **`focused_at` is server-tracked, not tmux's `window_activity`.**
   tmux's activity stamp bumps on any pane output (streaming logs, dev
   servers, Claude tokens). We instead record when a window becomes the
   active window in its session, or when the user acts on it via the
   dashboard (focus/send). See `update_focus_from_windows`.

2. **Claude detection requires status line in the last 4 non-empty lines.**
   Old status lines in scrollback should not trigger `is_claude=true` after
   the user has returned to a shell. See `parse_pane`.

3. **WebSocket paint is self-healing, not perfect.** The initial blob
   still mirrors tmux's size/cursor/alt-screen state (all from
   `display-message` before the capture body), but live bytes come from a
   per-session tmux control-mode client (`tmux_mirror.py`), and the
   mirror periodically ships an idempotent repaint of tmux's own grid.
   Reconcile frames are built **inside the reader task at the reply's
   `%end`** — building them in a future-woken task would let later
   `%output` land first and be reverted by the frame. Don't "optimize"
   this to futures.

   **The cursor is sampled BEFORE the body, and the order matters.** The
   two samples are separate tmux commands (`display-message` then
   `capture-pane`), so a character echoed between them lands in exactly
   one of the pair. A cursor *fresher* than the body renders one cell
   past a character the body doesn't carry, and the row's `\x1b[K` then
   erases that character — a visible gap that persists until the pane's
   next output. Staler cursor is the safe direction: text intact, cursor
   at worst one cell behind, corrected by the next byte. Reported as
   "the cursor is one ahead of where typing lands, with stuttering as it
   reconciles". Sending both as one `;`-joined command does NOT fix it —
   tmux still replies with two `%begin`/`%end` blocks, and
   `_send_command` registers one callback per write, so a combined line
   desyncs the whole reply-callback queue.

4. **`capture-pane` separates rows with bare `\n`; xterm needs `\r\n`.**
   Forgetting the carriage return staircases every line right by the
   previous line's length.

   **Every `capture-pane` that feeds a paint needs `-N`.** By default tmux
   strips trailing spaces, so a row whose content ends in whitespace renders
   SHORT while the cursor is still placed at tmux's true column — one empty
   cell between the text and the cursor. That is most of shell usage: every
   space typed between words, and every idle prompt (a `PS1` ending `"$ "`).
   Reported as "the cursor is one ahead of where typing lands, and I have to
   remember to place it one ahead". Measured on a live pane: `cursor_x=59`
   against a captured width of 58. `-N` pads to the pane width; the cost is
   1.0–1.8× on real panes (~10KB), which is worth it. Two call sites:
   `tmux_mirror._fire_reconcile` and the initial paint in `routes/ws.py`.

   Two things this is NOT, both chased first: it is not a sampling race
   between the body and cursor captures (that is real, and is why the cursor
   is sampled first — but a race is intermittent, and this offset is
   perfectly deterministic), and it is not emoji width (xterm needed the
   unicode11 provider, see below, but fixing that alone left the gap intact).
   When an offset is reproducible to the cell, look for an off-by-one in what
   gets DRAWN, not for a race.

5. **Multi-line input goes via tmux paste-buffer, then Enter via send-keys.**
   `send-keys` silently strips embedded newlines. There's a 100ms sleep
   between paste and Enter so TUIs (especially Claude Code) apply paste
   state before submit lands. See `/api/send`.

6. **Session/index are query params, not path segments.** Session names
   like `tc/foo/bar` contain slashes; path routing decoded `%2F` and 404'd.

7. **Spinner has hysteresis at the data layer.** `capture-pane` runs
   mid-redraw drop the spinner line; without smoothing, the "thinking"
   indicator flickers. Done server-side in `smooth_spinner` (panes.py),
   applied per-pane in `build_window_view`.

8. **Background-thread crashes must surface.** Every `threading.Thread`
   and `asyncio.create_task` call goes through `_bg` / `_task`. A naked
   `Thread(daemon=True)` that raises just disappears, and "the server's
   flakey" becomes uninvestigable.

9. **Pidfile reclaim treats reloader-child as the same instance.** Under
   `--reload`, uvicorn forks a worker. The pidfile holds the parent;
   killing the parent in reclaim would also nuke a healthy reloader.
   Check `PERISCOPE_DEV` and the process tree before terminating.

10. **`channel_shim.py` exits 0 on every failure mode.** Missing
    `$TMUX_PANE`, periscope not running, unreachable socket — all clean
    exits. A non-zero exit pops macOS's crash reporter every time Claude
    reconnects, which is intolerable for a nice-to-have channel.

11. **`@periscope_id` is stamped by WINDOW ID (`@N`), never `session:index`.**
    Indices renumber under `move-window` — which the single-session
    migration does in bulk and which moving a tab between tracks does
    again. A stamp aimed at `session:index` after a renumber lands on a
    *different* window, so the duplicate that triggered the re-mint is
    never cleared and the next poll re-mints again: a self-sustaining
    loop (observed 683 times on one window across three days, in
    `~/.config/periscope/periscope-8765.log`). A re-mint changes a
    window's identity, and `railSelection` is keyed `pane:<pid>` — so the
    detail pane silently detaches. Historically reported as "detail pane
    closes on cd". Regression signal on the log: same-poll duplicates
    surface as `duplicate @periscope_id ... keeper ...` (INFO, from
    arbitration — invariant 18) while `re-minting` (WARNING) covers
    cross-poll residue; `grep "re-minting\|duplicate @periscope_id"`
    catches both.

12. **Rebind eligibility (`_REBIND_TTL_S`, 15 min) is NOT GC retention
    (`_PID_TTL_S`, 30 days).** Rebind exists so persisted state reattaches
    when the tmux server restarts — window options are lost, so every
    window is re-sighted unstamped — and that happens seconds-to-minutes
    after boot. Sharing the 30-day GC TTL meant a fresh Claude at a repo
    root on master matched ANY entry from the past month via the
    `(branch, cwd)` fallback and inherited its `_IMMUNITY_FIELDS`,
    surfacing as a brand-new pane wearing a stale PR and Linear ticket.
    Both passes are collision-prone by construction now that `session` is
    a constant — the TTL is what keeps them honest.

13. **Rebind must never hand out an id a live window still carries.**
    `resolve_pids` builds `taken` incrementally, so a window resolved LATER
    in the pass was an eligible rebind candidate — its entry was refreshed
    seconds ago, well inside `_REBIND_TTL_S`. An unstamped window sitting
    earlier in the list therefore matched a LIVE window on (session, name),
    or on the `(branch, cwd)` fallback that a spawn into the caller's own
    worktree hits by construction, and stole its identity. That was the
    duplicate FACTORY; the dedup gate in `_resolve_one` only cleans up
    afterwards, re-minting the victim. `resolve_pids` now pre-computes
    `carried` (every well-formed `pid_raw` in the pass) and excludes it from
    rebind. `spawn_claude` already worked around this locally with
    `stamp_new_window`; every other creation path was exposed. Regression
    signal on the log: `grep "re-minting\|duplicate @periscope_id"` —
    same-poll duplicates land as the INFO arbitration message
    (invariant 18), cross-poll residue as the `re-minting` WARNING.

14. **A pane can be `attached` and still deaf.** Claude registers for
    `notifications/claude/channel` only when the server is named in its
    channel flags, so a Claude started WITHOUT `config.CHANNEL_FLAG`
    connects the shim (populating `_MCP_SESSIONS`, so `attached` is true)
    and then discards every push. `send_to` / `report` returned `ok: true`
    for messages nothing could receive. `channels.pane_channel_ready`
    reads the flag out of the pane's claude argv and `_deliver` refuses
    up front. The usual way to land flagless: `claude` is a zsh function
    resolved at shell startup, so a long-lived shell keeps a stale copy —
    periscope-spawned panes use `CLAUDE_EXEC` and are always ready.
    `list_claudes` exposes this as `channel_ready`, distinct from
    `attached`.

15. **`_MCP_SESSIONS` deregistration is identity-checked.** The registry is
    keyed by pane and the shim reconnects on the same pane after a restart,
    so an unconditional `pop` in the connection teardown let a dying
    connection evict the live successor that had already replaced it.

16. **`report` always lands.** A lead that exits before its worker finishes
    is the norm, not an edge case. Hard-failing destroyed the result — the
    worker had done the work and fell back to hand-writing a file. When no
    spawner is recorded, or it has exited or is deaf, the report is recorded
    as a user-facing alert on the worker's own pane; `delivered_to` says
    which happened.

17. **A delivered channel push is a META turn, and peek must show it.** It
    lands in the recipient's transcript as an `isMeta` user turn opening
    `<channel source="periscope"` — and `messages_from_jsonl` drops every
    `isMeta` event, so the one thing a sender peeks to confirm was invisible
    to peek by construction. A sender saw no block, concluded `send_to` was
    silently dropping, spent 40 minutes on it, filed a bug that had to be
    retracted, and re-sent the same directive four times.
    `turns.channel_messages_from_jsonl` extracts them and peek merges them by
    timestamp. That block is written by the RECIPIENT'S own Claude, so its
    presence is the delivery receipt; `send_to` reports `delivery: "queued"`
    (never "delivered") because a notification surfaces only on the target's
    next turn.

    Corollary, worth knowing before diagnosing: the incident above was NOT
    peek staleness. That pane's Claude had been started without the channel
    flag (invariant 14) and genuinely received nothing — peek was the only
    tool telling the truth, and the retraction was itself wrong. The work
    landed because Tom pasted it in by hand.

18. **Pane identity is session-id-first.** `resolve_pids` takes precomputed
    session hints (live sid + `--resume` lineage per pane, built by
    `_attach_git_then_resolve_pids` BEFORE `_STATE_LOCK` — hint-building forks
    tmux/ps, and list_claudes resolves on the event loop). Rebind pass 0
    matches `last_seen.sid` TTL-exempt (a sid is unique; the 15-min TTL guards
    occupancy collisions, invariant 12); pass 0b matches the argv
    `--resume <uuid>` lineage but ONLY with cwd corroboration — the hint is
    regex over ps argv, which flattens prompts, so a pane whose prompt merely
    quotes a resume command must not inherit a dead session's identity.
    Duplicate pids are arbitrated by recorded-sid evidence
    (`_arbitrate_duplicates`), not list order. Every rebind and arbitration
    decision is logged — `grep "rebind\|duplicate @periscope_id"` is the
    regression signal (the re-minting warning alone goes quiet for same-poll
    duplicates, which arbitration now intercepts). The session index scans
    EVERY live account's `<config_dir>/sessions/` with a per-pane config-dir
    tiebreak (a recycled pid leaves a stale same-pid file in the other
    account's dir).

19. **`pane_tracks` keys on `@periscope_id`, never `%N`.** The column is
    named `pid` so a raw-SQL regression against `pane_id` fails loudly, and
    the move-tab route 422s on a `pane_id` body field. Legacy `%N` rows
    migrate lazily in the first completed FULL-ROSTER resolve pass
    (`_maintain_track_rows`), which also owns the prune — gated on the pass's
    `taken` set because a boot-time prune fires before rebind can reattach,
    and a partial (single-window) pass's one-pid taken set would mass-delete
    every other pane's rows.
