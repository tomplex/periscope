# Activity section enrichment — design

Date: 2026-05-21

## Context

The modal sidebar's **Activity** section (`renderActivitySection` in
`static/modal.js`, fed by `cached_pane_activity` in `periscope/git_pr.py`)
is a merged chronological stream of four event kinds:

- `commit` — git commits in the last 24h
- `ci` — GitHub Actions runs on the branch (`passed`/`failed`/`running`)
- `alert` — Claude's channel `notify()` calls (`done`/`need_human`/`info`)
- `open` — a single "opened in periscope" anchor from `_acted_at`

An unresolved `need_human` alert is pinned above the stream. Data rides
the existing 1.5s `/api/pane` poll; git events are cached per
`(path, branch)` with a 60s TTL; alerts live in `_CHANNEL_ALERTS`
(in-memory, keyed by pane).

Three weaknesses motivate this work:

1. **Nothing survives a restart.** Alerts are in-memory; git is a 24h
   window. `bin/periscope restart` wipes the alert history.
2. **Rows are inert.** A failed CI run is red text, not a link to the run.
3. **No view of what Claude did.** Periscope indexes every transcript in
   `history/` but the Activity stream shows none of that work.

## Goals

- Persist the events git cannot reconstruct, so Activity survives restarts.
- Make rows actionable: a CI run, a commit, a milestone link out to
  GitHub.
- Add a *curated, low-noise* session slice: Haiku-synthesized "completed
  X" milestones and context-reset events (`/clear` and compaction).

## Non-goals

- PR / review lifecycle events (deliberately cut — signal/noise).
- Raw tool-call activity (edits, reads, bash) — too noisy.
- Test/build-run events, session start/end, idle detection — cut in
  brainstorming as not clearing the signal bar.
- Persisting `commit`/`ci`/`open`: `git log` and `gh run list` already
  *are* their durable store. Persisting them would duplicate the source
  of truth.

## Architecture

A new module `periscope/activity.py` becomes the home for everything
Activity-related that is not raw git. It owns:

- a SQLite durable store (the shared `periscope.db`),
- the read-path merge (persisted events + freshly-computed git events),
- the Haiku milestone summarizer and the context-reset detector.

`periscope/git_pr.py` keeps `shared_activity_for` (git/CI computation
only). `cached_pane_activity` moves to `activity.py` and calls into
`git_pr.shared_activity_for` for the git half. This follows periscope's
"one file per subsystem" convention; Activity logic currently smeared
across `git_pr.py` + `channels.py` + `modal.js` gets a single owner.

**Import discipline.** `activity.py` imports `git_pr.shared_activity_for`
and (for the worker) `panes`, `rename_ai`, and the `store` XDG-path
helper; `channels.py` imports `activity`. To keep this a DAG, `git_pr.py`
must never import `activity.py`. `routes/pane.py` currently imports
`cached_pane_activity` from `git_pr` — that import moves to `activity`.
Like `store.py`, `activity.py` must do no DB work at import time (open
the connection lazily on first use) to avoid the import-order fragility
documented in `store.py`.

### Event model

Every row the API returns is an event:

| field   | meaning |
|---------|---------|
| `src`   | `git` \| `alert` \| `session` — drives row styling in `modal.js` |
| `kind`  | `commit`/`ci`/`open` (git) · `alert` · `milestone`/`reset` (session) |
| `at`    | unix seconds |
| `text`  | display text |
| `state` | optional — `passed`/`failed`/`running` (ci) · `done`/`need_human`/`info` (alert) |
| `url`   | optional — clickable target (thread 3) |

`commit`/`ci`/`open` are computed on demand. `alert`/`milestone`/
`reset` are persisted. The read path merges and sorts newest-first.

## Thread 1 — durable store + wider window

*(First commit. No AI. Immediate value.)*

### Store

SQLite DB at `~/.config/periscope/periscope.db` — alongside `state.json`
(`$XDG_CONFIG_HOME/periscope/`, see `store.py:_state_path`). The file is
named generically, not `activity.db`: it is the obvious destination for
other persistent state that currently lives in `state.json` (prefs,
projects). Migrating that JSON in is **out of scope here** — but the
generic name means doing it later needs no rename. `activity.py` owns
the `events` and `cursors` tables; a future consumer adds its own. WAL
mode, so a dev instance (port 8766) can read the DB for its modal while
the prod instance — the sole writer, see "The activity worker" — writes.

```sql
CREATE TABLE events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind  TEXT NOT NULL,        -- 'pane' | 'branch'
  scope_key   TEXT NOT NULL,        -- pane_id (tmux %N)  |  repo_path\x1fbranch
  event_kind  TEXT NOT NULL,        -- 'alert' | 'milestone' | 'reset'
  at          INTEGER NOT NULL,     -- unix seconds
  text        TEXT NOT NULL,
  detail      TEXT,                 -- alert: done/need_human/info; else NULL
  url         TEXT,
  payload     TEXT,                 -- JSON, optional extras
  dedup_key   TEXT UNIQUE           -- natural id; record() does INSERT OR IGNORE
);
CREATE INDEX idx_events_scope ON events (scope_kind, scope_key, at);

-- Worker bookkeeping: the milestone last-summarized SHA per
-- (path, branch). (Thread 2.)
CREATE TABLE cursors (key TEXT PRIMARY KEY, value TEXT);
```

`activity.py` API (all stdlib `sqlite3`, no ORM):

- `record(scope_kind, scope_key, event_kind, text, *, at=None, detail=None, url=None, payload=None, dedup_key=None)`
  — `INSERT OR IGNORE` on `dedup_key`, so a double-fired milestone is
  idempotent. `dedup_key=None` always inserts.
- `events_for(pane_id, repo_path, branch, limit=40) -> list[dict]`
  — reads `pane`-scoped rows for `pane_id` and `branch`-scoped rows for
  `(repo_path, branch)`, mapped into the event model above.
- `prune(max_age_days=30)` — called once at lifespan startup; bounds DB
  growth. 30d is generous; the modal shows far fewer.

### Keying

- **Alerts & resets** → `scope_kind='pane'`, `scope_key=pane_id` (the
  tmux pane id, `%N`). This matches how `_CHANNEL_ALERTS` and
  `routes/alerts.py` already key panes, and keeps `_do_notify_tool` a
  cheap dict-append + local SQLite insert — no tmux/git subprocess on the
  `notify()` hot path. `%N` is unique for the life of the tmux server and
  is never recycled, so it is a sound durable key; it survives
  `bin/periscope restart` because tmux outlives periscope. (It is lost
  only if the tmux server itself restarts — effectively a reboot.)
- **Milestones** → `scope_kind='branch'`, `scope_key=f"{repo_path}\x1f{branch}"`.
  Milestones are commit-anchored, so they share the keying of the
  `commit` events they summarize and appear for every pane on the branch.

### Alerts move to the store

`channels.py:_do_notify_tool` already receives the pane (`%N`) and
currently appends to `_CHANNEL_ALERTS`. It additionally calls
`activity.record(scope_kind='pane', scope_key=pane, ...)` — no extra
lookup, the key it needs is already in hand. `_CHANNEL_ALERTS` stays as
a write-through in-memory cache — the unread badge and the existing
`channel_alerts` field on `/api/pane` keep working unchanged. On
startup, `activity.py` does **not** rehydrate `_CHANNEL_ALERTS` (unread
state is intentionally session-fresh); it only feeds the merged stream.

### Wider window

The hardcoded `--since=24h` / `--limit` in `shared_activity_for` become
`PERISCOPE_ACTIVITY_DAYS` (default `7`). `git log --since=${N}d`, and
`gh run list --limit` raised to cover the window (e.g. 20). The read
path's `events[:8]` cap is lifted to ~40 for the modal; `modal.js`
renders a scrollable list already (`e70fb4a`).

## Thread 3 — actionable rows

*(Folded into the first commit — no AI dependency.)*

Events carry an optional `url`; `activityRow` in `modal.js` wraps the
row text in `<a href target="_blank" rel="noopener">` when `url` is set.
External links already work in both targets: a real browser opens the
tab natively; the Tauri shell intercepts http(s) clicks and routes them
through `plugin:opener|open_url` (`static/tauri.js`). No new plumbing.

URL sources:

- `ci` → the run's `url` (add `url` to the `--json` field list in
  `shared_activity_for`'s `gh run list` call).
- `commit` → `https://github.com/<owner>/<repo>/commit/<sha>`. periscope
  does **not** currently capture owner/repo (the status-line parser
  keeps only the PR number — there is no `PR_RE` retaining the slug);
  `git_pr.py` derives it fresh from `git remote get-url origin`,
  normalizing the `git@…:…` / `https://…` / `.git` forms. The SHA comes
  from adding `%H` to the `git log` `--pretty` format. A repo with no
  GitHub `origin` yields no `url` — the row simply stays inert.
- `milestone` → the GitHub *compare* URL across the summarized commit
  range (`/compare/<first>^...<last>`), built from the same origin.

The pinned `need_human` block stays **display-only** — no inline reply.
The modal already mirrors the live terminal one tab over; a reply box in
the sidebar would duplicate input the terminal handles better. The pin's
job is loud visibility, not data entry.

## Thread 2 — Haiku milestones + context resets

*(Commits 2 and 3. Context resets are commit 2; milestones commit 3.)*

Milestone summaries read the **live** transcript JSONL for a pane;
reset detection does not, but the compact-vs-clear *label* peeks at it.

### Locating the live transcript

Claude Code writes transcripts to
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. The directory
name encodes the cwd (observed: `/` and `.` → `-`), but that encoding is
not treated as authoritative — every transcript entry carries an
explicit `cwd` field, so resolution does not depend on reproducing it:

1. Glob `~/.claude/projects/*/*.jsonl` for files whose first entry's
   `cwd` matches the pane's cwd. The encoded dir name narrows the glob
   as an optimization, but the `cwd`-field check is what's trusted.
2. Among matches, pick the file with the most recent append (newest
   mtime) — the one currently being written. Known edge case: two live
   Claude sessions sharing one cwd; the worker picks the more recently
   active and accepts that as good enough.

`history/backfill.py` already has `DEFAULT_PROJECTS_DIR` and
`find_jsonl_files`; `activity.py` adds a `live_transcript_for(cwd)`
helper rather than reaching into `history/` internals.

### The activity worker

Both features run from **one lifespan-driven background loop**
(`activity.py:run_worker`, started via `_task` in the app lifespan, not
piggybacked on any request path). Every ~30s it walks all active Claude
panes (`panes.list_windows`) and, per pane, runs the context-reset
check; per unique `(path, branch)`, runs the milestone check. This is
deliberately lifespan-driven: a commit run in a pane whose modal is
*not* open must still produce a milestone, so the worker cannot hang off
the modal-only `/api/pane` fetch.

The worker is **gated to the prod instance** (`config.PORT == 8765`) —
the same guard `app.py` uses for the MCP listener. `periscope.db` is a
single shared file; two workers (a dev instance alongside prod) would
double-spend Haiku and race the milestone cursor. Prod is the sole
writer; a dev instance only reads the DB for its modal. To exercise the
worker in dev, run the dev instance on `PERISCOPE_PORT=8765` with prod
stopped. The per-tick `git rev-parse HEAD` overlaps the 60s
activity-cache's `git log` but at microsecond cost — not worth
deduplicating.

### Context-reset events (commit 2)

Both `/clear` and a compaction reset Claude's context — and `/clear`
gets far more use than compaction, so the stream must catch both.
`/clear` leaves **no** transcript marker (it is not a `local_command`
entry and has no `system` subtype); only compaction writes a
`compact_boundary` entry. So detection keys off the one signal common to
both: the **status-line context %**.

`STATUS_RE` already parses the context % on every poll, and that figure
only ever *climbs* during a session (tokens accumulate) — it drops
*only* on a reset. The worker tracks the last-seen context % per pane
(in memory) and, on a drop, records a `reset` event. The first
observation of a pane sets the baseline and is not a reset; a reset
during periscope downtime is missed (acceptable — periscope wasn't
watching). 30s granularity on the event `at` is fine.

**Compact-vs-clear label (best-effort).** On a detected drop the worker
does a one-shot read of the tail of the live transcript (via
`live_transcript_for`). A recent `compact_boundary` entry — `type:
"system"`, `subtype: "compact_boundary"`, with a `compactMetadata`
object (`trigger`, `preTokens`, `postTokens`) — labels the event
`compacted` with the token delta (`context compacted · 303k → 14k`).
No such entry → `cleared` (`context cleared · /clear`). The label rides
in `detail`; if the transcript can't be read, a generic `context reset`
text is used.

### Haiku "completed X" milestones (commit 3)

Trigger: a **commit run** finishes on a `(path, branch)`.

Each worker tick calls `maybe_emit_milestone(path, branch)` for every
unique `(path, branch)` across active Claude panes:

1. Read `HEAD` SHA. Compare to the last-summarized SHA for this
   `(path, branch)`, stored in the `cursors` table.
2. If `HEAD` advanced **and** every pane on the branch is settled —
   `parse_pane` state `idle` or `shell`, *not* `working` or
   `needs-input` (a pane blocked on a permission prompt mid-run is not
   done) — proceed; otherwise wait for the next tick (debounce — a burst
   of commits collapses into one summary once the panes go quiet). The
   worker calls `parse_pane` directly, so it sees the raw five-state
   output, not the `/api/state` refinement (`idle`→`done`, etc.).
3. Gather: commit subjects + bodies since the last-summarized SHA
   (`git log <last>..HEAD`), and the user-turn text from the live
   transcript since the previous milestone's `at`.
4. `build_milestone_prompt(commits, prompts)` → `claude_complete(prompt,
   model="claude-haiku-4-5")` (the `rename_ai.py` helper — same client,
   same Haiku model). The prompt asks for **one line**, format
   `completed: <feature>`, ≤ 80 chars.
5. `activity.record(scope_kind='branch', event_kind='milestone',
   dedup_key=f"milestone:{HEAD-SHA}", ...)`, `at` = the newest commit's
   timestamp, `url` = the compare URL. Advance the last-summarized SHA.

Caps: at most ~15 commits and ~4000 chars of prompt text fed to Haiku
per call (truncate oldest-first) — bounds cost and latency.

### Failure modes

- **No API key** → `get_anthropic()` raises; `activity.py` catches,
  logs once, and disables the milestone feature for the process. Same
  posture as `history/`'s optional summaries. Reset detection (no AI)
  is unaffected.
- **No transcript found** → milestone summarizes commit subjects only;
  a reset event falls back to the generic `context reset` label.
- **Haiku error / timeout** → skip this run; the last-summarized SHA is
  *not* advanced, so the next tick retries.
- All background work goes through `_bg` / `_task` (crash-surfacing
  invariant #8).

## Dashboard-wide feed

Milestones are written to the store as `branch`-scoped events. The
notifications feed (`static/alerts.js`, dashboard-wide) gains a read of
recent `milestone` events alongside its existing alert aggregation —
cheap, since they are already in the DB. **Reset events stay
modal-only** (low value as a global signal). No new endpoint: the feed
reads milestones via the same `activity.py` query layer.

**Out of scope, noted for the future:** higher-level *alert resolution*
— marking a `need_human` resolved, dismissing or acknowledging alerts —
belongs here, at the notifications-feed layer, backed by a `resolved_at`
column or a resolution event in the store. This spec does not build it;
it only ensures the store and the feed are the right seam for it later.

## Staging

Three independent commits, each shippable on its own:

1. **Store + wider window + actionable rows.** New `activity.py` with
   the SQLite store in `periscope.db`; alerts write through to it;
   `cached_pane_activity` moves over and merges; `PERISCOPE_ACTIVITY_DAYS`;
   `url` on `ci`/`commit` rows. No AI.
2. **Context-reset events.** The status-line-%-drop detector; the
   one-shot compact-vs-clear label; `reset` rows.
3. **Haiku milestones.** `maybe_emit_milestone`, the prompt builder,
   the milestone row + compare URL, milestones in the notifications feed.

## Testing

`tests/test_activity.py` (mirrors `periscope/activity.py`, per the
`tests/test_<module>.py` convention):

- Store CRUD: `record` then `events_for` round-trips; `prune` drops
  rows past `max_age_days`.
- Read-path merge: `pane`-scoped + `branch`-scoped + computed git events
  sort newest-first into one stream.
- `live_transcript_for`: matches a transcript on its `cwd` field; picks
  newest mtime among matches; rejects a file whose `cwd` mismatches.
- `build_milestone_prompt`: shape assertions on the prompt text.
- Milestone trigger: with a recorded `git log` fixture and a **mocked**
  `claude_complete`, `maybe_emit_milestone` emits exactly one row for a
  multi-commit run and advances the SHA; a no-op when `HEAD` is
  unchanged or a pane is still `working`/`needs-input`. A second run on
  the same `HEAD` is a no-op via the `dedup_key`.
- Reset detection: a falling context-% sequence emits one `reset` event;
  a climbing sequence emits none; the first observation sets the baseline
  silently. With a `compact_boundary` JSONL fixture the label is
  `compacted` + token delta; without one it is `cleared`.

Existing suites (`test_parse_pane.py`, channel smoke) are untouched.
```
uv run pytest -q
```
