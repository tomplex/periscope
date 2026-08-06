# Spec — session-keyed identity + track-aware channels

Periscope keys pane identity on tmux occupancy — window options, `(session,
name)`, `(branch, cwd)` — while the durable identity it already knows how to
read, the Claude session id, sits unused by the resolver. The FDY-6630 field
report (`docs/notes/2026-08-05-fdy6630-collab-field-report.md`) documents what
that costs under restarts: a full identity merge where two same-worktree
sessions folded into one record, the survivor wore the other pane's identity,
and the arbitration hub went channel-mute while a dependent pane held dispatch
on its ruling.

Three shippable units, in dependency order:

- **Ship 1 (§1)** — the session index covers every live account. Standalone
  bug fix; prerequisite for the rest; also repairs transcript resolution and
  narrator tracking for secondary-account panes today.
- **Ship 2 (§2–§4)** — session id becomes the primary identity key: rebind
  pass 0, duplicate arbitration by rightful owner, decision logging.
- **Ship 3 (§5–§6)** — track membership re-keys to the durable identity, then
  gets exposed to the Claudes: roster rows, `you`, a `track` filter — the
  query the field report's panes reconstructed by hand from names, cwds, and
  commit subjects.

## Problem

All timestamps 2026-08-05, `~/.config/periscope/periscope-8765.log`:

- **15:47 (account swap into the `resumes` tmux session):** `duplicate
  @periscope_id 13d1fe3e on resumes:2 — re-minting` and `4215611f on
  resumes:3`. Those pids are `pit-optimization` and `pit-diff-machinery` — the
  field report's program panes. The same pid was live on two windows across
  two tmux sessions.
- **20:06 → 20:11 (the report's addendum):** `3c5e9a10` flagged duplicate on
  `periscope:4`, then on `resumes:4`. The dedup gate keeps whichever window
  comes **earlier in the poll's list order** (`pids.py:144`), so the pid
  ping-ponged between the two windows across polls. Every addendum symptom
  follows: `open_document` returning another pane's pid, `send_to` bouncing
  "refusing to send to your own pane", `list_claudes` dropping the pane,
  status lines crossing.
- **The theft is invisible.** The only log line in `pids.py` is the duplicate
  warning — the cleanup, not the cause. Which entry a rebind matched, and on
  which pass, is unrecorded; this incident could not be root-caused from the
  log alone.

Three code-level weaknesses compound:

1. **The `(branch, cwd)` rebind fallback (`pids.py:88-92`) is ambiguous by
   construction** when two panes share a worktree — the FDY-6630 hub topology.
   Within the 15-minute rebind TTL, two entries with identical `(branch, cwd)`
   are indistinguishable; dict order picks.
2. **Duplicate resolution has no rightful-owner tiebreak** (`pids.py:144-154`)
   — first-in-list wins, and list order is not stable across polls.
3. **`session_status._SESSIONS_DIR` is hard-coded to `~/.claude/sessions`**
   (`session_status.py:33`), so `live_session_id_for_pane` returns None for
   every pane running under `CLAUDE_CONFIG_DIR=~/.claude-b`. The authoritative
   identity source is blind on exactly the panes an account swap creates.

Downstream of the same instability: `pane_tracks` is keyed on the tmux pane id
(`activity.py:72`, `%N`), which rotates on every restart the report describes.
When it rotates, an explicit track tag silently detaches and the pane falls
back to its repo-default track (`tracks.py:57-65`) — a goal-track roster would
have dropped the resumed panes mid-program.

## What exists today

- `session_status.live_session_id_for_pane(pane_id)` (`session_status.py:237`)
  — walks the pane's process subtree to its claude pid
  (`pane_claude_pids`, breadth-first, first claude wins), reads
  `sessions/<pid>.json` for the sessionId that process reports *now*. All
  layers cache at `_CACHE_TTL_S = 1.0`, so per-window calls inside one poll
  collapse to two forks. Already trusted as the authority by `turns.py`
  (transcript resolution) — the resolver is the holdout.
- `resurrect._pane_config_dirs()` (`resurrect.py:116`) — pane id →
  `CLAUDE_CONFIG_DIR` for every live claude, via `ps eww`. Consumed by
  `window_view._pane_accounts` behind a **15s** TTL cache
  (`window_view.py:184`) — env is immutable for a process lifetime, so the
  long TTL is safe.
- `resolve_pids` (`pids.py:276`) — reuse stamped `@periscope_id` / rebind /
  mint, then GC. Rebind excludes `taken | carried` (invariant 13). Windows
  arrive with `pane_id` and `window_id` populated (`panes.py:341`).
- `list_claudes` (`channels.py:1202`) — machine-wide flat roster; no track
  annotation, no self-identification. The handler receives the caller's pane
  id, and resolves pids **on the event loop** before offloading capture to a
  thread (`channels.py:1217-1218`) — a latency constraint §2 must respect.
- Track membership: explicit `pane_tracks` tag else repo-default
  (`tracks.py:57`). `spawn_claude` already tags spawns — `workspace="same"`
  inherits the **caller's** track (`channels.py:768-771`), so a hub's fan-out
  is already a coherent track. Nothing exposes it to Claudes.
- **Resume preserves the session id on the current build** (verified
  2026-08-06: two live panes started `claude --resume <uuid>` report that
  same uuid as their current sessionId in `sessions/<pid>.json`). But
  CLAUDE.md's pane→session section documents id rotation on resume/compact
  from a real incident, so rotation must stay a handled case, not an assumed
  impossibility.

## 1. Session index covers every live account (Ship 1)

`_build_index` (`session_status.py:53`) globs `~/.claude/sessions/*.json`
only. Change: scan each live account's `<config_dir>/sessions/*.json` — the
union of `~/.claude` and every distinct dir the pane→config-dir scan reports.

**Dependency direction**: `session_status` documents itself as a stdlib-only
leaf (`session_status.py:24-25`), and `resurrect` already imports it — so
`session_status` cannot import `resurrect._pane_config_dirs`. Move the
config-dir scan (`_CONFIG_DIR_RE`, `_config_dir_from_ps`, `_pane_config_dirs`)
into `session_status`, cached at the 15s TTL it already effectively has in
`window_view` (env is immutable; 1s would add a fork/s for nothing).
`resurrect` and `window_view` import it from there — `resurrect` already
delegates its pane→pid walk to `session_status`, so this follows the
established direction.

**Cross-account pid collisions are real, not hypothetical**: within one dir, a
recycled pid *overwrites* the stale `<pid>.json` in place; across dirs, a
stale file in account A's dir persists alongside account B's live same-pid
file, and `_build_index`'s last-insert-wins merge (`session_status.py:60-69`)
has no tiebreak. The `_live_claude_pids()` guard checks that the pid is a live
claude — not *which file* described it. Fix: index entries carry their source
config dir; `by_pid` resolution prefers the file whose dir matches the pane's
`CLAUDE_CONFIG_DIR` (from the same scan), falling back to newest mtime when
the pane's dir is unknown. Without this tiebreak, a wrong sid would feed §2's
TTL-exempt pass 0 and reattach a 30-day-old entry's immunity fields —
invariant 12's exact failure mode through a new door.

Ships alone: it also repairs `turns.session_id_for_pane` and narrator session
tracking for secondary-account panes, which currently degrade to fallbacks.

## 2. Session id becomes rebind pass 0 (Ship 2)

**Sid resolution happens before the lock.** `_resolve_one` runs under
`_STATE_LOCK` (`pids.py:304`), and `list_claudes` runs the whole resolve on
the event loop — a cold `live_session_id_for_pane` forks `tmux list-panes` +
`ps` with multi-second timeouts. Those forks must not happen under the lock or
on the loop: the caller builds a `{pane_id: sid}` map first (one call per
window, all layers 1s-cached), then passes it into `resolve_pids`.
`_attach_git_then_resolve_pids` grows this step alongside its existing
branch-attach. Tests get simpler too — monkeypatch nothing, pass a dict.

Inside the pass, per window:

- **Record it**: `last_seen` gains `"sid"` and `"pane_id"` fields. Both
  participate in the `identity_changed` dirty check — a sid rotation or
  pane-id change is a real identity event worth a state.json write; neither
  happens at poll frequency.
- **Rebind pass 0**: match an entry whose `last_seen.sid` equals the window's
  live sid. Pass 0 ignores `_REBIND_TTL_S` and honors only the 30-day GC
  horizon: a session id is unique, so the collision risk that motivates the
  15-minute TTL (invariant 12) does not exist for this pass. Consequence
  worth naming: `claude --resume` of a days-old session in a fresh window
  reattaches its old identity — notes, `linked_pr`, `spawned_by` — where
  today it mints fresh.
- **Pass 0b — resume lineage**: when the live sid matches no entry, extract
  `--resume <uuid>` from the pane's claude argv (the machinery
  `pane_channel_ready` already uses to read claude argv) and match
  `last_seen.sid` against that uuid. Today this is usually redundant (resume
  preserves the id — see §"What exists today"), but it is the documented
  rotation case's safety net: if a resume ever mints a fresh sid, the argv
  still names the lineage. Same TTL exemption, same exclusions — **plus cwd
  corroboration**: the entry's `last_seen.cwd` must equal the window's cwd.
  The resume hint is a regex over `ps` argv, which flattens the whole command
  line INCLUDING any first-message prompt — without the gate, a fresh pane
  whose prompt merely mentions `claude --resume <uuid>` inherits a dead
  session's identity. The genuine cases (resurrect restore, move-account,
  resume tool) all resume in the transcript's own cwd, so the gate costs
  them nothing. Two residuals, accepted: a prompt quoting the uuid from
  *inside the same cwd* still matches (narrow, and the entry must also be
  orphaned); and any 0b match overwrites the entry's recorded sid with the
  new live sid — right for genuine rotation, lineage-destroying on a false
  match, and the two cases are indistinguishable, so don't "fix" the
  overwrite in either direction.
- Passes 1 and 2 are unchanged and remain the fallback for panes with no live
  claude (shells, codex panes, mid-restart gaps).
- Passes 0/0b still exclude `taken | carried`. Two live windows reporting the
  same sid should be impossible (one process, one pane); if it happens anyway,
  the exclusion keeps it from manufacturing duplicates and §4's logging makes
  it visible.

## 3. Duplicate arbitration by rightful owner (Ship 2)

New phase 0 in `resolve_pids`, before per-window resolution: group windows by
stamped `pid_raw`. For a pid on ≥2 windows, the keeper is the window whose
live sid matches the entry's recorded `last_seen.sid`; every other window gets
`pid_raw` cleared and resolves normally in phase 1 (rebind or mint, restamped
via the existing `pid != pid_raw` path at `pids.py:185`, window-id-targeted —
invariant 11 intact). No sid match on any window → first-in-list keeps it
(status quo), but the decision is logged either way.

`carried` (`pids.py:309`) is computed from the **raw stamps, before
arbitration clears losers** — the keeper retains the pid either way, so the
set is identical, but pinning the order keeps the reasoning simple. Known
residual, tolerated: a demoted window whose sid was never recorded can still
inherit a wrong `(branch, cwd)` entry in pass 2 — §4's logging makes that
visible instead of silent.

This replaces list-order luck with evidence: the keeper is the same window on
every poll regardless of enumeration order, so the 20:06/20:11 ping-pong
cannot recur.

## 4. Rebind and arbitration log every decision (Ship 2)

One `log.info` per rebind: pid, target, which pass matched (`sid` / `resume`
/ `session+name` / `branch+cwd`), and the matched entry's `last_seen`
summary. One per arbitration: pid, keeper, demoted windows, and whether sid
evidence decided it. The re-mint warning stays.

The field-report incident was undiagnosable post-hoc because the theft path
was silent (only the cleanup logged). `grep rebind` on the log becomes the
regression signal, alongside the existing `grep -c re-minting`.

## 5. `pane_tracks` re-keys to `@periscope_id` (Ship 3)

Track membership currently rides the least stable identifier in the system.
Re-key `pane_tracks` to hold the periscope pid, which Ship 2 makes durable
across restarts, resumes, and account swaps. **Rename the column to `pid`** so
any unconverted call site fails loudly at the SQL layer instead of silently
re-minting `%N` rows.

Read path:

- `tracks.resolve_track_for_window` reads `w.get("pid") or w.get("pid_raw")`
  — the pattern `narrator.py:279` already uses — because several callers hold
  raw `list_windows()` rows that carry only `pid_raw`: `narrator.py:421-426`,
  `routes/sessions.py:532` (via `_resolve_window_by_pid`, which returns raw
  rows per `channels.py:339-341`), and `tracks.teardown_targets`
  (`routes/tracks.py:88`). Callers with resolved rows (`window_view.py:412`,
  `open_ops.py:268`) read `pid` directly.
- `channels.py:770` (spawn `workspace="same"` inheriting the caller's track)
  passes a synthetic dict with no pid — it must pass the caller's resolved pid
  (`parent_pid`, `channels.py:725`) explicitly; when `parent_pid` is empty the
  spawn falls back to the repo-default track, logged.

Write/consume path — every site, not a sample (each miss silently re-creates
the bug this section removes):

| Site | Change |
|---|---|
| `channels.py:764,767` (spawn tags spawned pane) | tag by `stamp_new_window`'s returned pid |
| `channels.py:946` (resume tool tag) | mint/stamp pid first, tag by pid |
| `routes/sessions.py:349` | tag by pid |
| `routes/sessions.py:544` | tags `new_pane_id` *before* the pid mint at `:550` — reorder: mint, then tag |
| `tracks.seed_tracks:162` | key by pid |
| `tracks.migrate_workspaces_to_tracks:206` | reads `%N`-keyed `pane_workspace_map` and writes those keys back — convert through the live `%N→pid` map at migration time; unresolvable rows drop (they'd repo-default anyway) |
| `channels.py:1286` (`_do_list_workspaces_tool` tagged-tab counts) | liveness intersection moves from live pane ids to live pids |
| `routes/tracks.py:51-59` (move endpoint) | body field becomes `pid`; `Rail.jsx:173` sends `w.pid` (both fields are on the window object; drop handlers at `Rail.jsx:371,411` route through the same `moveTabToTrack`) |
| `activity.prune_pane_tracks` | takes the live **pid** set — see prune gating below |

**Prune gating**: boot housekeeping (`app.py:64-83`) currently prunes in a
`_bg` thread before any resolve pass. Post-tmux-restart the windows are
unstamped, so "live pids" is unknowable until rebind runs — pruning then
would delete every track row at exactly the moment Ship 2's rebind could
reattach them. Prune moves to after a completed resolve pass (using its
`taken` set) and never runs before the first one.

**Migration**: key formats are disjoint (`%N` vs 8-lowercase-hex), so migrate
lazily inside resolve phase 1 — when a window's `pane_id` has a `%`-keyed row
and its pid has none, insert the pid row and delete the `%` row. One
`pane_track_map()` read per pass, not per-window SELECTs (`activity`'s single
`_LOCK` over a `check_same_thread=False` connection makes cross-thread reads
safe, and no `activity._LOCK → _STATE_LOCK` path exists, so writing under
`_STATE_LOCK` cannot deadlock). After the first full poll, remaining
`%`-keyed rows belong to panes that no longer exist; a sweep on the *next*
boot deletes `%`-keyed leftovers.

Rejected alternative — keep `%N` keying and migrate rows on rebind: keeps two
keying schemes alive (`tabs_by_track` values are already pids) and leaves the
tag's fate coupled to prune ordering forever, instead of only through the
one-boot migration window.

## 6. Track-aware channel tools (Ship 3)

The field report's frictions #3 and #7 are both "no roster": panes
reconstructed *who is in my program* from names, cwds, and commit subjects.
The track is the roster unit — it already spans a program's worktrees, and
`spawn_claude` already tags membership. Expose it:

- **Every `list_claudes` row gains** `"track": {"id", "label", "kind"}` via
  `resolve_track_for_window` + `track_label` / `track_kind` — data the
  dashboard poll already computes per window. The loose bucket surfaces as
  `{"id": "loose", "label": "loose", "kind": "loose"}` rather than null, so
  "same track" comparisons never need a null guard. (These calls run in
  `_collect`'s thread — safe per `activity`'s threading model, §5.)
- **The response gains** `"you": {"handle", "track"}` — the caller resolved by
  matching its pane id (the `pane` argument the handler receives) in the
  already-resolved windows list. This also closes a gap the report tripped
  on: a Claude currently has no way to learn its *own* handle, which is half
  of why the identity merge was so disorienting. Response-level, not
  MCP-instructions-level, because tracks change mid-session. Edge:
  `list_windows` reports only each window's *active* pane (`panes.py:326-331`)
  — a caller in a background split pane gets `"you": null`, and the tool
  description says so rather than pretending it can't happen.
- **`list_claudes` accepts** `track: "mine" | <track id>` — filter rows to
  one track (`"mine"` = the caller's; refused with a clear error when `you`
  is null). The machine-wide default is unchanged. Loose callers match loose
  rows under `"mine"`.
- `spawn_claude` / `resume_session` results gain the spawned pane's `track` —
  the caller learns where its fan-out landed without a follow-up call.
- Tool descriptions for `send_to` / `spawn_claude` note that
  `workspace="same"` inherits the caller's track, so a hub can assert "my
  spawns are my roster" without ceremony.

Ordering note: §6 without §2–§5 would expose a roster that drops members on
every restart (the `pane_tracks` rotation). It ships last.

## Not doing (follow-ups from the field report, deliberately out of scope)

- **Name-based `send_to`** — orthogonal to identity keying; smaller after §2
  makes handles stop rotating.
- **Sender handle / `reply_to` on channel messages** — channel-payload shape
  change, separate spec.
- **Delivery receipts** — partially addressed by `960751d` (peek shows channel
  turns; `send_to` reports queued); the remaining ask is a receipt flag.
- **Doc-ownership hints** — real design, and the hand-rolled header convention
  is holding.
- **Softening the untrusted-channel framing for same-track peers** — §6 makes
  the trust boundary legible; the policy change is its own decision.
- **Contention/claims** — `docs/specs/inter-claude-contention.md` (parked).

## Testing

- `tests/test_pids.py`: pass-0/0b rebind (sid match beats session/name and
  branch/cwd matches; resume-lineage match; TTL-exempt; `taken | carried`
  exclusion), duplicate arbitration (sid evidence picks the keeper regardless
  of list order; no-sid falls back to first-in-list), `last_seen`
  sid/pane_id dirty semantics. Sid maps are passed into `resolve_pids` as
  plain dicts — no monkeypatching of process walks needed.
- `tests/test_session_status.py`: multi-dir `_build_index` (two config dirs,
  merged indexes, per-dir source recorded, config-dir tiebreak beats
  last-insert order, mtime fallback, unreadable dir skipped).
- `tests/test_tracks.py` + `tests/routes/test_tracks.py`: pid keying, lazy
  migration (a `%`-keyed row converts on sight; pid row wins when both
  exist), prune gated on a completed resolve, move endpoint takes `pid`,
  `teardown_targets` resolves tagged panes from raw rows (`pid_raw`).
- `tests/test_channels.py`: `track` on rows, `you` on the response (including
  the background-split-pane null), `track` filter (mine / explicit id /
  omitted / mine-with-null-you refusal), spawn/resume results carry `track`.
- The identity-merge scenario end-to-end: two same-worktree windows, restart
  rotates pane ids and session names, sid-carrying entries reattach to the
  right windows, track tags survive. Existing fixtures compose: `clean_state`
  + `fresh_activity_db`, and the autouse live-scan guards in
  `tests/conftest.py` already keep real process walks out.

## Risks

- **Sid unreadable mid-restart**: a pane whose claude hasn't rewritten
  `sessions/<pid>.json` yet resolves via passes 1–2, same as today. Pass 0 is
  additive; degradation is to the status quo, not below it.
- **Session-id rotation** (documented on resume/compact, though resume
  currently preserves the id): pass 0 misses, pass 0b catches the resume
  case via argv; a compact-rotation inside a stamped window never reaches
  rebind at all (the window keeps its `@periscope_id`; the next poll
  re-records the new sid). Arbitration during exactly a rotation degrades to
  first-in-list, logged.
- **Stale cross-account session file**: §1's config-dir tiebreak prevents a
  recycled pid's stale file from feeding pass 0; the mtime fallback covers
  panes whose config dir the `ps` scan missed.
- **Fork latency**: all subprocess work (sid map, config-dir scan) happens
  before `_STATE_LOCK` and off the event loop; the 15s config-dir cache keeps
  `ps eww` at its current cadence.
- **Migration deletes a live tag**: only `%`-keyed rows whose pane no longer
  appears in any poll are swept, and only on the boot after the migration
  poll — a live pane's row converts on first sight instead.
- **Duplicate recorded sids across entries** (e.g. move-account leaves the
  killed window's entry carrying the sid its successor also records): a
  TTL-exempt pass-0 rebind picks among them in dict order for up to the GC
  horizon. Accepted — the competing entries carry near-identical identity
  fields, so the practical difference is which notes/links reattach.
- **§6 deviation, deliberate**: the workspace="same"-inherits-track note
  landed on `spawn_claude`'s description only — `send_to` doesn't spawn, so
  the sentence had nothing to attach to there.
