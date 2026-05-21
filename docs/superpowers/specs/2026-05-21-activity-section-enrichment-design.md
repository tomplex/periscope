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
- Make rows actionable (open the run/commit; reply to a blocked pane).
- Add a *curated, low-noise* session slice: Haiku-synthesized "completed
  X" milestones and context-compaction events.

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

- a SQLite durable store (`activity.db`),
- the read-path merge (persisted events + freshly-computed git events),
- the Haiku milestone summarizer and compaction detector.

`periscope/git_pr.py` keeps `shared_activity_for` (git/CI computation
only). `cached_pane_activity` moves to `activity.py` and calls into
`git_pr.shared_activity_for` for the git half. This follows periscope's
"one file per subsystem" convention; Activity logic currently smeared
across `git_pr.py` + `channels.py` + `modal.js` gets a single owner.

### Event model

Every row the API returns is an event:

| field   | meaning |
|---------|---------|
| `src`   | `git` \| `alert` \| `session` — drives row styling in `modal.js` |
| `kind`  | `commit`/`ci`/`open` (git) · `alert` · `milestone`/`compaction` (session) |
| `at`    | unix seconds |
| `text`  | display text |
| `state` | optional — `passed`/`failed`/`running` (ci) · `done`/`need_human`/`info` (alert) |
| `url`   | optional — clickable target (thread 3) |

`commit`/`ci`/`open` are computed on demand. `alert`/`milestone`/
`compaction` are persisted. The read path merges and sorts newest-first.

## Thread 1 — durable store + wider window

*(First commit. No AI. Immediate value.)*

### Store

SQLite DB at `~/.config/periscope/activity.db` — alongside `state.json`
(`$XDG_CONFIG_HOME/periscope/`, see `store.py:_state_path`). WAL mode, so
a dev instance (port 8766) and prod (8765) can both write without lock
contention.

```sql
CREATE TABLE events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind  TEXT NOT NULL,        -- 'pane' | 'branch'
  scope_key   TEXT NOT NULL,        -- periscope_id  |  repo_path\x1fbranch
  event_kind  TEXT NOT NULL,        -- 'alert' | 'milestone' | 'compaction'
  at          INTEGER NOT NULL,     -- unix seconds
  text        TEXT NOT NULL,
  detail      TEXT,                 -- alert: done/need_human/info; else NULL
  url         TEXT,
  payload     TEXT                  -- JSON, optional extras
);
CREATE INDEX idx_events_scope ON events (scope_kind, scope_key, at);

-- Worker bookkeeping: milestone last-summarized SHA per (path,branch),
-- compaction tailer byte-offset per transcript file (threads 2/3).
CREATE TABLE cursors (key TEXT PRIMARY KEY, value TEXT);
```

`activity.py` API (all stdlib `sqlite3`, no ORM):

- `record(scope_kind, scope_key, event_kind, text, *, at=None, detail=None, url=None, payload=None)`
- `events_for(periscope_id, repo_path, branch, limit=40) -> list[dict]`
  — reads `pane`-scoped rows for the `periscope_id` and `branch`-scoped
  rows for `(repo_path, branch)`, mapped into the event model above.
- `prune(max_age_days=30)` — called once at lifespan startup; bounds DB
  growth. 30d is generous; the modal shows far fewer.

### Keying

- **Alerts & compaction** → `scope_kind='pane'`, `scope_key=periscope_id`.
  `periscope_id` (`periscope/pids.py`) is the stable pane identity;
  unlike tmux's `%N`, it does not get reused when a pane is destroyed,
  so old activity cannot bleed into a recreated pane.
- **Milestones** → `scope_kind='branch'`, `scope_key=f"{repo_path}\x1f{branch}"`.
  Milestones are commit-anchored, so they share the keying of the
  `commit` events they summarize and appear for every pane on the branch.

### Alerts move to the store

`channels.py:_do_notify_tool` currently appends to `_CHANNEL_ALERTS`.
It additionally calls `activity.record(...)`. `_CHANNEL_ALERTS` stays as
a write-through in-memory cache — the unread badge and the existing
`channel_alerts` field on `/api/pane` keep working unchanged. On
startup, `activity.py` does **not** rehydrate `_CHANNEL_ALERTS` (unread
state is intentionally session-fresh); it only feeds the merged stream.

Resolving `periscope_id` inside `_do_notify_tool`: the channel layer
already maps a pane → pid (see `channels.py` pid-mint path); `pids.py`
resolves pid → `periscope_id`.

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
- `commit` → `https://github.com/<owner>/<repo>/commit/<sha>`. The
  owner/repo is already parsed from the status line's PR URL
  (`PR_RE` / status parsing in `panes.py`); the commit SHA is added to
  the `git log` `--pretty` format (`%H`).
- `milestone` → the GitHub *compare* URL across the summarized commit
  range (`/compare/<first>^...<last>`).

**Inline reply on the pinned `need_human` alert.** The
`activity-pinned` block gains a one-line text input + send button that
POSTs to `/api/send` (the existing endpoint — session/index as query
params, multi-line-safe paste path). This unblocks a stuck pane without
opening the terminal. Only the pinned alert gets this; ordinary stream
rows do not.

## Thread 2 — Haiku milestones + compaction

*(Commits 2 and 3. Compaction is commit 2; milestones commit 3.)*

Both features need to read the **live** transcript JSONL for a pane.

### Locating the live transcript

Claude Code writes transcripts to
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, where
`encoded-cwd` is the pane's cwd with `/` and `.` replaced by `-` (e.g.
`/Users/tom/dev/periscope` → `-Users-tom-dev-periscope`). The encoding
is lossy and not reversible, so resolution is **forward-only**:

1. Encode the pane's cwd, glob `*.jsonl` in that dir, pick newest mtime.
2. Confirm the file's recorded `cwd` field matches the pane's cwd
   (transcript entries carry `cwd`).

`history/backfill.py` already has `DEFAULT_PROJECTS_DIR` and
`find_jsonl_files`; `activity.py` adds a `live_transcript_for(cwd)`
helper rather than reaching into `history/` internals.

### The activity worker

Both features run from **one lifespan-driven background loop**
(`activity.py:run_worker`, started via `_task` in the app lifespan, not
piggybacked on any request path). Every ~30s it walks all active Claude
panes (`panes.list_windows`) and, per pane, does the compaction tail;
per unique `(path, branch)`, does the milestone check. This is
deliberately lifespan-driven: a commit run in a pane whose modal is
*not* open must still produce a milestone, so the worker cannot hang off
the modal-only `/api/pane` fetch.

### Compaction events (commit 2)

The worker tails the live transcript of each active Claude pane. The compaction marker is a
transcript entry with `type: "system"`, `subtype: "compact_boundary"`,
carrying `timestamp` and a `compactMetadata` object
(`trigger` = `manual`/`auto`, `preTokens`, `postTokens`). On a match the
tailer calls `activity.record(scope_kind='pane',
event_kind='compaction', ...)` with text derived from the metadata —
e.g. `context compacted (auto · 303k → 14k tokens)`.

The tailer tracks a per-file byte offset (in the `cursors` table below)
so each restart re-reads only new lines. It does not re-emit compaction
events already in the DB (dedup on `(scope_key, event_kind, at)`).

### Haiku "completed X" milestones (commit 3)

Trigger: a **commit run** finishes on a `(path, branch)`.

Each worker tick calls `maybe_emit_milestone(path, branch)` for every
unique `(path, branch)` across active Claude panes:

1. Read `HEAD` SHA. Compare to the last-summarized SHA for this
   `(path, branch)`, stored in a `cursors` table —
   `cursors(key TEXT PRIMARY KEY, value TEXT)`, also used for the
   compaction tailer's per-file byte offsets.
2. If `HEAD` advanced **and** every pane on the branch is idle
   (`parse_pane` state ≠ `working` — i.e. the commit run is over),
   proceed; otherwise wait for the next tick (debounce — a burst of
   commits collapses into one summary once the pane goes quiet).
3. Gather: commit subjects + bodies since the last-summarized SHA
   (`git log <last>..HEAD`), and the user-turn text from the live
   transcript since the previous milestone's `at`.
4. `build_milestone_prompt(commits, prompts)` → `claude_complete(prompt,
   model="claude-haiku-4-5")` (the `rename_ai.py` helper — same client,
   same Haiku model). The prompt asks for **one line**, format
   `completed: <feature>`, ≤ 80 chars.
5. `activity.record(scope_kind='branch', event_kind='milestone', ...)`,
   `at` = the newest commit's timestamp, `url` = the compare URL.
   Advance the last-summarized SHA.

Caps: at most ~15 commits and ~4000 chars of prompt text fed to Haiku
per call (truncate oldest-first) — bounds cost and latency.

### Failure modes

- **No API key** → `get_anthropic()` raises; `activity.py` catches,
  logs once, and disables the milestone feature for the process. Same
  posture as `history/`'s optional summaries. Compaction (no AI) is
  unaffected.
- **No transcript found** → milestone summarizes commit subjects only;
  compaction tailer skips that pane.
- **Haiku error / timeout** → skip this run; the last-summarized SHA is
  *not* advanced, so the next tick retries.
- All background work goes through `_bg` / `_task` (crash-surfacing
  invariant #8).

## Dashboard-wide feed

Milestones are written to the store as `branch`-scoped events. The
notifications feed (`static/alerts.js`, dashboard-wide) gains a read of
recent `milestone` events alongside its existing alert aggregation —
cheap, since they are already in the DB. **Compaction events stay
modal-only** (low value as a global signal). No new endpoint: the feed
reads milestones via the same `activity.py` query layer.

## Staging

Three independent commits, each shippable on its own:

1. **Store + wider window + actionable rows.** New `activity.py` with
   the SQLite store; alerts write through to it; `cached_pane_activity`
   moves over and merges; `PERISCOPE_ACTIVITY_DAYS`; `url` on `ci`/
   `commit` rows; inline reply on the pinned alert. No AI.
2. **Compaction events.** Live-transcript tailer; `compaction` rows.
3. **Haiku milestones.** `maybe_emit_milestone`, the prompt builder,
   the milestone row + compare URL, milestones in the notifications feed.

## Testing

`tests/test_activity.py` (mirrors `periscope/activity.py`, per the
`tests/test_<module>.py` convention):

- Store CRUD: `record` then `events_for` round-trips; `prune` drops
  rows past `max_age_days`.
- Read-path merge: `pane`-scoped + `branch`-scoped + computed git events
  sort newest-first into one stream.
- `live_transcript_for`: encodes cwd correctly; picks newest mtime;
  rejects a dir whose transcript `cwd` mismatches.
- `build_milestone_prompt`: shape assertions on the prompt text.
- Milestone trigger: with a recorded `git log` fixture and a **mocked**
  `claude_complete`, `maybe_emit_milestone` emits exactly one row for a
  multi-commit run and advances the SHA; a no-op when `HEAD` is
  unchanged or a pane is still `working`.
- Compaction: a JSONL fixture with a compaction marker yields one
  `compaction` row; re-running the tailer does not duplicate it.

Existing suites (`test_parse_pane.py`, channel smoke) are untouched.
```
uv run pytest -q
```
