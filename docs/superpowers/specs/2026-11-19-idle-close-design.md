# Idle close: close idle Claude panes at night, resume them later

Status: draft for review. Prototypes: the "Idle close prototypes" canvas
(https://claude.ai/artifact/1KCaANrU4beN77zNbiJAYt).

## Goal

An idle Claude pane keeps its memory until you close it. You keep idle panes
open because you want to come back to them, and so the RAM fills. Idle close
closes a Claude pane after a number of idle weekdays. It keeps a record so that
you can resume the pane later, in its track, with its name, PR and ticket.

## Decisions already made

- The setting is a number of days.
- The sweep runs once a night, at 04:00.
- Saturday and Sunday do not count toward idle time.
- Pinned panes stay open. Panes that wait for you stay open.
- A closed pane stays resumable while Claude keeps its transcript (30 days
  after the last write, Claude's default `cleanupPeriodDays`).
- The closed list lives in ▣ › closed (option C). It is one view of history.
- Past sessions open in the detail area's Transcript tab.
- Ship order: this server part first, then the history view.

## Scope

In (phase 1, this spec):

- The `idle_close_days` setting and its row in `SettingsModal`.
- The sweep: a prod-only lifespan task, like `poke.run()`.
- The closed record: a `closed_panes` table in the activity DB.
- Resume puts the pane back in its track.
- A preview: the panes that close tonight.
- `/api/history` rows show when idle close closed the session.

Out (phase 2, its own spec): the history view in the dashboard, ▣ › closed,
the morning toast, the ⌘K "Closed" group, the Transcript tab for archived
sessions.

Out (not planned):

- Codex panes. Their resume path is different (`_window_new_codex_resume`).
- Shell panes, and the shell window that a worktree spawn opens next to Claude.
- A record for a pane that you kill by hand (× kill).
- The wrapper profile on resume (see "Known gaps").

## Terms

- **Idle since**: the time when the pane was last used. See "Idle time".
- **Weekday hours**: the hours between two times that fall on Monday to Friday,
  in local time.
- **The sweep**: the 04:00 pass that closes panes.
- **Closed record**: one `closed_panes` row for each pane that the sweep closed.

## 1. The setting

`idle_close_days: int` goes in `Settings` (`periscope/store.py:106`), under
`state['settings']`.

- `PATCH /api/settings` accepts an integer from 1 to 30.
- Unset means off. `update_settings` pops null keys, so null also means off
  (the same rule as `poke_at`).
- 04:00 is a constant, `SWEEP_AT`. It is not a setting, because nothing asks for
  another time.

`SettingsModal` gets one row:

```
Close idle Claude panes after [ 3 ] weekdays     [off]
2 panes close at 04:00 tonight: notify-buttons, bq-cost-guard
```

The second line comes from the preview (section 6). It shows only when the
setting is on.

## 2. Idle time

Idle since is the latest of these times:

1. The last write to the session's transcript (`jsonl_for_session`, mtime).
2. The end of the last turn (`completed_at`, kept in `state.json`).
3. Your last action on the pane in periscope (`acked_at`, kept in `state.json`).

Times that are missing do not count. The transcript catches turns that ran while
periscope was down. `acked_at` catches a pane that you opened in periscope but
did not type in. `focused_at` does not count: it is kept only in memory, and it
changes when you switch windows in a terminal (see decision D4).

A pane closes when its weekday hours since idle since are equal to or more than
`idle_close_days × 24`.

*Example:* the setting is 3 weekdays (72 hours). You last used a pane on Monday
at 17:00. On Thursday at 04:00 it has 59 weekday hours (7 + 24 + 24 + 4), so it
stays open. On Friday at 04:00 it has 83, so it closes.

`weekday_hours(start, end)` is a pure function on naive local datetimes. A
daylight saving change can move the result by one hour. That is acceptable for
a nightly pass.

## 3. The sweep

New module `periscope/idle_close.py`, in the same shape as `poke.py`:

- `due(now, settings, log) -> bool` is pure. It is true from 04:00 to 05:30
  (a 90-minute grace, like `poke_grace_min`), when the setting is on, and when
  `idle_close_log` has no entry for today.
- `closable(panes, now, days, pinned) -> list[...]` is pure. The sweep and the
  preview both use it.
- `_tick()` reads the caches. It gives the work to a thread through `_bg`.
- `run()` is a lifespan task, prod only. It ticks every 60 seconds.

If the Mac sleeps through 04:00 to 05:30, the sweep skips that night (decision
D3). Otherwise it would close panes at 08:30, while you sit down to use them.

A pane is closable when all of these are true:

- `parse_pane` says the agent is `claude`.
- Its state is `idle`. A `working` or `needs-input` pane stays open.
- It is not done-but-unread, which is `completed_at > acked_at` (decision D1).
- Its pid is not in `prefs.ui.pinned_pids` (`static/src/prefs.js:403`).
- Its weekday hours since idle since are equal to or more than the limit.

For each closable pane, the sweep does these steps in this order:

1. Write the closed record (section 4).
2. `kill-pane -t <pane_id>`. Use the stable pane id, not `session:index`,
   because `renumber-windows on` moves the indexes (as in
   `periscope/routes/cleanup.py:84`). Then call `drop_target_focus`.
3. Index the session into history with `index_one(jsonl_path, force=True)`
   (`history/indexer.py:233`), in a background thread. Nobody has confirmed
   that Claude's `SessionEnd` hook fires when tmux kills the pane. This step
   puts the session in history in both cases.

Then the sweep writes `idle_close_log = {date, at, closed: [session ids],
mem_mb}` to `state.json`. Phase 2's morning toast reads it.

The tmux-continuum save runs on its own interval (`resurrect.save_now()`). A
reboot after 04:00 but before the next save can bring a closed pane back. The
pane resumes its session, and the next night's sweep closes it again.

## 4. The closed record

A new table in the activity DB (`periscope.db`), beside `pane_tracks`:

```sql
CREATE TABLE IF NOT EXISTS closed_panes (
  session_id     TEXT PRIMARY KEY,  -- Claude session id
  pid            TEXT NOT NULL,     -- @periscope_id of the closed pane
  name           TEXT,
  track_id       TEXT,
  cwd            TEXT,
  branch         TEXT,
  linked_pr      INTEGER,
  linked_linear  TEXT,
  account        TEXT,
  mem_mb         INTEGER,           -- footprint at close, from resources.measure
  idle_since     INTEGER NOT NULL,
  closed_at      INTEGER NOT NULL
);
```

The record keeps a copy of these fields because the live sources do not
survive:

- `prune_pane_tracks` (`periscope/activity.py:501`) deletes the track of a dead
  pid at each boot.
- The `state.json` entry is garbage-collected when it has no field from
  `_IMMUNITY_FIELDS` (`periscope/pids.py:151`).

A record has no close reason. The sweep is its only writer.

A record lives while Claude keeps the transcript:

- Each sweep deletes the rows for which `jsonl_for_session` finds no file.
- A resume deletes the row (section 5).

## 5. Resume

All resumes go through `_window_new_resume` (`periscope/routes/sessions.py:152`).
That route gets one change: after the spawn, if a closed record exists for
`resume_id`, it does these steps:

1. Tag the new pane with the record's track (`set_pane_track`,
   `periscope/activity.py:473`).
2. Delete the record.

The history page and the MCP tool `resume_session` (`periscope/channels.py:983`)
use this route, so both put the pane back in its track. Phase 2's ⌘K group will
use it too. Move-account also uses it, but a live pane has no closed record.

Pass 0b (`periscope/pids.py:96`) binds a resumed session to its old
`state.json` entry by session id. Thus the name, PR and ticket come back. The
implementation must verify this with a test.

The resumed pane opens in the tmux session "resumes". The rail groups panes by
track, not by tmux session, so the pane shows in its track.

The route's account rule does not change: `usage.choose_launch(account)` picks
the account (`docs/account-routing.md`).

When Claude has no transcript, the route refuses with 404. The `/history` P1 fix
(branch `thibaultdody/history-p1-fixes`) adds this check. Idle close uses the
same check.

## 6. API

- `PATCH /api/settings`: accepts `idle_close_days` (section 1).
- `GET /api/idle-close/preview`: the panes over the limit at the next 04:00,
  each with name, idle since, weekday hours, `mem_mb` and the verdict:
  `closes`, `stays · pinned`, `stays · waits for you` or
  `stays · weekend skipped`. It uses `closable`. The settings row shows its
  count and names.
- `/api/history` rows: a `closed_at` field when a closed record exists. The
  current `/history` page shows "closed Thu 04:00" on those rows.

`GET /api/closed` and the morning toast's state field are in phase 2.

## 7. Tests

Add cases to the existing modules (`docs/testing.md`). The index thread must
be patched in every test, because a real thread that touches the activity DB is
forbidden.

- `weekday_hours`: same day; Friday to Monday; a start on Saturday; an end on
  Sunday.
- `due`: before 04:00, in the grace, after the grace, setting off, logged today.
- `closable`: a pinned pane; `needs-input`; `working`; done-but-unread; a shell
  pane; a pane under the limit only because of the weekend.
- The sweep: the record is written before the kill; the kill uses the pane id;
  `index_one` is called with `force=True`; the log is written.
- Retention: the sweep deletes a row when the transcript is missing.
- Resume: with a record, the track is set and the row is deleted; with no
  record, nothing changes.
- `PATCH /api/settings`: 0, 31 and a string are refused; null turns it off.

## 8. Known gaps

- **Wrapper profile**: `_window_new_resume` does not pass a profile, so a
  resumed pane runs the default profile. This is true today for history's
  resume too. It gets a Linear ticket, not a workaround here.
- **Background shells**: an idle Claude can own a background shell, for example
  a dev server that it started. Periscope has no signal for this. The sweep
  kills it with the pane.
- **Second account**: `jsonl_for_session` looks only in `~/.claude/projects`.
  The move-account docstring says another account's `projects` folder is a
  link to it. `~/.claude-b/projects` does not exist on this machine, so this is
  not tested here.

## 9. Decisions made in review

- **D1. Done-but-unread panes.** Decided on 19 Nov: keep them open. They show
  as news on the rail, and they wait for you to read them, like a `needs-input`
  pane.
- **D2. The default.** Decided on 19 Nov: off. Other installs get this feature
  from a release, and it kills panes.
- **D3. The Mac sleeps through 04:00.** Decided on 19 Nov: skip that night,
  like the poke. Result: on a Mac that sleeps every night at 04:00, no pane
  closes.
- **D4. What counts as use.** Decided on 19 Nov: transcript writes, turn ends
  and your actions in periscope. Terminal focus (`focused_at`) does not count,
  because a restart clears it.

## 10. Assumptions to check in the implementation

- An idle Claude has written every turn to its transcript, so `kill-pane` loses
  nothing. Check: kill an idle test pane, then compare the transcript before
  and after.
- Pass 0b binds a resumed session to its old `state.json` entry.
- `resources.measure` gives a footprint for each pane id at 04:00, when no
  browser polls `/api/state`.
