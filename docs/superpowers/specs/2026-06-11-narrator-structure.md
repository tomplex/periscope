# Narrator — code structure proposal

**Spec:** `docs/superpowers/specs/2026-06-11-narrator-design.md`
**Date:** 2026-06-11

## Assumptions

1. **Cooldown stamp before first narrator row.** A manual rename can land on a
   pane that has no `pane_status` row yet; the cooldown must still apply. The
   stamp accessor upserts a placeholder row (`status=''`, `jsonl_size=0`) —
   satisfies the spec's `NOT NULL`, the state-route merge skips empty
   statuses, and `should_regenerate` treats `jsonl_size=0` as "differs" so the
   first real status still generates promptly.
2. **`POST /api/rename` resolves `pane_id` itself.** The route has only
   `session:index`; it fetches the active pane id via
   `tmux("display-message", "-t", target, "-p", "#{pane_id}")` before
   stamping. The auto-rename routes already have `pane_id` in their
   `list_windows()`-derived dicts.
3. **Tail bound for the path-variant parse** is a `deque(fh, maxlen=500)` line
   read (same pattern as `activity._recent_user_prompts`'s 2000 and
   `_recent_compact_meta`'s 200); 500 raw lines comfortably covers 3 prompts +
   8 tool calls.
4. **Narrator prompt uses status + rename in one JSON object** per spec; the
   prompt builder lives with its consumer (the convention:
   `build_milestone_prompt` lives in `activity.py`, `build_rename_prompt` in
   `rename_ai.py`).

## File layout

```
periscope/
  narrator.py                  NEW — decision core (pure) + tick orchestration
  activity.py                  + pane_status schema, PaneStatusRow, CRUD/prune accessors,
                                 'rename' event kind (no code change — kinds are free-form),
                                 one lazy-import narrator.tick(panes) call in _worker_tick
  rename_ai.py                 + RENAME_RULES shared taste constant (extracted from
                                 build_rename_prompt), transcript_summary_from_path variant
  routes/state.py              + one bulk pane_status read, dict-merge into window payloads
  routes/pane.py               + stamp renamed_at in POST /api/rename
  routes/auto_rename.py        + stamp renamed_at at both tmux rename-window apply sites
  app.py                       + prune_pane_status(alive) beside prune_pane_sessions in lifespan
static/src/
  split/RailRows.jsx           + muted truncated status_line second row (+ stale dim)
  split/Detail.jsx             + untruncated status_line in header, "as of Nm ago" title attr
  inspector/Inspector.jsx      + 'rename' case in timelineColor/timelineLabel
static/dist/app.js             rebuilt + committed (project convention)
tests/
  test_narrator.py             NEW — pure-core matrix + tick tests (fresh_db pattern)
  test_activity.py             + pane_status CRUD/prune cases
  test_rename_ai.py            + transcript_summary_from_path + shared-rules cases
  routes/test_state.py         + seeded pane_status row → status_line/status_at in payload
```

## Per-module structure

### `periscope/narrator.py` — NEW

**Rung: frozen data + pure functions, with a thin impure shell.** Matches the
codebase convention (`activity.py`, `rename_ai.py` are flat modules); nothing
here owns coupled mutable state. The only in-process state is the
disabled-latch — a single module-level tri-state, not a class:

```python
_enabled_checked: bool | None = None   # None = not yet checked
def _enabled() -> bool: ...            # one-shot ANTHROPIC_API_KEY env check,
                                       # logs the disable line exactly once
```

A class for one memoized boolean fails the "earns its keep by encapsulating
state" bar. All other tick-to-tick state lives in the `pane_status` table
(spec design — that's what makes statuses survive restarts).

Constants: `MIN_INTERVAL_S = 90`, `MAX_PER_TICK = 5`,
`RENAME_COOLDOWN_S = 1800`, `STATUS_MAX_LEN = 72`.

**Pure decision core** (no DB, no tmux, no API — directly unit-testable):

```python
@dataclass(frozen=True)
class NarratorResult:
    status: str
    rename: str | None

def should_regenerate(row: PaneStatusRow | None, *, session_id: str,
                      jsonl_size: int, now: int) -> bool
def pick_regenerations(candidates: list[tuple[str, ...]], *,  # oldest generated_at first
                       cap: int = MAX_PER_TICK) -> list[...]
def parse_response(raw: str) -> NarratorResult | None          # strict JSON, status length;
                                                               # None → keep previous status
def rename_decision(suggestion: str | None, *, current_name: str,
                    row: PaneStatusRow | None, now: int) -> str | None
    # guards: == current name, cooldown, format (1-3 dash-words, ≤25 chars, charset)
def is_external_rename(row: PaneStatusRow, current_name: str) -> bool
    # current differs from seen_name and from any narrator-applied name
def build_narrator_prompt(*, window_name: str, branch: str | None,
                          pr: int | None, cwd: str, signals: dict) -> str
    # reuses rename_ai.RENAME_RULES for the rename half; status rules inline here
```

`should_regenerate` takes the typed row, not a cursor — the session-switch
reset (clear `jsonl_size`/`renamed_at`) is a *write* the shell performs when
the function returns true for the session-changed reason; to keep the core
pure it returns a small reason enum
(`Regen = Literal["session_switch", "grew", "first_sight"] | None`) rather
than a bare bool, so the shell knows whether to reset the row.

**Impure shell:**

```python
def tick(panes: list[tuple[dict, dict]]) -> None
    # called from activity._worker_tick with its existing (window, parsed) list;
    # per-pane try/except + log.exception (the milestone-check pattern);
    # resolves session via activity.get_pane_session + the */<id>.jsonl glob
    # (same glob as turns._jsonl_for_session — NO cwd fallback, per spec);
    # stat → should_regenerate → transcript_summary_from_path →
    # claude_complete → parse_response → upsert row → rename_decision →
    # tmux rename-window + activity.record('rename' event)
```

Imports: `import periscope.activity as activity` (module-object import, the
`turns.py` style), `from periscope.rename_ai import claude_complete,
transcript_summary_from_path, RENAME_RULES`, `from periscope.tmux import tmux`,
`from periscope.git_pr import cached_git_state`. `claude_complete` and `tmux`
are imported *into the narrator namespace* so tests monkeypatch
`narrator.claude_complete` — the exact pattern `tests/test_activity.py` uses
against `activity.claude_complete`.

**Import cycle:** `activity._worker_tick` calls `narrator.tick`, and narrator
imports activity — so the import in `activity.py` is function-level inside
`_worker_tick` (one line, commented), keeping `activity.py`'s top-level import
graph acyclic. Everything else about `activity.py`'s "gains only schema +
event kind + one call" stays true.

### `periscope/activity.py` — extended

**Rung: functions (existing module style).** Owns all SQL, as it does for
`pane_sessions` / `usage_samples` / `ui_events` — narrator never touches
`_CONN`/`_LOCK` directly. New `# --- pane_status: narrator storage ---`
section:

```python
@dataclass(frozen=True)
class PaneStatusRow:          # lives here because the schema owner defines the row type;
    pane_id: str              # putting it in narrator.py would force activity→narrator import
    session_id: str | None
    status: str
    generated_at: int
    jsonl_size: int
    seen_name: str | None
    renamed_at: int | None

def get_pane_status(pane_id: str) -> PaneStatusRow | None
def all_pane_statuses() -> list[PaneStatusRow]            # narrator tick: oldest-first cap selection
def upsert_pane_status(row: PaneStatusRow) -> None
def stamp_pane_rename(pane_id: str, *, name: str, at: int) -> None
    # UPSERT seen_name + renamed_at; inserts the ''-status placeholder when absent (Assumption 1)
def pane_status_lines() -> dict[str, tuple[str, int]]     # bulk read for routes/state.py;
                                                          # skips rows with empty status
def prune_pane_status(alive_pane_ids: set[str]) -> int    # mirror of prune_pane_sessions
```

Schema appended to `_SCHEMA` (verbatim from the spec). The `'rename'` event
kind needs zero code in `events_for`/`_row_to_event` (free-form kinds map to
`src:"session"` — settled in spec review). `_worker_tick` gains the lazy
import + `narrator.tick(panes)` after the milestone loop, before
`checkpoint()`.

### `periscope/rename_ai.py` — extended

**Rung: functions (existing module style).** Two additions:

1. `RENAME_RULES: list[str]` — the constraint lines lifted out of
   `build_rename_prompt` (1-3 words / ≤25 chars / concept-over-mechanism /
   bad-good examples / keep-if-apt). `build_rename_prompt` splices the
   constant back in; `narrator.build_narrator_prompt` imports it. A list of
   lines, not a joined string — both builders assemble line-lists.
2. `transcript_summary_from_path(jsonl_path: Path, *, n_user: int = 3,
   n_tools: int = 8, tail_lines: int = 500) -> dict` — same return shape as
   `transcript_summary`. Lives here, not in narrator.py, because it shares
   `_TOOL_PATH_FIELD` / `_summarize_tool_call` and the signal-collection walk;
   placing it in narrator would mean importing private helpers across modules.
   Implementation: `deque` tail read of raw lines, skip
   `isMeta`/`isSidechain` raw entries (preserving the `messages_from_jsonl`
   filter, spec §Signal hygiene), adapt raw user/assistant entries into the
   minimal `{role, text, tool_uses}` shape, then run the **shared collector**
   extracted from the existing `transcript_summary` body:

   ```python
   def _collect_signals(messages: list[dict], *, n_user: int, n_tools: int) -> dict
   ```

   so the existing pane-resolving variant and the path variant cannot drift.
   It does **not** call `messages_from_jsonl` — that is full-file two-pass
   (tool-result back-patching the narrator doesn't need) and the whole point
   is bounding the parse.

### `periscope/routes/state.py` — extended

After the fan-out join (post-`annotate_hot_panes`):

```python
statuses = activity.pane_status_lines()        # ONE SELECT under activity._LOCK
for view in result:
    s = statuses.get(view.get("pane_id") or "")
    if s:
        view["status_line"], view["status_at"] = s
```

In the route, deliberately NOT in `window_view.build_window_view` — the
32-thread fan-out would serialize on `activity._LOCK` (settled in spec
review).

### Rename surfaces — `routes/pane.py`, `routes/auto_rename.py`

Each tmux `rename-window` apply site gains one
`activity.stamp_pane_rename(pane_id, name=..., at=now)` call:
`pane.py:166` (pane_id via `display-message`, Assumption 2),
`auto_rename.py:91` (session loop) and `auto_rename.py:155` (single-window).
Three call sites, one shared accessor — no helper module needed.

### Frontend

No new files. `RailRows.jsx`: second muted CSS-truncated line when
`status_line` present; `rail-dim` class when `now - status_at > 15min`.
`Detail.jsx`: untruncated line in the header with `title="as of Nm ago"`
(reuse `relTime` from `src/util.js`). `Inspector.jsx`: one branch each in
`timelineColor` / `timelineLabel` for `kind === "rename"` (lines 30–49 are the
existing kind maps). Rebuild + commit `static/dist/app.js`.

## Patterns

**Used:**
- Functional core / imperative shell — `narrator.py`'s decision functions are
  pure over `PaneStatusRow`; `tick()` is the only place IO happens.
- Frozen dataclass value-objects — `PaneStatusRow`, `NarratorResult`.
- Module-level constant extraction — `RENAME_RULES` shared by two prompts.
- Per-pane try/except + `log.exception` in `tick()` — the existing
  `maybe_emit_milestone` resilience pattern.
- Keyword-only args on all multi-arg functions (codebase + taste convention).

**Considered and rejected:**
- A `Narrator` class — the only mutable state is a one-shot disabled latch;
  everything else is in SQLite by design. Module global + function wins.
- Defensive parsing beyond the model boundary — `parse_response` /
  `rename_decision` are the *only* defensive code (model output is external);
  internal callers are trusted per spec.
- Custom exception types — none; failures keep the previous status and log,
  control flow never catches a narrator-specific type.
- Refactoring `messages_from_jsonl` to accept a tail — cross-package churn in
  `history/` for one consumer; the raw-line tail adapter is smaller.
- A shared `apply_rename()` helper wrapping tmux+stamp across the three
  routes + narrator — four call sites with different error handling and
  return needs; one stamp accessor is the actual shared part.

## Test strategy

All tests offline (project has no live-API tests; spec mandates none).

- **`tests/test_narrator.py`** — unit, no DB for the pure core:
  `should_regenerate` matrix (session switch, growth, shrink-via-clear, no
  row + transcript, interval gate, idle), `parse_response` (garbage JSON,
  missing/over-length status, malformed rename), `rename_decision`
  (== current, cooldown, format violations), `is_external_rename`,
  `pick_regenerations` oldest-first cap, prompt-builder snapshot for a
  fixture pane. Tick-level tests reuse the `fresh_db` autouse-fixture pattern
  from `tests/test_activity.py` (tmp `config.ACTIVITY_DB`, reset
  `activity._CONN`) with monkeypatched `narrator.claude_complete`,
  `narrator.tmux`, `narrator.transcript_summary_from_path`, and a tmp
  `activity._PROJECTS_DIR` — real SQLite, no mocks on the DB layer. Cover:
  rename records the `'rename'` event; Haiku exception keeps the previous
  row; disabled latch logs once.
- **`tests/test_activity.py`** — pane_status CRUD round-trip, placeholder
  upsert from `stamp_pane_rename`, `pane_status_lines` skips empty status,
  `prune_pane_status` against an alive set. Real SQLite via `fresh_db`.
- **`tests/test_rename_ai.py`** — `transcript_summary_from_path` on a
  hand-written fixture JSONL: prompts/tools/files extracted, isMeta and
  isSidechain entries dropped, tail bound respected on an oversized file;
  `RENAME_RULES` lines present in both `build_rename_prompt` and
  `build_narrator_prompt` output (the no-drift test).
- **`tests/routes/test_state.py`** — seeded `pane_status` row surfaces as
  `status_line`/`status_at` on the matching window and is absent otherwise;
  existing `client` + `_patch` machinery, real DB via the fresh-db fixture.
- **UI** — browser-verified per project convention; no component tests.

Testability note: keeping `should_regenerate` reason-returning and
row-dataclass-driven means the entire regeneration policy is testable with
zero fixtures — that is the structure preventing a mocked-tick-passes /
prod-fails gap.

## Decisions to sanity-check

1. **`PaneStatusRow` + all pane_status SQL live in `activity.py`, not
   `narrator.py`.** Alternative: narrator owns its own SQL section against
   `activity._conn`/`_LOCK`. Close because it grows activity.py with
   narrator-specific accessors; decided by the existing rule that activity.py
   owns every periscope.db tenant (`usage_samples` is consumed only by
   usage.py yet lives here) and by avoiding an activity→narrator type import.
2. **`transcript_summary_from_path` in `rename_ai.py`** (with the
   `_collect_signals` extraction). Alternative: in narrator.py, its only
   consumer. Decided by shared private helpers and keeping all
   transcript-signal extraction in one module; flagging because "helper lives
   with its primary consumer" cuts the other way.
3. **`should_regenerate` returns a reason literal, not a bool.** Alternative:
   bool + the shell re-deriving session-switch for the row reset. The reason
   enum keeps the reset decision inside the pure core; slightly fancier
   signature than the spec sketched.
4. **Placeholder `status=''` row for pre-status cooldown stamps**
   (Assumption 1). Alternative: relax the schema to `status TEXT` nullable,
   deviating from the spec's pinned DDL. The placeholder stays within the
   spec schema but means two read paths must skip empty statuses.
