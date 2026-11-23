# Idle close: close idle Claude panes at night, resume them later

Status: draft for review. Prototypes: the "Idle close prototypes" canvas
(https://claude.ai/artifact/1KCaANrU4beN77zNbiJAYt).

## Goal

An idle Claude pane keeps its memory until you close it. You keep idle panes
open because you want to come back to them, and so the RAM fills. Idle close
closes a Claude pane after a number of idle weekdays. It keeps a record so that
you can resume the pane later, in its track, with its name, PR and ticket.

## Decisions already made

- The setting is a number of weekdays.
- The sweep runs once a night, at 04:00.
- Saturday and Sunday do not count toward idle time.
- Pinned panes stay open. Panes that need you, and panes with a result that
  you did not read, stay open.
- A closed pane stays resumable while Claude keeps its transcript (30 days
  after the last write, Claude's default `cleanupPeriodDays`).
- The closed list lives in ▣ › closed (option C). It is one view of history.
- Past sessions open in the detail area's Transcript tab.
- Ship order: this server part first, then the history view.

## Dependency

PR #67 (THI-467) adds `turns.resume_refusal`, the one check that refuses a
resume. It returns 404 when Claude has no transcript. Idle close uses the same
check. Merge PR #67 first.

## Scope

In (phase 1, this spec):

- The `idle_close_weekdays` setting and its row in `SettingsModal`.
- The sweep: a prod-only lifespan task, like `poke.run()`.
- The closed record: a `closed_panes` table in the activity DB.
- Resume puts the pane back with its pid, track, name and wrapper profile.
- A preview: the panes that close tonight, and why the others stay.
- `/api/history/search` rows show when idle close closed the session.

Out (phase 2, its own spec): the history view in the dashboard, ▣ › closed,
the morning toast, the ⌘K "Closed" group, the Transcript tab for archived
sessions.

Out (not planned):

- Codex panes. Their resume path is different (`_window_new_codex_resume`).
- Shell panes, and the shell window that a worktree spawn opens next to Claude.
- A record for a pane that you kill by hand (× kill).

## Terms

- **Idle since**: the time when the pane was last used. See "Idle time".
- **Weekday hours**: the hours between two times that fall on Monday to Friday,
  in local time.
- **The sweep**: the 04:00 pass that closes panes.
- **Verdict**: what the sweep does with one pane: the code `closes`, or the
  code of the reason it stays.
- **Closed record**: one `closed_panes` row for each pane that the sweep closed.

## 1. The setting

`idle_close_weekdays: int` goes in `Settings` (`periscope/store.py:106`), under
`state['settings']`. The name says weekdays because `cleanup_idle_days`, in
the same block, counts calendar days.

- `PATCH /api/settings` accepts an integer from 1 to 30. `SettingsPatch`
  declares the field `StrictInt | None`, so Pydantic refuses JSON `true` and
  `"3"` with 422. A lax `int` reads `true` as 1, the most aggressive value.
  The route refuses 0 and 31 with 400.
- Unset means off. `update_settings` pops null keys, so null turns it off.
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
2. The end of the last turn (the view's `completed_at`).
3. Your last action on the pane in periscope (the view's `acted_at`).

The views come from `state_hub.current_windows()` (section 3). The view takes
the larger of the in-memory and the persisted stamp for 2 and 3
(`periscope/window_view.py:435`). The transcript catches turns that ran while
periscope was down. `acted_at` catches a pane that you opened in periscope but
did not type in. `focused_at` does not count: it is kept only in memory, and it
changes when you switch windows in a terminal (see decision D4).

Idle since is `None` when none of the three times is set. Section 10 predicts
this pane: a Claude that you started from a terminal and never prompted. Such
a pane stays open, and the preview leaves it out (section 3).

A pane closes when its weekday hours since idle since are equal to or more than
`idle_close_weekdays × 24`.

*Example:* the setting is 3 weekdays (72 hours). You last used a pane on Monday
at 17:00. On Thursday at 04:00 it has 59 weekday hours (7 + 24 + 24 + 4), so it
stays open. On Friday at 04:00 it has 83, so it closes.

`weekday_hours(start, end)` is a pure function on naive local datetimes. A
daylight saving change can move the result by one hour. That is acceptable for
a nightly pass.

## 3. The sweep

New module `periscope/idle_close.py`, in the same shape as `poke.py`.

### Pure functions

- `idle_since(transcript_mtime, completed_at, acted_at) -> int | None`: the
  latest of the three. A 0 or `None` stamp does not count. With no stamp that
  counts, the result is `None`.
- `verdict(pane, at, weekdays, pinned) -> str | None` returns a code. The
  checks read `pid`, `session_id`, `state`, `panes`, `idle_since` and
  `transcript_mtime`. In this order:
  1. Idle since is `None`, or the wall-clock hours since idle since are less
     than `weekdays × 24`: `None`. The pane is not near the limit, and the
     preview leaves it out.
  2. No session id, or no transcript: `no-transcript`. Resume refuses such a
     session with 404, so a record for it can never resume.
  3. The pid is in `pinned`: `pinned`.
  4. The window has more than one pane: `split-window`. `@periscope_id` is a
     window option, so after `kill-pane` the window keeps the closed pane's
     pid, PR and ticket, and a resume could not take them back.
  5. The state is not `idle`: the state, for example `needs-input`, `done`
     (decision D1) or `working`.
  6. Weekday hours are equal to or more than the limit: `closes`.
  7. Otherwise: `weekend`.
- `due(now, settings, log) -> bool`: true from `SWEEP_AT` to `SWEEP_AT` + 90
  minutes (like `poke_grace_min`), when the setting is on, and when
  `idle_close_log` has no entry for today.

One function builds the `pane` dict from a view:
`pane_from_view(view) -> dict | None`. It gives `None` for a codex or shell
view. The dict has the fields that `verdict` reads, plus `pane_id`, `name`
and `mem_mb` for the record and the preview, and `profile` for the record.
The session id comes from
`turns.session_id_for_pane(pane_id)`, and the transcript mtime from
`jsonl_for_session`. `mem_mb` is `None` when the view's `res` is `None`: the
process snapshot failed, or the pane is not in it (`periscope/resources.py:263`).
`profile` is `config.profile_env(view["profile"]) or None`. The view reads the
profile from the live Claude's environment (`session_status.pane_profiles()`,
`periscope/window_view.py:586`), so the sweep adds no `ps` call. `None` is the
default profile.
For the window's pane count, add `window_panes` to `tmux.PANE_FIELDS`, and
copy it to the `list_windows` row and the view as `panes`.

The sweep and the preview both use `pane_from_view` and `verdict`. The view's
state is already `done` when `completed_at > acted_at`
(`periscope/window_view.py:438`), so `verdict` needs no separate rule for an
unread result.

The server sends codes, and the frontend forms the words, as
`static/src/notify/copy.js:117` does for `needs-input`. In phase 2,
`needs-input` reads "stays · needs you", `no-transcript` reads "stays · no
transcript", `split-window` reads "stays · split window", `weekend` reads
"stays · weekend skipped", and each other code reads "stays · <code>".

### The tick

`run()` is a lifespan task, prod only. It calls `_tick()` every 60 seconds.
`_tick()` is async:

1. If `due` is false, return.
2. Write `idle_close_log = {date, at}` to `state.json`.
3. `views = await state_hub.current_windows()`. This gives the last dashboard
   build while a tab is open, or a fresh `build_state()` when no tab is open
   (the usual case at 04:00).
4. Give the work to a thread: `_bg("idle-close", sweep, views)`.

The log comes before the work. Thus a failing pane, or a sweep that runs
longer than one tick, cannot start a second sweep. If the sweep crashes, that
night is lost, as when the Mac sleeps through 04:00 (decision D3). The log has
no list of closed panes: phase 2's morning toast reads `closed_panes` where
`closed_at` is after today's `SWEEP_AT`.

If the Mac sleeps through 04:00 to 05:30, the sweep skips that night (decision
D3). Otherwise it would close panes at 08:30, while you sit down to use them.

### The sweep worker

`sweep(views)` runs in the `_bg` thread:

1. Retention (section 4).
2. Build the `pane` dicts with `pane_from_view`. The pinned pids come from
   `store.get_ui()["pinned_pids"]`, which the dashboard's pin writes
   (`static/src/prefs.js:403`). The memory comes from the view's `res`;
   `build_state` runs `resources.measure` on each call.
3. For each pane with the verdict `closes`, in its own `try`, so that one
   failing pane does not stop the others:
   1. Check the pane again, because the views can be up to 13 seconds old,
      and a turn can start in that time. Skip the pane if one check fails:
      - `pane_id` still exists in tmux.
      - Its window still has one pane.
      - `session_id_for_pane(pane_id)` gives the same session id.
      - `session_status.session_state_for(session_id)` still says `idle`.
      - The transcript mtime did not change. Only this check sees a short
        turn that started and ended after the snapshot.
   2. Write the closed record (section 4).
   3. `kill-pane -t <pane_id>`. Use the stable pane id, not `session:index`,
      because `renumber-windows on` moves the indexes (as in
      `periscope/routes/cleanup.py:84`). The window has one pane, so the
      kill closes the window too. Then call `drop_target_focus`. If the kill
      fails, delete the record and log a warning.
4. If a pane closed, call `resurrect.save_now(force=True)`. Otherwise a reboot
   before the next continuum save brings the closed panes back. A plain
   `save_now()` does not save here: `continuum_save.sh` saves only when
   `@continuum-save-interval` (15 minutes) has passed since
   `@continuum-save-last-timestamp`, and the activity worker calls it every
   30 seconds. `force=True` unsets that timestamp
   (`set-option -gu @continuum-save-last-timestamp`), so the script reads 0
   and saves. The script then writes a new timestamp, so the worker's
   15-minute cadence continues. The option name stays in `resurrect.py`.
5. For each closed session, call `index_one(jsonl_path, force=True)`
   (`history/indexer.py:233`) inline, and log an exception, as
   `history/hook.py:49` does. Nobody has confirmed that Claude's `SessionEnd`
   hook fires when tmux kills the pane. This step puts the session in history
   in both cases.

## 4. The closed record

A new table in the activity DB (`periscope.db`), beside `pane_tracks`:

```sql
CREATE TABLE IF NOT EXISTS closed_panes (
  session_id   TEXT PRIMARY KEY NOT NULL,  -- Claude session id
  pid          TEXT NOT NULL,              -- @periscope_id of the closed pane
  mem_mb       INTEGER,                    -- footprint at close, NULL if unknown
  profile      TEXT,                       -- wrapper profile, NULL for the default
  idle_since   INTEGER NOT NULL,
  closed_at    INTEGER NOT NULL
);
```

The sweep writes the record with `INSERT … ON CONFLICT(session_id) DO UPDATE`,
as `set_pane_track` does (`periscope/activity.py:478`). A session can close
twice: for example, you resume it with `claude --resume <id>` in a terminal,
and it goes idle again.

The record keeps nothing that a live store keeps:

- The name, branch, cwd, PR and ticket stay in the pid's `state.json` entry
  (`store.get_window(pid)`: `last_seen.name`, `last_seen.branch`,
  `last_seen.cwd`, `linked_pr`, `linked_linear`).
- That entry outlives the record. `_gc_windows` (`periscope/pids.py:285`)
  deletes an entry only when it has no field from `_IMMUNITY_FIELDS` and its
  `last_seen` is more than 30 days old. The record lives at most 30 days after
  the last transcript write, which is before the last sighting.
- The track tag stays in `pane_tracks`. `prune_pane_tracks`
  (`periscope/activity.py:501`) deletes the tag of a dead pid at each boot. It
  skips the pids that have a `closed_panes` row (`AND pid NOT IN (SELECT pid
  FROM closed_panes)`; both tables are in the activity DB), as
  `_IMMUNITY_FIELDS` keeps a `state.json` entry. After retention deletes a
  row, the next boot prunes its tag.

The wrapper profile is in the record because no store keeps it. It lives only
in the environment of the live Claude (`docs/wrapper-profiles.md`), and
`kill-pane` ends that process.

A record has no close reason. The sweep is its only writer.

The session came back when a live window wears the record's pid, or a live
Claude runs its session. `came_back(record, live_pids) -> bool` asks this
question. `live_pids` is the `pid_raw` set of `list_windows()`, as
`resolve_pids` builds `carried` (`periscope/pids.py:488`). The session check
reads the live session files, as `session_status.claude_proc_for` does. The
two checks catch both outcomes of a `claude --resume` that you typed: Pass 0b
rebinds its window to the record's pid, and Claude keeps or changes the
session id. (`periscope/session_status.py:200` and `periscope/turns.py:34`
disagree on whether Claude keeps it.)

Retention, `prune_closed(live_pids)`, runs at boot and at the start of each
sweep. It deletes:

- The rows for which `jsonl_for_session` finds no file.
- The rows for which `came_back` is true. The session came back by another
  path, such as a reboot or a `claude --resume` that you typed.

A resume also deletes the row (section 5).

## 5. Resume

`_window_new_resume` (`periscope/routes/sessions.py:137`) gets one change.
When a closed record exists for `resume_id`, after the refusal checks:

1. If `came_back(record, live_pids)` is true (section 4), delete the record
   and resume as today, with a fresh pid. A live window already wears the
   record's pid, or a live Claude runs the session. A second window with
   that pid would make two windows share one `@periscope_id`, and
   `_arbitrate_duplicates` (`periscope/pids.py:363`) would take the PR,
   ticket and name pin from one of them.
2. Read the name from the record's `state.json` entry (`last_seen.name`).
   Do this before the spawn: the next poll writes the new window's name over
   it.
3. Open the window with `-n <name>` and stamp `record.pid` in the same tmux
   command:
   `new-window … -n <name> -P -F '#{window_index}' ; set-option -w @periscope_id <record.pid>`.
   The `;` is its own argv item. Do the same for `new-session`. Skip `-n` when
   `narrator.is_placeholder_name(name)` is true. The pane name is the tmux
   window name (`periscope/panes.py:407`), so `-n` restores it.
   - Why one command: `_tmux_mutate` kicks the state hub after
     `new-window` (`periscope/tmux.py:200`). A dashboard build that lists the
     window before a separate stamp mints a fresh id, and stamps it after
     `record.pid` (`periscope/pids.py:241`). Then the pane comes back without
     its PR, ticket, name pin and track, and nothing repairs it. tmux runs
     the chained commands in one queue, so no `list-windows` sees the window
     unstamped.
   - Checked with tmux 3.6a on a private server: the chained `set-option -w`
     lands on the new window after `new-window` without `-d` and after
     `new-session -d`. After `new-window -d` it lands on the old current
     window. The resume's `new-window` has no `-d`; keep it that way.
   - `_resolve_one` keeps a stamped id (`periscope/pids.py:195`). Thus the
     window gets the old `state.json` entry with its PR, ticket and name pin.
     No rebind is needed, so the cwd does not need to match.
   - Pass the record's profile, `env_args(config_dir, record.profile)`, on
     both branches. The pane then comes back on its plugin set. Do not pass
     the model: `--resume` restores the session's own model, and
     `ANTHROPIC_MODEL` would override a later `/model`
     (`docs/wrapper-profiles.md`). Resurrect does the same
     (`periscope/resurrect.py:203`).
   - On the `new-session` branch, scrub the session env when `config_dir` or
     the profile is set, as the new-pane route does
     (`periscope/routes/sessions.py:346`). `new-session -e` sets the session
     env, so without the scrub every later resume into "resumes" runs that
     profile.
4. The window wears `record.pid`, so its track tag in `pane_tracks` applies
   again (section 4). `resolve_track_for_window` ignores a tag whose track is
   archived (`periscope/tracks.py:73`), so resume needs no track step.
5. Delete the record. Return `pid` in the result.

The callers:

- The dashboard route (`periscope/routes/sessions.py:524`), which the history
  page uses, returns the pid.
- The MCP tool `resume_session` (`_do_resume_session_tool`,
  `periscope/channels.py:985`) stamps a fresh pid today. Change it to
  `result.get("pid") or stamp_new_window(target)`. An explicit `workspace_id`
  still tags the pane into that track.
- Move-account (`periscope/routes/sessions.py:589`) moves a live pane, so
  `came_back` is true for its session. Step 1 deletes a stale record, and the
  pane keeps its fresh pid.
- Phase 2's ⌘K group will use the dashboard route.

The resumed pane opens in the tmux session "resumes". The rail groups panes by
track, not by tmux session, so the pane shows in its track.

The route's account rule does not change: `usage.choose_launch(account)` picks
the account (`docs/account-routing.md`).

## 6. API

- `PATCH /api/settings`: accepts `idle_close_weekdays` (section 1).
- `GET /api/idle-close/preview`: one row for each Claude pane for which
  `verdict(pane, next SWEEP_AT, …)` is not `None`. Each row has the name, idle
  since, weekday hours, `mem_mb` and the verdict code. The preview uses the
  current state of each pane; the sweep decides again at 04:00. The settings
  row shows the count and names of the `closes` rows. With the setting off,
  the response is an empty list.
- `/api/history/search` rows: a `closed_at` field when a closed record exists.
  The current `/history` page shows "closed Thu 04:00" on those rows.

`GET /api/closed` and the morning toast are in phase 2.

## 7. Tests

Put the tests in new files, as the mirror rule in `CLAUDE.md` places a new
module's tests: `tests/test_idle_close.py` and `tests/routes/test_idle_close.py`
(`tests/test_poke.py` is the precedent).

Thread isolation (`docs/testing.md`): two threads are a risk. The sweep writes
the activity DB. `index_one` writes the real `~/.claude/history.db`, copies
into `~/.claude/projects-archive` and can call Haiku. Add an autouse neuter of
`idle_close._bg` to `_no_plan_usage_refresh` in `tests/conftest.py`, next to
poke's. That one guard stops both threads, because the sweep calls `index_one`.
Tests that run `sweep` call it directly and patch `index_one`. The `_tick`
tests patch `state_hub.current_windows` with an async stub: with no fresh
build, it runs `build_state` in a thread (`periscope/state_hub.py:72`).

- `weekday_hours`: same day; Friday to Monday; a start on Saturday; an end on
  Sunday; the section 2 example (Thursday 04:00 gives 59, Friday 04:00 gives
  83).
- `idle_since`: the transcript mtime wins when it is the newest; a 0 stamp does
  not count; `idle_since(None, 0, 0)` is `None`.
- `pane_from_view`: a view whose `focused_at` is newer than every other stamp
  gives the same `idle_since` as without it (decision D4); a codex or shell
  view gives `None`; a view with `res` `None` gives `mem_mb` `None`, and the
  pane can still close; a view with the profile `full` gives `full`, and
  `default` gives `None`.
- `due`: before 04:00, in the grace, after the grace, setting off, logged today.
- `verdict`: under the limit gives `None`; idle since `None` gives `None`;
  exactly `weekdays × 24` weekday hours gives `closes`; no session id; no
  transcript; an account-B session whose transcript `jsonl_for_session` does
  not find; pinned; a window with 2 panes gives `split-window`;
  `needs-input`; `done`; `working`; a pane over the limit only in wall-clock
  hours gives `weekend`.
- `_tick`: when due, it writes the log and hands one sweep to `_bg`; when not
  due, nothing (`tests/test_poke.py:208`). When the stub raises, the log is
  written, and a second `_tick` at the same frozen time hands nothing to
  `_bg`.
- The sweep: the record is written before the kill; the kill uses the pane id;
  a failed kill deletes the record; one failing pane does not stop the others;
  a pane with no stamp beside a closable pane: the closable pane closes. At
  the re-check, the pane is skipped, with no record and no `kill-pane`, when
  its session id or state changed, when its transcript mtime is newer, when
  its window has 2 panes, or when its `pane_id` is gone. With a pane closed,
  `resurrect.save_now` is called once with `force=True`. `index_one` is
  called with `force=True`. A second close of the same session replaces the
  row. The record keeps the pane's profile.
- `resurrect.save_now(force=True)` unsets `@continuum-save-last-timestamp`
  before it runs the script; a plain `save_now()` does not.
- Retention: a row whose transcript is missing is deleted; a row whose
  transcript exists stays; a row whose pid a live window wears is deleted; a
  row whose session a live Claude runs is deleted. `prune_pane_tracks` keeps
  the tag of a pid with a `closed_panes` row, and prunes it after the row is
  gone.
- Resume, through the route, on both branches: `_patch_resume_path(mocker,
  calls, has_session=1)` for `new-session` and `has_session=0` for
  `new-window` (`tests/routes/test_sessions.py:594`). With a record
  `{pid: 'dddd4444'}`, a `pane_tracks` tag `t1` on `dddd4444`, and a
  `state.json` entry with `linked_pr: 12` and a name, run the route and then
  `resolve_pids`. On each branch, assert that the spawn argv has `-n <name>`
  and the chained `set-option -w @periscope_id dddd4444`, that the window's
  pid is `dddd4444`, its track is `t1`, `linked_pr` is 12, and the record is
  deleted. Assert also that no separate `set-option @periscope_id` call
  follows the spawn: the chained stamp is the fix for the race in section 5.
  A placeholder name gives no `-n`. A pinned name stays pinned. With no
  record, nothing changes.
- Resume, the profile, on both branches: a record with the profile `full`
  gives `-e CLAUDE_WRAPPER_PROFILE=full` in the spawn argv and no
  `ANTHROPIC_MODEL`. On the `new-session` branch, `scrub_session_env` runs
  after the spawn. A record with no profile gives no profile `-e`.
- Resume, a session that came back: a record whose pid a live window wears,
  and a record whose session a live Claude runs, each give a fresh pid and
  delete the record.
- Resume, through `resume_session`: the tool keeps the record's pid. A failed
  `new-window` keeps the record.
- Preview (`tests/routes/test_idle_close.py`): with 3 weekdays and a frozen
  clock, one pane for each verdict code, one pane with no stamp (200, and no
  row for it), and one pane that crosses the limit between now and the next
  04:00. With the setting off, an empty list.
- `/api/history/search` (`tests/routes/test_history.py`): a `closed_panes` row
  gives `closed_at`; a session with no row has none.
- `PATCH /api/settings`: 0 and 31 give 400; `true` and `"3"` give 422; null
  removes the key.

## 8. Known gaps

- **Wrapper profile with no record**: a resume of a session that has no
  closed record runs the default profile, as today. Nothing keeps the profile
  of a session that ended in another way, for example with `/exit` or × kill.
- **Move-account and the profile**: move-account resumes a live pane's session
  on the other account (`periscope/routes/sessions.py:589`) but passes no
  profile, so a `full` pane comes back on the default profile. This is true
  today. [THI-471](https://linear.app/thibault-dody/issue/THI-471) tracks the
  fix.
- **Background shells**: an idle Claude can own a background shell, for example
  a dev server that it started. Periscope has no signal for this. The sweep
  kills it with the pane.
- **Second account**: `jsonl_for_session` looks only in `~/.claude/projects`.
  The move-account docstring says another account's `projects` folder is a
  link to it. If it is not, an account-B pane gets `no-transcript` and never
  closes. That is safe, and a test pins it.
- **A session that comes back outside periscope** keeps its record until the
  next sweep, so `/history` shows it as closed for up to a day.
- **The stamp race on other spawn paths**: six call sites open a window, then
  stamp it with `stamp_new_window` in a second tmux command. A dashboard build
  between the two can mint a second id and stamp it last. Resume avoids this
  with the chained stamp (section 5), and it covers two of the six:
  - `periscope/channels.py:1023`, the `resume_session` tool, takes resume's
    pid (section 5).
  - `periscope/routes/sessions.py:604`, move-account, stamps a second id on a
    window that the chain already stamped. A poll mints only for a window
    with no stamp (`periscope/pids.py:195-242`), so no poll stamp lands last.

  Four keep the race:
  - `periscope/routes/sessions.py:375`: the new-pane route.
  - `periscope/channels.py:655`: the `spawn_claude` tool.
  - `periscope/worktree_spawn.py:410` and `:426`: the shell and Claude windows
    of a new project.

  A read-only check of prod found that 5 of the 21 `pane_tracks` rows written
  since the 2026-11-18 boot are on pids that no poll saw.
  [THI-468](https://linear.app/thibault-dody/issue/THI-468) tracks the fix.

## 9. Decisions made in review

- **D1. Panes in the `done` state** (a result that you did not read).
  Decided on 19 Nov: keep them open. They
  show as news on the rail, and they wait for you to read them, like a pane
  that needs you.
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
- Claude writes the transcript at the first prompt, not at start. If so, a
  Claude that you started and never prompted has no transcript. It gets
  `no-transcript`, or `None` when you also never acted on it in periscope.
  Check: start a test Claude with no prompt and look
  for `~/.claude/projects/*/<sid>.jsonl`.
- `build_state()` at 04:00 takes 3 to 13 seconds, as at other times. The
  re-check before each kill covers a pane that changes in that time.
