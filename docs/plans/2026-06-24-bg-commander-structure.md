# `--bg` commander + job tracking — code-structure proposal

Spec: `docs/superpowers/specs/2026-06-24-bg-commander-design.md` (post-review revision).

Scope: replace the singleton tmux commander with per-command ephemeral `claude --bg`
commanders; add a `commands` job table; generalize the channel registry from
pane-id to handle; rework the omnibox console into a job list.

---

## 1. Spec pushback

Two structural assumptions in the spec are worth challenging before the plan is written.

**(a) "the `commands` table … in the activity DB."** The spec puts the job table in
`periscope.db` (activity.py's schema) "alongside the dropped commander marker." I agree
on the *database file* (one SQLite per process is the convention) but **not** on putting
the table DDL and its CRUD inside `activity.py`. `activity.py` is already a 800+ line
grab-bag (events, pane_sessions, pane_workspaces, usage_samples, ui_events, pane_status,
captain_log, commander, the worker tick, the read-path merge). Adding the `commands`
table + its CRUD + the `claude agents --json` sync + `claude stop` cleanup there grows the
god-module and couples dispatch to the activity worker's internals. **Group by domain:
dispatch + job-tracking is one concern and gets its own module** (`bg_commander.py`, see
§4), which opens its connection to the same `config.ACTIVITY_DB` via the same lazy
`sqlite3.connect` pattern. The schema (`CREATE TABLE IF NOT EXISTS commands …`) lives in
that module's own `_SCHEMA` executed against the shared DB file; SQLite tolerates two
modules creating their own tables in one file (history/db.py already does this against a
different db). This keeps `activity.py` getting *smaller* (commander + captain_log tables
leave it), not larger.

If Tom prefers one schema string for the whole `periscope.db`, the fallback is: DDL in
`activity._SCHEMA`, CRUD + sync + dispatch in `bg_commander.py`. I'd still keep the logic
out of activity.py. Flagged in §7.

**(b) Captain's log — "default: drop it."** The spec leans toward dropping captain's-log
entirely and I concur, but want the structural consequence stated plainly so the plan
doesn't half-remove it: dropping it deletes two `_CHANNEL_TOOLS` entries, two `_do_*`
handlers, `_require_commander`, `_CAPTAINS_LOG_KINDS`, and the `captain_log` table +
`append_captain_log`/`recent_captain_log`/`CaptainLogRow` in activity.py. That is *cleaner*
than rewiring `_require_commander → is_commander(handle)` for two tools a `--bg` commander
can't even reach (they're not in the pinned allowlist). **Recommendation: drop it.** The
proposal below assumes drop; if Tom wants to keep it, it's an additive `is_commander(handle)`
rewire and a `DROP TABLE` that doesn't happen.

---

## 2. Assumptions

- **Build-order step 0 (launchd subscription auth) passes** — `claude --bg` authes on
  subscription under the launchd env. The proposal assumes the dispatch is a plain
  `subprocess.Popen([...])` from the FastAPI process. If step 0 forces a login-shell or
  env-shim wrapper, that changes only the argv construction inside one function
  (`_dispatch_argv`), not the module structure. I isolate argv building precisely so this
  residual risk lands in one testable seam.
- **Dispatch is fire-and-forget**: `Popen`, don't wait. We capture the pid only to detect
  an immediate non-zero exit is *out of scope* (spec §"no failed status"). So dispatch does
  not need the child's exit code; `Popen` and move on. (If a synchronous "did it launch"
  check is wanted, `Popen` + a 0.0s poll is the seam — noted but not built.)
- **`messages_from_jsonl` location**: it lives in `history.search` and is re-exported via
  `periscope.turns`. The job-transcript endpoint reuses `turns.jsonl_for_session(session_id)`
  + `history.search.messages_from_jsonl` (same pair `_do_peek_tool` already uses).
- **`PERISCOPE_MCP_CONFIG` is generated once at lifespan boot** (prod-only), written under
  `config.config_dir()`, and its path is a `config.py` constant/function. Per-dispatch
  regeneration buys nothing (the content is static: it points channel_shim at the socket).
  The per-command identity is injected via **env on the Popen**, not via per-command config
  files — see §4 `bg_commander`.
- **The `cmdr:<id>` env handle reaches channel_shim via the dispatched process env**, which
  the `--bg` worker inherits into the channel_shim it spawns. Assumption: `claude --bg`
  propagates its process env to the MCP stdio server it launches. (This is the one runtime
  assumption the spec's "PERISCOPE_CALLER_ID in the shim's env" depends on; flagged for the
  step-1 synthetic-dispatch test.)
- **The omnibox keeps its existing `apiCall`/poll idioms**; the job list reuses the
  `Transcript.jsx` render path (`renderTurn`) for the read-only transcript view.

---

## 3. File layout

```
periscope/
  bg_commander.py            NEW. Dispatch + commands-table CRUD + status sync +
                             claude-stop cleanup + the orchestrator ROLE_PROMPT.
                             (replaces the deleted commander.py)
  commander.py               DELETED.
  channels.py                CHANGED. handle generalization: is_commander(handle),
                             second startswith guard relaxed, spawn_claude commander
                             branch keys off is_commander(handle), captain's-log tools
                             + _require_commander removed.
  channel_shim.py            CHANGED. PERISCOPE_CALLER_ID handle support; hello frame
                             carries the handle; the shim's own "%“-guard relaxed.
  activity.py                CHANGED (shrinks). Drop commander table + CommanderMarker +
                             set/get/clear_commander + is_commander_pane; drop captain_log
                             table + append/recent_captain_log + CaptainLogRow. Worker tick
                             gains a one-line call to bg_commander.sync_jobs().
  config.py                  CHANGED. Add PERISCOPE_MCP_CONFIG path, ORCHESTRATOR_PROMPT_FILE
                             path, and (optional) the model/allowlist constants.
  app.py                     CHANGED. Remove _archive_stale_commander_project + the
                             ensure_commander boot block; add the one-shot
                             bg_commander.write_mcp_config() at lifespan boot (prod-only).
  narrator.py                CHANGED. Remove _is_commander + the two is_commander_pane skips.
  routes/command.py          REWRITTEN. POST /api/command (dispatch), GET /api/command/jobs,
                             GET /api/command/jobs/{id}/turns. /api/command/status deleted.
  routes/state.py            CHANGED. Delete the is_commander_pane rail-hide line.

static/src/
  overlays/OpenOmnibox.jsx   CHANGED. useCommanderConsole/CommanderConsole removed; a
                             job-list + read-only transcript view added.
  open/JobList.jsx           NEW (optional split). Job-list panel + transcript view, if
                             OpenOmnibox.jsx would otherwise bloat. See §4.
  open/classify.js           UNCHANGED in shape — the "⚡ run" card stays; picking it now
                             dispatches + shows the job list rather than the console.

tests/
  test_bg_commander.py       NEW. Pure-seam unit tests (status mapping, agents-json parse,
                             handle parse, dispatch-argv construction).
  routes/test_command.py     REWRITTEN. Dispatch + jobs + jobs/{id}/turns route tests.
  test_channels.py           CHANGED. is_commander(handle) + relaxed guard tests.
  test_channel_shim.py       CHANGED. PERISCOPE_CALLER_ID handle in the hello frame.
  test_activity.py           CHANGED. Drop commander/captain_log assertions.
  test_commander.py          DELETED.
  test_commander_spawn.py    DELETED.
```

---

## 4. Per-module structure

### `periscope/bg_commander.py` — NEW (the answer to decision #1)

**Rung: plain functions over a frozen dataclass row.** No mutable state, no lifecycle, no
polymorphism. A job is an immutable value-object (`Job` frozen dataclass) read out of
SQLite; everything else is a pure-ish function (dispatch, CRUD, sync, cleanup). This is the
same shape activity.py uses for its rows (`@dataclass(frozen=True)` + module functions) and
exactly rung 1/2 of the ladder. **A `BgCommander` class would be over-abstraction** — there
is no coupled mutable state to encapsulate; the "state" is rows in a shared DB.

This module owns: the `commands` table DDL + CRUD, dispatch, `claude agents --json` parsing,
the running→done status mapping, and `claude stop` cleanup. One concern (a command's whole
lifecycle), one module.

```python
@dataclass(frozen=True)
class Job:
    id: str            # the --bg session_id (uuid)
    text: str
    cwd: str
    status: str        # 'running' | 'done'  (last-synced value)
    started_at: int

ROLE_PROMPT: str       # moved verbatim from commander.py (the orchestrator prompt)

# --- table ---
def _conn() -> sqlite3.Connection                          # lazy, shared ACTIVITY_DB, own _SCHEMA
def insert_job(*, id: str, text: str, cwd: str, at: int) -> None
def list_jobs() -> list[Job]                               # newest-first
def get_job(job_id: str) -> Job | None
def running_job_ids() -> set[str]                          # the is_commander() validation set
def _set_status(job_id: str, status: str) -> None

# --- dispatch (the one launchd-risk seam) ---
def dispatch(text: str, *, cwd: str | None = None) -> str  # mints uuid, inserts job, Popen, returns id
def _dispatch_argv(*, session_id: str, text: str) -> list[str]   # PURE — unit-tested
def _dispatch_env(*, session_id: str) -> dict[str, str]          # PURE — sets PERISCOPE_CALLER_ID=cmdr:<id>

# --- status sync (the answer to decision #3) ---
def parse_agents_json(raw: str) -> dict[str, str]          # PURE — {sessionId: state} for kind==background
def map_state(state: str | None, *, started_at: int, now: int, present: bool) -> str  # PURE
def sync_jobs(*, now: int | None = None) -> None           # read agents --json, map, persist, claude-stop done

# --- mcp config (lifespan boot) ---
def write_mcp_config() -> None                             # generate PERISCOPE_MCP_CONFIG json once
```

Rationale for each load-bearing decision:

- **`dispatch()` mints the id, inserts the `running` row, then Popens.** Insert-before-Popen
  closes the spec's "absent during the registration window" race from the *write* side: the
  row exists the instant `/api/command` returns `{job_id}`, so `is_commander` validation and
  the job list both see it immediately, independent of when `claude agents` catches up.
- **`_dispatch_argv` and `_dispatch_env` are split out as pure functions** precisely because
  build-order step 0 may force an env/login-shell change. The whole launchd-auth residual
  risk lands in two functions that take no I/O and are trivially unit-testable. If step 0
  says "wrap in a login shell," only `_dispatch_argv` changes.
- **`map_state` takes `present`, `started_at`, `now`, `state` and returns `'running'|'done'`.**
  This is the spec's full rule (state=="done"→done; other present state→running; absent &
  older-than-grace→done; absent & younger→running) as one pure function — the single most
  important unit-test target. The 60s grace is a module constant `_ABSENT_GRACE_S = 60`.
- **`sync_jobs()` is the shared callee for both the worker tick and the on-open read.** It
  reads `claude agents --json --all` once, calls `parse_agents_json`, then for each *running*
  job in the table applies `map_state`; transitions to `done` trigger `claude stop <id>`
  (spec resolution #4). Idempotent — safe to call from both the 30s tick and `GET /jobs`.

**The circular-import answer (decision #3):** `bg_commander.py` imports nothing from
`activity.py`. `activity.py`'s worker tick imports `bg_commander` lazily inside `_worker_tick`
(the same lazy-import pattern it already uses for `narrator`) and calls `bg_commander.sync_jobs()`.
Direction: **activity → bg_commander** (one-way), mirroring activity→narrator. `bg_commander`
depends only on `config`, `log`, `turns`/`history` (for the transcript endpoint it doesn't even
need — that's the route's job), and `subprocess`. No cycle possible.

### `periscope/channels.py` — CHANGED (the central handle rework, decision #2)

**Rung: stays plain functions. No handle type.** The spec asks whether `is_commander(handle)`
is a free function or a small handle type. **Free function.** A handle is a `str` that
starts with `%` or `cmdr:`; introducing a `Handle` dataclass/NewType for a YAGNI single-user
tool buys nothing — there are exactly two kinds, the discrimination is a 1-line prefix check,
and `_MCP_SESSIONS` is already `dict[str, Any]` keyed by an opaque string. A type here is
abstraction without a present concrete use (the taste rule: "a second is imaginable" does
not qualify, and here there isn't even a second representation — it's a tagged string).

Changes, all minimal:

```python
def is_commander(handle: str) -> bool:
    """True iff handle is a live commander: cmdr: prefix AND in the running-jobs set."""
    from periscope import bg_commander
    return handle.startswith("cmdr:") and handle[len("cmdr:"):] in bg_commander.running_job_ids()
```

- The accept guard in `_handle_mcp_connection` relaxes from `if not pane.startswith("%"):`
  to `if not (pane.startswith("%") or pane.startswith("cmdr:")):` (spec: **both** guards;
  this is the server-side one). The `finally` `_MCP_SESSIONS.pop(handle)` is already
  handle-agnostic — no change.
- **Rename the connection-handler local from `pane` to `handle`** in `_handle_mcp_connection`
  and `_run_mcp_for_pane`'s signature, and rename the hello key read from `hello.get("pane")`.
  The hello frame still carries one string; the variable name lies today (it's already an
  opaque key). Keep the per-tool handler param named `pane` to avoid a 25-call churn — they
  receive the handle; only the pane-dependent tools (not in the commander allowlist) treat it
  as a `%N`, and those are never reached by a commander. **Document this in a one-line comment
  at `_call_tool`** ("the handler param is the caller handle — `%N` for panes, `cmdr:<id>`
  for commanders; pane-dependent tools assume `%N` and are excluded from the commander
  allowlist server-trusts via the pinned `--allowedTools`").
- `_do_spawn_claude_tool`: `is_commander = activity.is_commander_pane(pane)` →
  `is_commander = channels.is_commander(pane)` (the local var shadows the function name today;
  rename the local to `commander_caller`). The commander branch already skips the
  `display-message` caller-context derivation conceptually — make it explicit: when
  `commander_caller`, do not run `tmux display-message -t <handle>` (a `cmdr:` handle is not a
  tmux target; today's code would call it on a bogus target). The spec calls this out
  ("skipped for commander callers").
- Remove `_require_commander`, `_CAPTAINS_LOG_KINDS`, `_do_captains_log_read_tool`,
  `_do_captains_log_append_tool`, and the two `_CHANNEL_TOOLS` captain's-log records.

### `channel_shim.py` — CHANGED

**Rung: unchanged (it's already a `Shim` class — a legitimate rung-3 concrete class owning
the reconnect state machine).** Do not restructure it. One surgical change: the handle source.

```python
CALLER_ID = os.environ.get("PERISCOPE_CALLER_ID", "") or os.environ.get("TMUX_PANE", "")
```

- `run()`'s guard relaxes from `if not TMUX_PANE.startswith("%"):` to accept `%` or `cmdr:`
  on `CALLER_ID` (spec: the shim-side guard, the second of the two).
- `_serve()`'s hello frame `{"pane": TMUX_PANE}` → `{"pane": CALLER_ID}` (keep the JSON key
  `"pane"` for wire compatibility with the unchanged server reader, OR rename to `"handle"`
  on both sides in the same change — prefer renaming the key to `"handle"` since both ends
  change together and the key name `"pane"` now lies; flagged in §7 as a close call).

### `periscope/config.py` — CHANGED

Plain constants/functions, leaf module (unchanged discipline).

```python
PERISCOPE_MCP_CONFIG = config_dir() / "bg-mcp-config.json"     # generated at boot
ORCHESTRATOR_PROMPT_FILE = config_dir() / "orchestrator-prompt.txt"  # written from bg_commander.ROLE_PROMPT
BG_COMMANDER_MODEL = "sonnet"
BG_COMMANDER_ALLOWED_TOOLS = (                                  # the pinned four (spec)
    "Read,Grep,Glob,"
    "mcp__periscope__catalog,mcp__periscope__open,"
    "mcp__periscope__create_workspace,mcp__periscope__spawn_claude"
)
```

Keep the allowlist string **in config.py, not buried in `_dispatch_argv`** — it's a
security-load-bearing constant (the spec's whole "pinned to four pane-independent tools"
argument). A constant named `BG_COMMANDER_ALLOWED_TOOLS` makes the pin auditable in one place.

### `periscope/routes/command.py` — REWRITTEN

Routes stay thin; all logic delegates to `bg_commander`. `raise HTTPException` per convention.

```python
POST /api/command           {text} -> {job_id}      # validate non-empty, bg_commander.dispatch(text)
GET  /api/command/jobs      -> [{id,text,status,started_at}]   # bg_commander.sync_jobs(); list_jobs()
GET  /api/command/jobs/{id}/turns -> {messages}     # get_job→404; turns.jsonl_for_session→messages_from_jsonl
```

- `GET /jobs` calls `sync_jobs()` then `list_jobs()` — the on-open fresh read (spec).
- `/jobs/{id}/turns`: `get_job(id)` (404 if unknown) → `turns.jsonl_for_session(id)` (404 if
  no transcript yet) → `history.search.messages_from_jsonl(...)`. Reuses the exact pair
  `_do_peek_tool` uses. No new transcript code.
- The `cwd` for dispatch: a "sensible repo root" (spec). Keep this decision in
  `bg_commander.dispatch`'s default (e.g. `config`-level default repo or `~`); the route just
  passes `text`. Flagged in §7 — the spec is thin on which cwd.

### `static/src/overlays/OpenOmnibox.jsx` (+ optional `open/JobList.jsx`) — CHANGED

The live console (`useCommanderConsole` + `CommanderConsole`) is replaced by a job list that
loads from `GET /api/command/jobs` and a row-selected read-only transcript from
`/api/command/jobs/{id}/turns`. Reuse `renderTurn` from `split/Transcript.jsx` for the
transcript body (don't re-implement turn rendering). Picking the "⚡ run" card POSTs
`/api/command`, then switches the omnibox into job-list mode with the new job selected.

**Split decision:** if adding the job-list + transcript view pushes OpenOmnibox.jsx past
~400 LOC of mixed concerns, extract `open/JobList.jsx` (the list + transcript panel) and let
OpenOmnibox import it — same way the omnibox already composes a classifier. Default: try
inline first; extract if it crowds the file. Flagged in §7.

---

## 5. Patterns

**Used:**
- *Frozen data + pure functions* (`Job` + module functions in `bg_commander`) — the row is an
  immutable value-object; logic is pure/CRUD.
- *Pure-function isolation of a risk boundary* (`_dispatch_argv`/`_dispatch_env`/`map_state`/
  `parse_agents_json`) — the launchd-auth and the `claude agents` wire-format risks are pushed
  into no-I/O functions so they're unit-testable and one-line-changeable.
- *Lazy import to break a cycle* (`activity._worker_tick` → `bg_commander`) — the established
  activity→narrator idiom, reused for activity→bg_commander.
- *Auditable security constant* (`config.BG_COMMANDER_ALLOWED_TOOLS`) — the pinned allowlist
  in one named place.
- *Tagged-string handle* (`%N` / `cmdr:<id>`) — discriminated by prefix, no wrapper type.

**Considered and rejected:**
- *A `Handle`/`CommanderHandle` type or `NewType`* — rejected: YAGNI; two-kind tagged string,
  1-line discrimination, the registry is already `dict[str, Any]`. (Decision #2.)
- *A `BgCommander`/`JobTracker` class* — rejected: no coupled mutable state to encapsulate;
  state is rows in shared SQLite. Class would be a function-bag (the rung-3 anti-trigger).
- *Putting the table/CRUD in activity.py* — rejected: grows a god-module; dispatch is its own
  domain. (Spec pushback (a).)
- *Per-dispatch MCP config file* — rejected: content is static; per-command identity rides on
  env, not a file. One boot-time `write_mcp_config()` suffices.
- *Rewiring `_require_commander` to `is_commander(handle)`* — rejected in favor of deleting
  captain's-log (the tools are unreachable by the new allowlist). (Spec pushback (b).)
- *Custom exception type for dispatch failure* — rejected: no caller catches a specific type;
  `HTTPException` at the route, built-ins below. (No `failed` status this phase anyway.)

---

## 6. Test strategy

The feature is prod-only (MCP socket + launchd auth), so end-to-end dispatch is a **manual
prod smoke test** (build-order step 6). The structure is deliberately shaped so the
*decision logic* is unit-testable off-prod without mocking `claude` or tmux.

| Module / seam | Test kind | Where | Notes |
|---|---|---|---|
| `bg_commander.map_state` | unit (pure) | `tests/test_bg_commander.py` | The core matrix: done→done; running/blocked→running; absent+old→done; absent+young→running. The highest-value test in the change — it encodes the spec's grace rule. |
| `bg_commander.parse_agents_json` | unit (pure) | same | Feed a captured real `claude agents --json --all` blob (a fixture string, v2.1.190 shape: `kind`, `state`) → `{sessionId: state}`, background-only. Pin the exact wire shape so a CLI bump that changes it fails here, not silently in prod (the Q1-migration lesson: don't let a mocked-away format pass while prod fails). |
| `bg_commander._dispatch_argv` / `_dispatch_env` | unit (pure) | same | Assert the pinned allowlist, `--bg`, `--session-id`, `--append-system-prompt-file`, `--strict-mcp-config`, and `PERISCOPE_CALLER_ID=cmdr:<id>`. Locks the security-load-bearing flags. |
| `bg_commander` table CRUD (`insert/list/get/running_job_ids/_set_status`) | integration (real sqlite) | same | Against a temp `ACTIVITY_DB` (the existing `fresh_activity_db`/XDG-redirect fixture). Real SQLite, no mock — matches activity.py's test style. |
| `bg_commander.sync_jobs` | integration (real sqlite) + injected agents-reader | same | Inject the `claude agents` raw string (don't shell out): seed running jobs, pass a blob, assert persisted statuses + which ids got `claude stop`. Make the `claude agents` call and the `claude stop` call injectable params (default real subprocess) so the test never spawns `claude`. **This is the testability seam** — `sync_jobs(*, agents_raw=None, stop_fn=None)` or a module-level `_AGENTS_CMD`/`_STOP_FN` injected like config's exec seams. |
| `channels.is_commander` | unit | `tests/test_channels.py` | prefix + running-jobs membership; `%N`→False, `cmdr:<unknown>`→False, `cmdr:<running>`→True. |
| relaxed accept guard | unit | `tests/test_channels.py` | a `cmdr:` hello is accepted (the regression the spec warns about: server guard alone rejects every `cmdr:`). |
| `channel_shim` `PERISCOPE_CALLER_ID` | integration (subprocess) | `tests/test_channel_shim.py` | The existing real-subprocess harness: set `PERISCOPE_CALLER_ID=cmdr:test`, assert the hello frame carries it. (Heed the `.venv` drift landmine in CLAUDE.md.) |
| routes (`/api/command`, `/jobs`, `/jobs/{id}/turns`) | integration (TestClient) | `tests/routes/test_command.py` | Monkeypatch `bg_commander.dispatch`/`sync_jobs`/`list_jobs` (the route is thin) and assert status codes + shapes; 404 on unknown job / no transcript. |
| `activity` deletions | unit | `tests/test_activity.py` | Drop commander/captain_log assertions; confirm the worker tick calls `bg_commander.sync_jobs` (monkeypatch + assert called). |
| omnibox job list | manual | — | Frontend; verified in prod smoke (step 6). `classify.js` "⚡ run" card stays unit-tested as today. |

**Testability flag (the one to enforce):** `sync_jobs` must accept the `claude agents` output
and the `claude stop` action as **injectable seams** (parameter or module-level overridable,
the way `config.claude_exec()`/`PERISCOPE_CLAUDE_EXEC` and `PERISCOPE_TMUX_SOCKET` are inert
in prod but redirectable in tests). Without that seam the only way to test the sync is to mock
`subprocess`, which is exactly the mocked-test-passes-prod-fails trap. With it, the pure
mapping + the persistence are both tested against real SQLite and a real captured wire blob.

---

## 7. Decisions to sanity-check

1. **`commands` table CRUD lives in `bg_commander.py`, not `activity.py`** (against the
   spec's "in the activity DB … alongside the marker"). *Alternative:* DDL+CRUD in
   activity.py. *Close because:* the spec literally says activity DB, and there's a mild
   pull toward one schema string per DB file. I chose the domain split (and activity.py
   *shrinks* net). If you want one `_SCHEMA`, put DDL in activity, keep logic in bg_commander.

2. **Drop captain's-log entirely** rather than rewire `_require_commander → is_commander`.
   *Alternative:* keep it, rewire the gate. *Close because:* it's a behavior removal; if you
   ever want ephemeral commanders to share cross-command memory, you'd re-add it — but the
   pinned allowlist already makes the tools unreachable, so keeping them is dead surface.

3. **No `Handle` type — `is_commander(handle: str)` is a free function.** *Alternative:* a
   `NewType`/dataclass for the handle. *Close because:* taste says generics/types are free,
   but this isn't generics — it's a wrapper around a 2-variant tagged string with 1-line
   discrimination. I judged it YAGNI; reasonable to disagree if you foresee a third handle kind.

4. **Hello-frame key renamed `"pane"` → `"handle"`** (both shim + server change together).
   *Alternative:* keep the `"pane"` key for minimal diff. *Close because:* the key name now
   lies, but it's a private wire contract between two files changed in the same commit, so the
   rename is cheap and honest. Minor; flag only because it touches `test_channel_shim.py`.

5. **Job-list view inline in `OpenOmnibox.jsx` vs. a new `open/JobList.jsx`.** *Alternative:*
   always extract. *Close because:* it depends on resulting LOC/concern-count, which I can't
   measure until written — default inline, extract if it crowds. Plan-writer should check the
   file size after.

6. **Dispatch `cwd` default.** The spec says "a sensible repo root" but doesn't pin it. I put
   the default in `bg_commander.dispatch` (e.g. `~` or a configured default repo). *Sanity-
   check:* is there a canonical "default repo root" periscope already knows (e.g. the main
   project dir), or is `~` fine since the commander only reads to decide placement and spawns
   workers elsewhere? Likely `~` is fine (mirrors the old singleton's `$HOME` cwd).
