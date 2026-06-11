# Narrator: semantic pane status + auto-rename on divergence

**Date:** 2026-06-11
**Status:** Approved (design review with Tom; execution delegated as autonomous)

## Problem

The dashboard shows *which* panes are busy (spinner, attention states) but
not *what* they're doing. Tom runs 10-25 Claude panes; knowing "this one is
migrating the usage scrape, that one is blocked on a schema question"
currently requires opening each pane. Window names also drift: a pane named
for last week's task quietly becomes this week's different task.

## What this builds

1. **Narrator status line** — a live, AI-generated one-liner per Claude
   pane ("fixing flaky reconcile test in tmux_mirror"), surfaced in the
   rail (muted line under the pane name, truncated) and the detail header
   (untruncated).
2. **Auto-rename on divergence** — when the narrator's view of the work
   meaningfully diverges from the window's current name, the window is
   renamed automatically (`tmux rename-window`), with an entry in the
   pane's activity feed. Windows only; session names untouched.

Deferred, explicitly out of scope: stuck-pane babysitter (semantic
blocked/looping detection), narrator for non-Claude panes, any frontend
state beyond rendering the line.

## Architecture

```
activity.run_worker (existing 30s loop, prod-only — port-8765 guard)
  └─ _worker_tick(...)  — existing reset/milestone checks
       └─ narrator.tick(panes)            NEW — one call, same thread
            per Claude pane:
              1. resolve session JSONL (activity.get_pane_session +
                 the same glob turns.py uses)
              2. stat it — skip unless grown since last status
                 AND ≥ MIN_INTERVAL_S since last generation
              3. signals = rename_ai.transcript_summary(...)  (existing:
                 recent prompts, tool calls, files) + current window
                 name, branch/PR (cached_git_state), cwd
              4. ONE Haiku call → {"status": ..., "rename": null | name}
              5. write pane_status row; maybe rename the window

/api/state (routes/state.py)
  └─ merge status_line from pane_status into each window's payload
       (db read per poll — also how a dev instance on 8766 sees
        prod-generated statuses; no shared in-process state)

frontend (static/src/)
  ├─ RailRows: muted one-line status under the pane name (CSS truncate)
  └─ Detail header: same line, untruncated
```

### `periscope/narrator.py` (new module)

One responsibility: per-tick, decide which panes need a fresh status,
generate it, persist it, and apply guarded renames. Called synchronously
from `_worker_tick` (which already runs off-loop via `asyncio.to_thread`).
`activity.py` gains only the `pane_status` schema, the `'rename'` event
kind, and the one-line tick call.

Decision core is pure and unit-testable:
- `should_regenerate(row, session_id, jsonl_size, now)` — true when
  EITHER the pane's current session id differs from `row.session_id`
  (covers `/clear`, which mints a new smaller JSONL — a size-only "grew"
  check would freeze the pre-clear status indefinitely — and tmux pane-id
  recycling across server restarts), OR the JSONL size differs from the
  stored `jsonl_size`; in both cases gated by
  `now - generated_at >= MIN_INTERVAL_S` (90s). On session switch the
  row's `jsonl_size` and `renamed_at` are reset. A pane with no stored
  row regenerates on first sight (if it has a transcript). Idle panes
  never regenerate — zero cost.
- **JSONL resolution happens once, in the narrator** (via
  `activity.get_pane_session` + the `<id>.jsonl` glob), and the resolved
  path is threaded into signal extraction — a `transcript_summary`
  variant that takes the path directly and tail-bounds its parse (the
  existing variant re-resolves via `get_turns_for_pane`, which falls back
  to newest-mtime-in-cwd and full-file-parses transcripts that can run
  tens of MB). **No cwd fallback for the narrator**: on a shared cwd a
  wrong-session status is worse than no status, and the hook
  self-corrects on the next prompt.
- Per-tick cap: at most `MAX_PER_TICK` (5) regenerations, oldest
  `generated_at` first. A 25-pane storm degrades to slightly-stale
  statuses, never a Haiku bill spike.

### Storage

`pane_status` table in periscope.db (schema owned by `activity.py`, like
`pane_sessions` / `usage_samples`):

```sql
CREATE TABLE IF NOT EXISTS pane_status (
  pane_id      TEXT PRIMARY KEY,   -- tmux %id
  session_id   TEXT,               -- Claude JSONL stem at generation time
  status       TEXT NOT NULL,
  generated_at INTEGER NOT NULL,   -- unix seconds
  jsonl_size   INTEGER NOT NULL,   -- size at generation (change check)
  seen_name    TEXT,               -- window name at last generation
  renamed_at   INTEGER             -- rename-cooldown stamp (narrator,
                                   -- manual routes, or detected external)
);
```

Pruned alongside the existing dead-pane pruning in lifespan housekeeping
(same `alive` set as `prune_pane_sessions`). Statuses survive restarts;
the `jsonl_size` comparison resumes cleanly.

### Model contract

One call per regeneration via the existing
`rename_ai.claude_complete(prompt, model="claude-haiku-4-5")`. The prompt
carries the same signal block `build_rename_prompt` uses today (priority:
recent user prompts → tool calls/files → branch/PR), plus the current
window name. Response is strict JSON:

```json
{"status": "fixing flaky reconcile test in tmux_mirror", "rename": null}
```

**Status rules (in-prompt):** ≤72 chars; present-progressive;
concept-level ("migrating usage scrape to OAuth endpoint", not "editing
usage.py"); no terminal/pane/window jargon; describes the most recent
*work* even if the pane has gone idle — the card's existing state chip
already conveys busy/idle, the narrator never duplicates it.

**Rename rules (in-prompt):** suggest only on *meaningful* divergence;
existing taste constraints (1-3 words, lowercase-with-dashes, ≤25 chars,
concept over mechanism, never generic); an explicit few-shot example of
returning `rename: null` when the current name is still apt. The failure
mode to fear is name churn, not staleness.

**Code-side guards (model output is an external boundary — defensive
parsing is justified here and only here):** drop the rename if it equals
the current name, if `renamed_at` is within `RENAME_COOLDOWN_S` (30 min),
or if it fails format constraints (length, charset). Drop the whole
response if JSON is malformed or `status` is missing/over-length (keep
the previous status; retry next tick naturally).

**Manual renames start the cooldown too** — the narrator must never
clobber a name a human just chose. Two mechanisms, both cheap:
(a) the in-process rename surfaces — `POST /api/rename` (routes/pane.py)
and both `/api/auto-rename-*` routes — stamp `pane_status.renamed_at`
when they apply a name; (b) the narrator stores the window name it last
observed (`seen_name`): if the current name differs from both `seen_name`
and any name the narrator itself applied, someone renamed it externally
(including tmux-native `rename-window`) — record the new name and start
the cooldown instead of renaming. Note: the cooldown is keyed per pane
while renames are per window; a window whose active pane changes gets a
fresh row and bypasses the cooldown — accepted edge case in a
one-pane-per-window workflow.

**Shared taste rules:** the rename constraints currently live in
`build_rename_prompt`; the narrator prompt reuses them. Extract the
shared rule block into a module-level constant in `rename_ai.py` consumed
by both prompts, so the taste can't drift.

**Renames apply** via `tmux("rename-window", "-t", target, name)` and
record an activity event — new `'rename'` kind in the existing events
table — so the pane's feed shows `renamed: claude → fs-liveness`.

### Signal hygiene

isMeta is already handled: `messages_from_jsonl` skips
`isMeta`/`isSidechain` raw entries (history/search.py), so
slash-command/skill expansions never reach `recent_user_prompts`. The
narrator's tail-bounded `transcript_summary` variant must preserve that
filtering.

### API + frontend

- `routes/state.py`: ONE bulk
  `SELECT pane_id, status, generated_at FROM pane_status` per poll,
  dict-merged by `pane_id` into the window payloads in the route — NOT a
  per-pane SELECT inside `window_view.build_window_view` (that runs on a
  32-thread fan-out and would serialize on `activity._LOCK`). Each window
  dict gains `status_line` (string or absent) and `status_at` (unix
  seconds — consumed by the UI as a dim-when-stale signal, below).
- `RailRows.jsx`: render `status_line` as a second, muted, CSS-truncated
  line when present; dim it when `status_at` is older than ~15 min (work
  moved on, narrator hasn't caught up or pane went quiet).
- `Detail.jsx` header: render it untruncated, with an "as of Nm ago"
  title-attr tooltip from `status_at`.
- `Inspector.jsx`: one label/color case for the new `'rename'` event
  kind (the fallback would render it generic-grey otherwise).
- Rebuild + commit `static/dist/app.js` (project convention).

## Failure modes

- **No `ANTHROPIC_API_KEY`:** narrator disables itself with one log line
  at first tick; dashboard works exactly as today. (Implementation note:
  `get_anthropic` raises per call — the narrator needs its own one-shot
  env check to deliver the disable-once behavior.)
- **Haiku error / garbage JSON:** previous status kept, exception logged,
  natural retry next tick. Never crash the worker tick (the existing
  milestone check sets the pattern: per-pane try/except with
  `log.exception`).
- **Pane with no transcript** (shell, hook never fired): no row, no UI
  line.
- **tmux rename failure** (window died mid-tick): logged, dropped.

## Cost

Haiku, ~1-2k input tokens + ~60 output per regeneration. A pane
completing turns continuously regenerates at most every 90s →
~$0.10/hour; idle panes cost nothing; per-tick cap bounds the worst case
at 5 calls / 30s regardless of pane count.

## Testing

- Unit (no API): `should_regenerate` matrix; response parsing/validation
  (garbage JSON, over-length status, malformed rename, rename==current);
  cooldown + format guards; oldest-first per-tick cap selection.
- Snapshot: prompt builder output for a fixture pane.
- Route: seeded `pane_status` row appears as `status_line`/`status_at` in
  the `/api/state` window payload.
- UI: verified in the browser (project convention — no component unit
  tests).
- No live-API tests anywhere.

## Facts settled during spec review (do not re-litigate)

- isMeta/isSidechain entries are already filtered by
  `messages_from_jsonl` (history/search.py:242) and
  `activity._recent_user_prompts`.
- The per-window dict is assembled in `window_view.build_window_view`
  (32-thread fan-out from routes/state.py); `pane_id` is present in it.
- A new `'rename'` event kind flows through `events_for`/`_row_to_event`
  unmodified (non-alert kinds map to `src:"session"`); only Inspector.jsx
  needs a label/color case.
- The worker tick is strictly sequential (await tick → sleep 30s), the
  reset check is cadence-insensitive, and `maybe_emit_milestone` already
  makes a blocking Haiku call in-tick — added narrator latency degrades
  cadence only.
- Dev (8766) reads the same XDG `periscope.db` (WAL mode), and the
  worker guard is `config.PORT == 8765` — dev shows prod-generated
  statuses with no extra plumbing.
